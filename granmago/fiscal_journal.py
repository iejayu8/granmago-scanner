"""Append-only fiscal journal of closed GranMago trades (SQLite + CSV export).

Shared with PR #14 (standalone/fiscal_journal.py): same schema and columns.

Design (see standalone/README.md for the full schema):

* ``trades`` holds one immutable row per closed leg. SQLite triggers abort any
  UPDATE or DELETE, so rows can only be appended. ``trade_key`` is UNIQUE and
  inserts use ``INSERT OR IGNORE``: replaying the same close is a no-op.
* The EUR rate is never fetched while the engine holds its ledger lock. A row
  is written first with ``fx_status = 'pending'``. A resolver later appends the
  rate to ``fx_resolutions`` (also append-only, one row per trade_key). The
  view ``trades_eur`` joins both and computes ``pnl_eur``. If the rate source is
  down the trade is still recorded, just without EUR.
* Rate: ECB euro foreign exchange reference rate USD per EUR
  (https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist-90d.xml), treating
  USDT as USD. For a trade closed on Madrid date D the observation dated D is
  used. If D is in the past and has no observation (weekend/holiday), the
  latest observation before D is used. If D is today and the ECB has not
  published yet, the row stays pending and is retried.

Readers (the Fiscal bot) should open the database read-only
(``file:...?mode=ro`` URI) or read the CSV export.
"""
import contextlib
import csv
import hashlib
import json
import os
import sqlite3
import threading
import xml.etree.ElementTree as ET
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

SCHEMA_VERSION = 2
# 1.8.0 (schema 2): fiscal trace per trade, added with ALTER TABLE ADD COLUMN
# (idempotent; existing append-only rows are never rewritten and read NULL in
# these columns). row_hash_version: NULL = v1 hash (v1 columns), 2 = all columns.
V2_COLUMNS = [
    ("entry_trade_ids", "TEXT"),      # Blofin tradeIds of the entry fills
    ("entry_fee_usdt", "REAL"),       # entry commission (Blofin fills, else order detail)
    ("exit_fee_usdt", "REAL"),        # exit commission (Blofin closing fills)
    ("fee_source", "TEXT"),           # blofin_fills | blofin_order_detail+fills | modelled_paper
    ("open_time_source", "TEXT"),     # blofin_fill | blofin_order | engine
    ("close_time_source", "TEXT"),    # blofin_fill | engine
    ("missing_fields", "TEXT"),       # demo/real: fiscal fields Blofin did not give ('' = complete)
    ("row_hash_version", "INTEGER"),
]
MADRID = ZoneInfo("Europe/Madrid")
ECB_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist-90d.xml"
ECB_SOURCE = "ECB eurofxref-hist-90d USD per EUR (USDT treated as USD)"

TRADE_COLUMNS = [
    "trade_key", "recorded_at_utc", "mode", "fiscal", "engine_version", "instance",
    "local_trade_id", "leg", "partial",
    "open_time_utc", "open_time_madrid", "close_time_utc", "close_time_madrid", "close_date_madrid",
    "pair", "side", "quantity_base", "notional_usdt", "entry_price", "exit_price",
    "fees_usdt", "funding_usdt", "funding_status", "pnl_usdt", "r_multiple", "reason",
    "entry_order_id", "exit_order_id", "client_order_id", "tpsl_id", "exchange_trade_ids",
    "signal_close_ms", "raw_json", "row_sha256",
] + [c for c, _ in V2_COLUMNS]
HASH_V1_COLUMNS = TRADE_COLUMNS[:TRADE_COLUMNS.index("row_sha256") + 1]

DDL = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS trades (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_key TEXT NOT NULL UNIQUE,
    recorded_at_utc TEXT NOT NULL,
    mode TEXT NOT NULL CHECK (mode IN ('paper','demo','real')),
    fiscal INTEGER NOT NULL CHECK (fiscal IN (0,1)),
    engine_version TEXT NOT NULL,
    instance TEXT NOT NULL,
    local_trade_id INTEGER,
    leg INTEGER NOT NULL,
    partial INTEGER NOT NULL CHECK (partial IN (0,1)),
    open_time_utc TEXT,
    open_time_madrid TEXT,
    close_time_utc TEXT NOT NULL,
    close_time_madrid TEXT NOT NULL,
    close_date_madrid TEXT NOT NULL,
    pair TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('long','short')),
    quantity_base REAL,
    notional_usdt REAL NOT NULL,
    entry_price REAL NOT NULL,
    exit_price REAL NOT NULL,
    fees_usdt REAL NOT NULL,
    funding_usdt REAL NOT NULL,
    funding_status TEXT NOT NULL,
    pnl_usdt REAL NOT NULL,
    r_multiple REAL,
    reason TEXT,
    entry_order_id TEXT NOT NULL DEFAULT '',
    exit_order_id TEXT NOT NULL DEFAULT '',
    client_order_id TEXT NOT NULL DEFAULT '',
    tpsl_id TEXT NOT NULL DEFAULT '',
    exchange_trade_ids TEXT NOT NULL DEFAULT '',
    signal_close_ms INTEGER,
    raw_json TEXT NOT NULL,
    row_sha256 TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fx_resolutions (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_key TEXT NOT NULL UNIQUE REFERENCES trades(trade_key),
    usd_per_eur REAL NOT NULL CHECK (usd_per_eur > 0),
    rate_date TEXT NOT NULL,
    source TEXT NOT NULL,
    fetched_at_utc TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS trades_no_update BEFORE UPDATE ON trades
BEGIN SELECT RAISE(ABORT, 'trades is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trades_no_delete BEFORE DELETE ON trades
BEGIN SELECT RAISE(ABORT, 'trades is append-only'); END;
-- INSERT OR REPLACE would delete the old row without firing DELETE triggers:
-- an existing key (or seq) silently keeps the original row instead.
CREATE TRIGGER IF NOT EXISTS trades_no_replace BEFORE INSERT ON trades
WHEN EXISTS (SELECT 1 FROM trades WHERE trade_key = NEW.trade_key OR seq = NEW.seq)
BEGIN SELECT RAISE(IGNORE); END;
CREATE TRIGGER IF NOT EXISTS fx_no_replace BEFORE INSERT ON fx_resolutions
WHEN EXISTS (SELECT 1 FROM fx_resolutions WHERE trade_key = NEW.trade_key OR seq = NEW.seq)
BEGIN SELECT RAISE(IGNORE); END;
CREATE TRIGGER IF NOT EXISTS fx_no_update BEFORE UPDATE ON fx_resolutions
BEGIN SELECT RAISE(ABORT, 'fx_resolutions is append-only'); END;
CREATE TRIGGER IF NOT EXISTS fx_no_delete BEFORE DELETE ON fx_resolutions
BEGIN SELECT RAISE(ABORT, 'fx_resolutions is append-only'); END;
CREATE VIEW IF NOT EXISTS trades_eur AS
SELECT t.*,
       CASE WHEN f.trade_key IS NULL THEN 'pending' ELSE 'resolved' END AS fx_status,
       f.usd_per_eur AS fx_usd_per_eur,
       f.rate_date AS fx_rate_date,
       COALESCE(f.source, 'pending') AS fx_source,
       f.fetched_at_utc AS fx_fetched_at_utc,
       CASE WHEN f.trade_key IS NULL THEN NULL ELSE ROUND(t.pnl_usdt / f.usd_per_eur, 2) END AS pnl_eur,
       CASE WHEN f.trade_key IS NULL THEN NULL ELSE ROUND(t.fees_usdt / f.usd_per_eur, 4) END AS fees_eur
FROM trades t LEFT JOIN fx_resolutions f ON f.trade_key = t.trade_key;
"""

EXPORT_COLUMNS = ["seq"] + TRADE_COLUMNS + [
    "fx_status", "fx_usd_per_eur", "fx_rate_date", "fx_source", "fx_fetched_at_utc", "pnl_eur", "fees_eur"]


def journal_path(data_dir, mode):
    """fiscal_journal_<mode>.sqlite (demo now; real once live trading exists)."""
    if mode not in ("paper", "demo", "real"):
        raise ValueError(mode)
    return Path(data_dir) / ("fiscal_journal_%s.sqlite" % mode)


def missing_fields(row):
    """Fiscal fields a demo/real row should carry from Blofin but does not."""
    out = []
    for col in ("entry_order_id", "exit_order_id", "entry_trade_ids", "exchange_trade_ids"):
        if not row.get(col):
            out.append(col)
    for col in ("entry_fee_usdt", "exit_fee_usdt"):
        if row.get(col) is None:
            out.append(col)
    if row.get("fee_source") != "blofin_fills":
        out.append("fee_source")
    if not str(row.get("funding_status") or "").startswith("exchange"):
        out.append("funding_usdt")
    elif row.get("funding_status") != "exchange":
        out.append("funding_window")
    if not row.get("open_time_utc"):
        out.append("open_time_utc")
    elif row.get("open_time_source") != "blofin_fill":
        out.append("open_time_fill")  # Blofin order time or engine clock, not the fill
    if row.get("close_time_source") != "blofin_fill":
        out.append("close_time_fill")
    return out


def _utc_iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _madrid_iso(dt):
    return dt.astimezone(MADRID).isoformat(timespec="seconds")


def _parse_time(value):
    """ISO string (with offset) or epoch ms -> aware datetime, else None."""
    if value is None or value == "":
        return None
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return datetime.fromtimestamp(float(value) / 1000.0, timezone.utc)
        dt = datetime.fromisoformat(str(value))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=MADRID)
        return dt
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _num(value, default=None):
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if out == out else default


class FiscalJournal:
    def __init__(self, path, *, mode="paper", engine_version="", instance="box"):
        if mode not in ("paper", "demo", "real"):
            raise ValueError("mode must be paper, demo or real")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.mode = mode
        self.engine_version = str(engine_version)
        self.instance = str(instance)
        self._lock = threading.Lock()
        with self._connect() as con:
            con.executescript(DDL)
            con.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
            con.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('created_at_utc', ?)",
                        (_utc_iso(datetime.now(timezone.utc)),))
            self._migrate_v2(con)

    @staticmethod
    def _migrate_v2(con):
        """Schema 2: add the fiscal-trace columns. Idempotent; no row is touched."""
        have = {r[1] for r in con.execute("PRAGMA table_info(trades)")}
        for name, kind in V2_COLUMNS:
            if name not in have:
                con.execute("ALTER TABLE trades ADD COLUMN %s %s" % (name, kind))
        con.execute("INSERT INTO meta(key, value) VALUES ('schema_version', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (str(SCHEMA_VERSION),))
        con.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_v2_at_utc', ?)",
                    (_utc_iso(datetime.now(timezone.utc)),))

    @contextlib.contextmanager
    def _connect(self):
        con = sqlite3.connect(str(self.path), timeout=10, isolation_level=None)
        try:
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("PRAGMA synchronous=FULL")
            con.execute("PRAGMA foreign_keys=ON")
            yield con
        finally:
            con.close()

    # ---------------------------------------------------------------- writes
    def build_row(self, closed, pos, *, now=None):
        """Map one engine history row (+ the position dict) to a journal row."""
        closed = dict(closed or {})
        pos = dict(pos or {})
        now = now or datetime.now(timezone.utc)
        exit_audit = closed.get("exitMarket") if isinstance(closed.get("exitMarket"), dict) else {}
        entry_audit = pos.get("entryMarket") if isinstance(pos.get("entryMarket"), dict) else {}
        exchange = self.mode in ("demo", "real")
        close_dt = _parse_time(exit_audit.get("observedAt")) or _parse_time(exit_audit.get("ts"))
        close_src = "blofin_fill" if close_dt and str(exit_audit.get("source", "")).startswith("Blofin") else "engine"
        close_dt = close_dt or now
        open_dt, open_src = _parse_time(pos.get("entryFillTs")), "blofin_fill"
        if open_dt is None:
            open_dt = (_parse_time(entry_audit.get("observedAt")) or _parse_time(pos.get("time"))
                       or _parse_time(entry_audit.get("ts")))
            open_src = ("blofin_order" if open_dt and str(entry_audit.get("source", "")).startswith("Blofin")
                        else "engine" if open_dt else None)
        entry = _num(closed.get("entry"), 0.0)
        notional = _num(closed.get("notional"), 0.0)
        leg = len(pos.get("closedPnls") or []) or 1
        partial = 0 if closed.get("tradePnl") is not None else 1
        key_src = "|".join(str(x) for x in (self.instance, self.mode, closed.get("tradeId"), pos.get("time") or "",
                                             closed.get("pair"), closed.get("side"), leg))
        trade_key = hashlib.sha256(key_src.encode()).hexdigest()[:32]
        row = {
            "trade_key": trade_key,
            "recorded_at_utc": _utc_iso(now),
            "mode": self.mode,
            "fiscal": 1 if self.mode == "real" else 0,
            "engine_version": self.engine_version,
            "instance": self.instance,
            "local_trade_id": closed.get("tradeId"),
            "leg": int(leg),
            "partial": int(partial),
            "open_time_utc": _utc_iso(open_dt) if open_dt else None,
            "open_time_madrid": _madrid_iso(open_dt) if open_dt else None,
            "close_time_utc": _utc_iso(close_dt),
            "close_time_madrid": _madrid_iso(close_dt),
            "close_date_madrid": close_dt.astimezone(MADRID).date().isoformat(),
            "pair": closed.get("pair"),
            "side": closed.get("side"),
            "quantity_base": round(notional / entry, 12) if entry > 0 else None,
            "notional_usdt": notional,
            "entry_price": entry,
            "exit_price": _num(closed.get("exit"), 0.0),
            "fees_usdt": _num(closed.get("fees"), 0.0),
            # Paper does not model funding. Demo/real take it from Blofin's funding history
            # (GET /api/v1/account/funding-fees) when the trader passes fundingUsdt.
            "funding_usdt": _num(closed.get("fundingUsdt"), 0.0),
            # Missing Blofin funding stays flagged: funding_status 'unavailable' and
            # the CSV export leaves funding_usdt empty (the column is NOT NULL in v1).
            "funding_status": ("not_modelled_paper" if self.mode == "paper"
                               else "unavailable" if closed.get("fundingUsdt") is None
                               else "exchange_partial_7d" if closed.get("fundingWindowTruncated")
                               else "exchange"),
            "pnl_usdt": _num(closed.get("pnl"), 0.0),
            "r_multiple": _num(closed.get("rMultiple")),
            "reason": closed.get("reason"),
            "entry_order_id": str(pos.get("entryOrderId") or ""),
            "exit_order_id": str(closed.get("exitOrderId") or ""),
            "client_order_id": str(pos.get("clientOrderId") or ""),
            "tpsl_id": str(pos.get("tpslId") or ""),
            "exchange_trade_ids": str(closed.get("exchangeTradeIds") or ""),
            "signal_close_ms": closed.get("closeMs"),
            "entry_trade_ids": str(pos.get("entryTradeIds") or "") if exchange else None,
            "entry_fee_usdt": _num(closed.get("entryFees")) if exchange else _num(pos.get("entryFee")),
            "exit_fee_usdt": _num(closed.get("exitFees")) if exchange else None,
            "fee_source": (closed.get("feeSource") or None) if exchange else "modelled_paper",
            "open_time_source": open_src,
            "close_time_source": close_src,
            "row_hash_version": 2,
            "raw_json": json.dumps({"history": closed, "position": {k: v for k, v in pos.items() if k != "closedPnls"}},
                                   sort_keys=True, default=str),
        }
        row["missing_fields"] = ",".join(missing_fields(row)) if exchange else ""
        body = json.dumps({k: row[k] for k in TRADE_COLUMNS if k not in ("row_sha256", "recorded_at_utc")},
                          sort_keys=True, default=str)
        row["row_sha256"] = hashlib.sha256(body.encode()).hexdigest()
        return row

    def append(self, row):
        """INSERT OR IGNORE one row. True when a new row was written."""
        cols = ",".join(TRADE_COLUMNS)
        marks = ",".join("?" for _ in TRADE_COLUMNS)
        with self._lock, self._connect() as con:
            cur = con.execute("INSERT OR IGNORE INTO trades (%s) VALUES (%s)" % (cols, marks),
                              [row.get(c) for c in TRADE_COLUMNS])
            return cur.rowcount == 1

    def record_close(self, closed, pos, *, now=None):
        return self.append(self.build_row(closed, pos, now=now))

    def append_fx(self, trade_key, usd_per_eur, rate_date, source, fetched_at=None):
        fetched_at = fetched_at or _utc_iso(datetime.now(timezone.utc))
        with self._lock, self._connect() as con:
            cur = con.execute(
                "INSERT OR IGNORE INTO fx_resolutions (trade_key, usd_per_eur, rate_date, source, fetched_at_utc) "
                "VALUES (?,?,?,?,?)", (trade_key, float(usd_per_eur), rate_date, source, fetched_at))
            return cur.rowcount == 1

    # ----------------------------------------------------------------- reads
    def pending_fx(self):
        with self._connect() as con:
            return con.execute("SELECT trade_key, close_date_madrid FROM trades_eur WHERE fx_status='pending' "
                               "ORDER BY seq").fetchall()

    def counts(self):
        with self._connect() as con:
            total = con.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
            pending = con.execute("SELECT COUNT(*) FROM trades_eur WHERE fx_status='pending'").fetchone()[0]
        return {"rows": total, "fx_pending": pending}

    def rows(self):
        with self._connect() as con:
            con.row_factory = sqlite3.Row
            return [dict(r) for r in con.execute("SELECT * FROM trades_eur ORDER BY seq")]

    def export_csv(self, out_path):
        """Atomic full export of the trades_eur view (regenerated, never edited in place)."""
        out = Path(out_path)
        tmp = out.with_suffix(out.suffix + ".tmp")
        rows = self.rows()
        with open(tmp, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=EXPORT_COLUMNS, extrasaction="ignore")
            w.writeheader()
            for r in rows:
                w.writerow(r)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, out)
        return len(rows)

    # ------------------------------------------------------------- fx rates
    def resolve_pending(self, rates, *, today=None):
        """Append ECB rates for pending rows. ``rates``: {date_iso: usd_per_eur}."""
        today = today or datetime.now(MADRID).date()
        done = 0
        if not rates:
            return 0
        dates = sorted(rates)
        for key, d in self.pending_fx():
            pick = pick_rate(rates, dates, d, today)
            if pick is None:
                continue
            rate_date, value = pick
            if self.append_fx(key, value, rate_date, ECB_SOURCE):
                done += 1
        return done


def pick_rate(rates, dates, trade_date, today):
    if trade_date in rates:
        return trade_date, rates[trade_date]
    try:
        td = date.fromisoformat(trade_date)
    except (TypeError, ValueError):
        return None
    if td >= today:
        return None  # wait for today's fixing
    before = [d for d in dates if d < trade_date]
    if not before:
        return None
    return before[-1], rates[before[-1]]


def parse_ecb_xml(text):
    """{date: USD per EUR} from the ECB eurofxref XML."""
    root = ET.fromstring(text)
    out = {}
    for cube in root.iter():
        if cube.tag.endswith("Cube") and cube.get("time"):
            for c in cube:
                if c.get("currency") == "USD":
                    out[cube.get("time")] = float(c.get("rate"))
    return out


def fetch_ecb_rates(timeout=15):
    import requests
    resp = requests.get(ECB_URL, timeout=timeout)
    resp.raise_for_status()
    rates = parse_ecb_xml(resp.text)
    if not rates:
        raise ValueError("ECB XML without USD rates")
    return rates
