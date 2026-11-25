"""1.8.4: market regime log (record only; never used by trading decisions).

Same definitions as the 21/11/2026 dynamic-limit study
(granmago-analisis-demo-20261121/dinamico, PREREGISTRO.md, week_sim.py):

* ATR14 on 4H candles, Wilder: TR[0] = h-l, ATR[13] = mean(TR[0..13]),
  then ATR[i] = (ATR[i-1]*13 + TR[i]) / 14.
* ratio = ATR14[i] / mean(ATR14[i-179..i])  (180 candles of 4H = 30 days),
  defined from index 193 on (at least 194 candles).
* move = |close[i] - close[i-3]| / ATR14[i]  (12 h measured in ATR).
* For the 4H candle closing at T, i is that candle (open time T - 4H).
* vr = max(ratio BTC, median ratio of the basket), mv = max(move BTC, median
  move of the basket); the basket includes BTC.
* tranquilo: vr < 1.0 and mv < 1.5; brusco: vr >= 1.4 or mv >= 3.0;
  movido otherwise. Fixed thresholds.

Only Blofin public candles the scanner already downloads (4H, limit 300).
Missing data is never filled in: the row says 'insuficiente' with the cause.

Storage: <DATA_DIR>/market_regime.sqlite, separate from the ledger and the
fiscal journal. Tables `regime` (one row per 4H candle and mode, idempotent)
and `signal_events` (signals opened or discarded, joined to the regime by
regime_candle_close_ms). Rows older than RETENTION_DAYS are pruned.
"""
import contextlib
import sqlite3
import statistics
import threading
from datetime import datetime, timezone
from pathlib import Path

H4_MS = 4 * 3600 * 1000
BTC = "BTC-USDT"
# The study's basket: the 28 monitoring pairs minus IOST-USDT (not on Blofin demo).
BASKET = tuple(p + "-USDT" for p in (
    "BTC ETH SOL XRP BNB ADA DOGE AVAX LINK DOT LTC BCH ZEC PEPE 1000BONK NEAR XMR INJ SUI TAO XLM TRX HBAR "
    "ENA WLD AAVE QNT").split())
ATR_N = 14
MEAN_N = 180
MIN_INDEX = 193
MOVE_BARS = 3
VR_CALM, MV_CALM = 1.0, 1.5
VR_ROUGH, MV_ROUGH = 1.4, 3.0
RETENTION_DAYS = 400  # at least one year
OUTCOMES = ("abierta", "descartada")
PUBLISH_WAIT_MS = 30 * 60 * 1000  # wait up to 30 min for every basket candle of the new close
DB_NAME = "market_regime.sqlite"

REGIME_COLUMNS = ["candle_close_utc", "candle_close_ms", "mode", "recorded_at_utc", "btc_ratio",
                  "basket_median_ratio", "vr", "btc_mv", "basket_median_mv", "mv", "regime", "n_symbols",
                  "n_basket", "missing", "engine_version"]
EVENT_COLUMNS = ["id", "at_utc", "mode", "pair", "side", "signal_close_utc", "signal_close_ms", "outcome",
                 "reason", "detail", "regime_candle_close_utc", "regime_candle_close_ms"]
EVENT_EXPORT_COLUMNS = EVENT_COLUMNS + ["regime", "vr", "mv"]

DDL = """
CREATE TABLE IF NOT EXISTS regime (
    candle_close_ms INTEGER NOT NULL,
    mode TEXT NOT NULL,
    candle_close_utc TEXT NOT NULL,
    recorded_at_utc TEXT NOT NULL,
    btc_ratio REAL, basket_median_ratio REAL, vr REAL,
    btc_mv REAL, basket_median_mv REAL, mv REAL,
    regime TEXT NOT NULL CHECK (regime IN ('tranquilo','movido','brusco','insuficiente')),
    n_symbols INTEGER NOT NULL, n_basket INTEGER NOT NULL,
    missing TEXT NOT NULL DEFAULT '',
    engine_version TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (candle_close_ms, mode)
);
CREATE TABLE IF NOT EXISTS signal_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at_utc TEXT NOT NULL,
    mode TEXT NOT NULL,
    pair TEXT NOT NULL,
    side TEXT,
    signal_close_utc TEXT,
    signal_close_ms INTEGER,
    outcome TEXT NOT NULL CHECK (outcome IN ('abierta','descartada')),
    reason TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    regime_candle_close_utc TEXT NOT NULL,
    regime_candle_close_ms INTEGER NOT NULL,
    UNIQUE (mode, pair, side, signal_close_ms, outcome, reason)
);
CREATE VIEW IF NOT EXISTS signal_events_regime AS
SELECT e.*, r.regime AS regime, r.vr AS vr, r.mv AS mv
FROM signal_events e LEFT JOIN regime r
  ON r.candle_close_ms = e.regime_candle_close_ms AND r.mode = e.mode;
"""


def utc_iso(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def candle_close_for(now_ms):
    """Close time of the last closed 4H candle at now_ms."""
    now_ms = int(now_ms)
    return now_ms - now_ms % H4_MS


def atr14(h, l, c):
    """Wilder ATR14, same seed as the study. Entries before index 13 are None."""
    n = len(c)
    tr = [h[0] - l[0]] + [max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])) for i in range(1, n)]
    out = [None] * n
    if n < ATR_N:
        return out
    a = sum(tr[:ATR_N]) / ATR_N
    out[ATR_N - 1] = a
    for i in range(ATR_N, n):
        a = (a * (ATR_N - 1) + tr[i]) / ATR_N
        out[i] = a
    return out


def symbol_metrics(candles, close_ms):
    """(ratio, move, cause). ratio/move None with a cause when not computable.
    candles: dicts with ts (open ms), h, l, c; only candles closed by close_ms are used."""
    want = close_ms - H4_MS
    rows = sorted((x for x in candles or [] if int(x["ts"]) <= want), key=lambda x: int(x["ts"]))
    if not rows or int(rows[-1]["ts"]) != want:
        return None, None, "sin vela %s" % utc_iso(close_ms)
    if rows[-1].get("confirm") is False:  # 1.8.6: Blofin still provisional (confirm "0")
        return None, None, "vela %s sin confirmar" % utc_iso(close_ms)
    i = len(rows) - 1
    if i < MIN_INDEX:
        return None, None, "historial %d<%d velas" % (len(rows), MIN_INDEX + 1)
    h = [float(x["h"]) for x in rows]
    l = [float(x["l"]) for x in rows]
    c = [float(x["c"]) for x in rows]
    a = atr14(h, l, c)
    window = [v for v in a[max(0, i - MEAN_N + 1):i + 1] if v is not None]
    mean = sum(window) / len(window) if window else 0.0
    if not a[i] or mean <= 0:
        return None, None, "ATR nulo"
    return a[i] / mean, abs(c[i] - c[i - MOVE_BARS]) / a[i], None


def classify(vr, mv):
    if vr is None or mv is None:
        return "insuficiente"
    if vr >= VR_ROUGH or mv >= MV_ROUGH:
        return "brusco"
    if vr < VR_CALM and mv < MV_CALM:
        return "tranquilo"
    return "movido"


def compute(candles_by_pair, close_ms, basket=BASKET):
    """Regime row for the 4H candle closing at close_ms (no I/O)."""
    ratios, moves, missing = {}, {}, []
    for pair in basket:
        if pair not in candles_by_pair:
            missing.append("%s:sin datos del escaneo" % pair)
            continue
        r, m, cause = symbol_metrics(candles_by_pair[pair], close_ms)
        if cause:
            missing.append("%s:%s" % (pair, cause))
        else:
            ratios[pair], moves[pair] = r, m
    row = {"candle_close_ms": int(close_ms), "candle_close_utc": utc_iso(close_ms), "btc_ratio": ratios.get(BTC),
           "btc_mv": moves.get(BTC), "basket_median_ratio": None, "basket_median_mv": None, "vr": None, "mv": None,
           "n_symbols": len(ratios), "n_basket": len(basket), "missing": ";".join(missing)}
    if ratios:
        row["basket_median_ratio"] = statistics.median(ratios.values())
        row["basket_median_mv"] = statistics.median(moves.values())
    if BTC in ratios:  # vr/mv need BTC by definition
        row["vr"] = max(row["btc_ratio"], row["basket_median_ratio"])
        row["mv"] = max(row["btc_mv"], row["basket_median_mv"])
    row["regime"] = classify(row["vr"], row["mv"])
    return row


class RegimeStore:
    def __init__(self, data_dir):
        self.path = Path(data_dir) / DB_NAME
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._connect() as con:
            con.executescript(DDL)

    @contextlib.contextmanager
    def _connect(self):
        con = sqlite3.connect(str(self.path), timeout=10, isolation_level=None)
        try:
            con.execute("PRAGMA journal_mode=WAL")
            con.row_factory = sqlite3.Row
            yield con
        finally:
            con.close()

    def has(self, close_ms, mode):
        with self._connect() as con:
            return con.execute("SELECT 1 FROM regime WHERE candle_close_ms=? AND mode=?",
                               (int(close_ms), mode)).fetchone() is not None

    def record(self, row, mode, *, now_ms, engine_version=""):
        """INSERT OR IGNORE: one row per candle and mode (idempotent)."""
        row = dict(row, mode=mode, recorded_at_utc=utc_iso(now_ms), engine_version=engine_version)
        cols = [c for c in REGIME_COLUMNS]
        with self._lock, self._connect() as con:
            cur = con.execute("INSERT OR IGNORE INTO regime (%s) VALUES (%s)" % (",".join(cols), ",".join("?" * len(cols))),
                              [row.get(c) for c in cols])
            return cur.rowcount == 1

    def record_event(self, *, now_ms, mode, pair, side, signal_close_ms, outcome, reason, detail=""):
        if outcome not in OUTCOMES:  # INSERT OR IGNORE would silently skip a CHECK failure
            raise ValueError("outcome %r" % (outcome,))
        rc = candle_close_for(now_ms)
        sc = int(signal_close_ms) if isinstance(signal_close_ms, (int, float)) and signal_close_ms else None
        with self._lock, self._connect() as con:
            cur = con.execute(
                "INSERT OR IGNORE INTO signal_events (at_utc, mode, pair, side, signal_close_utc, signal_close_ms, "
                "outcome, reason, detail, regime_candle_close_utc, regime_candle_close_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (utc_iso(now_ms), mode, pair, side, utc_iso(sc) if sc else None, sc, outcome, reason,
                 str(detail or "")[:300], utc_iso(rc), rc))
            return cur.rowcount == 1

    def latest(self, mode):
        with self._connect() as con:
            r = con.execute("SELECT * FROM regime WHERE mode=? ORDER BY candle_close_ms DESC LIMIT 1", (mode,)).fetchone()
            return dict(r) if r else None

    def regime_rows(self):
        with self._connect() as con:
            return [dict(r) for r in con.execute("SELECT %s FROM regime ORDER BY candle_close_ms, mode"
                                                 % ",".join(REGIME_COLUMNS))]

    def event_rows(self):
        with self._connect() as con:
            return [dict(r) for r in con.execute("SELECT * FROM signal_events_regime ORDER BY id")]

    def prune(self, now_ms, days=RETENTION_DAYS):
        cut = int(now_ms) - days * 86400 * 1000
        with self._lock, self._connect() as con:
            a = con.execute("DELETE FROM regime WHERE candle_close_ms < ?", (cut,)).rowcount
            b = con.execute("DELETE FROM signal_events WHERE regime_candle_close_ms < ?", (cut,)).rowcount
        return a + b

    def export_tables(self):
        """(file name, columns, rows) for fiscal_export's extra CSV files."""
        return [("market_regime.csv", REGIME_COLUMNS, self.regime_rows()),
                ("signal_events.csv", EVENT_EXPORT_COLUMNS, self.event_rows())]
