#!/usr/bin/env python3
"""Backtest the GranMago paper loop over a fixed window.

Reuses scoring, sizing, SL/TP and trailing from granmago/server.py.
Does not change add-on behaviour. The add-on charges no fees, so this
charges none. Candles come from Blofin, the source the add-on requests
before Binance.

The scan is evaluated on each closed 1-minute bar (the live loop scans
about every 90s and checks an open trade about every 20s). Scoring uses
only candles that had closed, plus a synthetic in-progress 4H/1D/1W bar
built from prices up to that minute, which is what the exchange returns
for the forming candle. No future close is used.
"""

import argparse
import json
import math
import os
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "granmago"))
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="granmago-bt-"))
os.environ.setdefault("LOG_LEVEL", "error")

import server  # noqa: E402

server.save_state = lambda: None
server.add_log = lambda *args, **kwargs: None

MADRID = ZoneInfo("Europe/Madrid")
MIN_MS = 60_000
H4_MS = 4 * 60 * MIN_MS
DAY_MS = 24 * 60 * MIN_MS
WEEK_MS = 7 * DAY_MS
# 1970-01-05 was the first Monday. Blofin weeks open Monday 00:00 UTC.
MONDAY_SHIFT_MS = 4 * DAY_MS

START_LOCAL = datetime(2026, 10, 4, 0, 0, tzinfo=MADRID)
INITIAL_BALANCE = 1000.0
RISK_PCT = 2.0
LEVERAGE = 5
CACHE_DIR = Path(os.environ.get("BACKTEST_CACHE", "/tmp/granmago-bt-cache"))
BLOFIN = "https://openapi.blofin.com/api/v1/market/candles"

# Same windows the add-on requests, including the forming candle.
LIMITS = {"4H": 150, "1D": 80, "1W": 60}


def week_open(ts):
    return ts - ((ts - MONDAY_SHIFT_MS) % WEEK_MS)


def bucket_open(ts, duration):
    if duration == WEEK_MS:
        return week_open(ts)
    return ts - (ts % duration)


def parse_blofin(rows):
    out = []
    for c in rows:
        try:
            close = float(c[4])
            if close <= 0:
                continue
            out.append({
                "ts": int(float(c[0])),
                "o": float(c[1]),
                "h": float(c[2]),
                "l": float(c[3]),
                "c": close,
            })
        except (TypeError, ValueError, IndexError):
            continue
    out.sort(key=lambda row: row["ts"])
    dedup = {}
    for row in out:
        dedup[row["ts"]] = row
    return [dedup[k] for k in sorted(dedup)]


def http_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "granmago-backtest"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _covers(rows, start_ms, end_ms):
    if not rows:
        return False
    span = max(MIN_MS, end_ms - start_ms)
    # A page that stopped early must not be reused. Allow one missing bar at each end.
    if rows[0]["ts"] > start_ms + 2 * MIN_MS and rows[0]["ts"] > start_ms + span * 0.02:
        return False
    if rows[-1]["ts"] < end_ms - span * 0.02:
        return False
    return True


def _fetch_page(url):
    last = None
    for attempt in range(8):
        try:
            body = http_json(url)
        except Exception as exc:
            last = exc
            time.sleep(0.6 * (2 ** attempt))
            continue
        if isinstance(body, dict) and str(body.get("code")) == "0" and body.get("data"):
            return body
        last = body
        time.sleep(0.6 * (2 ** attempt))
    raise RuntimeError("Blofin page failed %s (%s)" % (url, last))


def fetch_blofin(inst_id, bar, start_ms, end_ms):
    """Candles with open time in [start_ms, end_ms], oldest first. Blofin pages newest first."""
    cache = CACHE_DIR / ("%s-%s.json" % (inst_id, bar))
    if cache.exists():
        payload = json.loads(cache.read_text(encoding="utf-8"))
        rows = [r for r in payload.get("rows", []) if start_ms <= r["ts"] <= end_ms]
        if _covers(rows, start_ms, end_ms):
            return rows
        cache.unlink()
    found = {}
    cursor = None
    pages = 0
    stalls = 0
    while pages < 500:
        params = {"instId": inst_id, "bar": bar, "limit": "300"}
        if cursor is not None:
            params["after"] = str(cursor)
        url = BLOFIN + "?" + urllib.parse.urlencode(params)
        body = _fetch_page(url)
        pages += 1
        batch = body["data"]
        oldest = None
        for raw in batch:
            try:
                ts = int(float(raw[0]))
            except (TypeError, ValueError, IndexError):
                continue
            oldest = ts if oldest is None else min(oldest, ts)
            if start_ms <= ts <= end_ms:
                found[ts] = raw
        if oldest is None:
            raise RuntimeError("empty page for %s %s" % (inst_id, bar))
        # Reached the start of the requested window. A short final page is normal there.
        if oldest <= start_ms:
            break
        # A short page, or a page that did not move backward, is a stall in the middle
        # of history. Retry the same cursor instead of treating it as the end.
        if len(batch) < 300 or (cursor is not None and oldest >= cursor):
            stalls += 1
            if stalls >= 4:
                raise RuntimeError("pagination stalled for %s %s at %s" % (inst_id, bar, oldest))
            time.sleep(1.0 * stalls)
            continue
        stalls = 0
        cursor = oldest
        time.sleep(0.05)
    rows = parse_blofin(list(found.values()))
    if not _covers(rows, start_ms, end_ms):
        raise RuntimeError("incomplete %s %s: %d bars %s → %s" % (
            inst_id, bar, len(rows),
            madrid(rows[0]["ts"]) if rows else "-",
            madrid(rows[-1]["ts"]) if rows else "-",
        ))
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps({"start": start_ms, "end": end_ms, "rows": rows}), encoding="utf-8")
    return rows


def ohlc_merge(parts, ts):
    if not parts:
        return None
    return {
        "ts": ts,
        "o": parts[0]["o"],
        "h": max(p["h"] for p in parts),
        "l": min(p["l"] for p in parts),
        "c": parts[-1]["c"],
    }


def slice_closed(candles, duration, decision_ms):
    """Candles whose close is at or before decision_ms."""
    if not candles:
        return []
    # open + duration <= decision
    last_open = decision_ms - duration
    lo = 0
    hi = len(candles)
    # rightmost ts <= last_open
    while lo < hi:
        mid = (lo + hi) // 2
        if candles[mid]["ts"] <= last_open:
            lo = mid + 1
        else:
            hi = mid
    return candles[:lo]


def bars_between(candles, start_open, end_open_exclusive):
    if not candles:
        return []
    lo = 0
    hi = len(candles)
    while lo < hi:
        mid = (lo + hi) // 2
        if candles[mid]["ts"] < start_open:
            lo = mid + 1
        else:
            hi = mid
    start = lo
    lo = start
    hi = len(candles)
    while lo < hi:
        mid = (lo + hi) // 2
        if candles[mid]["ts"] < end_open_exclusive:
            lo = mid + 1
        else:
            hi = mid
    return candles[start:lo]


class PairData:
    def __init__(self, pair, m1, h4, d1, w1):
        self.pair = pair
        self.m1 = m1
        self.h4 = h4
        self.d1 = d1
        self.w1 = w1
        self.m1_by_ts = {c["ts"]: c for c in m1}

    def minute(self, open_ts):
        return self.m1_by_ts.get(open_ts)

    def forming(self, decision_ms, duration, source, source_duration):
        minute_open = decision_ms - MIN_MS
        bucket = bucket_open(minute_open, duration)
        if bucket + duration <= decision_ms:
            return None
        parts = []
        closed_src = [
            c for c in slice_closed(source, source_duration, decision_ms)
            if c["ts"] >= bucket
        ]
        parts.extend(closed_src)
        # The source's own forming bar, if it sits inside this higher-TF bucket.
        sub = self.forming_source(decision_ms, source_duration)
        if sub is not None and sub["ts"] >= bucket:
            parts.append(sub)
        return ohlc_merge(parts, bucket)

    def forming_source(self, decision_ms, duration):
        if duration == MIN_MS:
            return None
        if duration == H4_MS:
            minute_open = decision_ms - MIN_MS
            bucket = bucket_open(minute_open, H4_MS)
            if bucket + H4_MS <= decision_ms:
                return None
            parts = bars_between(self.m1, bucket, decision_ms)
            # bars_between uses open ts. Minutes that have closed by decision
            # have open < decision. The minute that closes at decision has
            # open == decision - MIN, which is < decision. Good.
            parts = [c for c in parts if c["ts"] + MIN_MS <= decision_ms]
            return ohlc_merge(parts, bucket)
        if duration == DAY_MS:
            return self.forming(decision_ms, DAY_MS, self.h4, H4_MS)
        if duration == WEEK_MS:
            return self.forming(decision_ms, WEEK_MS, self.h4, H4_MS)
        return None

    def window(self, decision_ms):
        h4 = slice_closed(self.h4, H4_MS, decision_ms)
        forming_h4 = self.forming_source(decision_ms, H4_MS)
        if forming_h4 is not None:
            h4 = h4 + [forming_h4]
        h4 = h4[-LIMITS["4H"]:]

        d1 = slice_closed(self.d1, DAY_MS, decision_ms)
        forming_d = self.forming_source(decision_ms, DAY_MS)
        if forming_d is not None:
            d1 = d1 + [forming_d]
        d1 = d1[-LIMITS["1D"]:]

        w1 = slice_closed(self.w1, WEEK_MS, decision_ms)
        forming_w = self.forming_source(decision_ms, WEEK_MS)
        if forming_w is not None:
            w1 = w1 + [forming_w]
        w1 = w1[-LIMITS["1W"]:]
        return h4, d1, w1


def activation_price(pos):
    entry = pos["entry"]
    move = server.trailing_activation_pct() / 100.0
    if pos["side"] == "long":
        return math.nextafter(entry * (1.0 + move), math.inf)
    return math.nextafter(entry * (1.0 - move), 0.0)


def mark(price):
    """One live monitor tick at `price`. Returns the exit reason, or None."""
    pos = server.STATE["openPosition"]
    if not pos:
        return None
    server.ensure_position_fields(pos)
    side = pos["side"]
    pnl = server.position_pnl(pos, price)
    pos["price"] = price
    if pnl is not None:
        pos["unrealized"] = round(pnl, 2)
    server.update_best_price(pos, price)
    if pos.get("trailingActive"):
        server.tighten_stop(pos)
        if server._stop_hit(side, price, pos.get("sl")):
            server.close_position("trailing", price)
            return "trailing"
    else:
        if server._stop_hit(side, price, pos.get("sl")):
            server.close_position("SL", price)
            return "SL"
        if server._target_hit(side, price, pos.get("tp")):
            server.close_position("TP", price)
            return "TP"
        if server.trailing_should_activate(pos, price):
            server.activate_trailing(pos)
            if server._stop_hit(side, price, pos.get("sl")):
                server.close_position("trailing", price)
                return "trailing"
    return None


def drive(p0, p1, gap, on_close):
    """Move from an already-processed price p0 to p1.

    A gap fill uses p1 itself (the next print). Inside a 1-minute bar the
    high and low did trade, so a stop or target between the two prices fills
    at that level. Levels are visited in price order, so a 1% activation is
    applied before the further fixed target and the target is then dropped.
    """
    pos = server.STATE["openPosition"]
    if not pos:
        return
    if gap or p0 == p1:
        reason = mark(p1)
        if reason:
            on_close(reason, p1)
        return
    pos = server.STATE["openPosition"]
    levels = []
    sl = server._as_price(pos.get("sl"))
    tp = server._as_price(pos.get("tp"))
    if sl is not None:
        levels.append(sl)
    if tp is not None and not pos.get("trailingActive"):
        levels.append(tp)
    if server.trailing_enabled() and not pos.get("trailingActive"):
        levels.append(activation_price(pos))
    mids = []
    for level in levels:
        if p1 > p0 and p0 < level < p1:
            mids.append(level)
        elif p1 < p0 and p1 < level < p0:
            mids.append(level)
    mids.sort(reverse=p1 < p0)
    for px in mids + [p1]:
        if server.STATE["openPosition"] is None:
            return
        reason = mark(px)
        if reason:
            on_close(reason, px)
            return


def path_of(bar):
    o, h, l, c = bar["o"], bar["h"], bar["l"], bar["c"]
    if c >= o:
        return [o, l, h, c]
    return [o, h, l, c]


def new_state(threshold, trailing, activation=1.0, distance=0.5):
    return {
        "running": True,
        "balance": INITIAL_BALANCE,
        "initialBalance": INITIAL_BALANCE,
        "openPosition": None,
        "history": [],
        "log": [],
        "lastScan": [],
        "bestLong": None,
        "bestShort": None,
        "lastTickAt": None,
        "lastScanAt": None,
        "busy": False,
        "settings": {
            "riskPct": RISK_PCT,
            "leverage": LEVERAGE,
            "scanSeconds": 90,
            "trailingEnabled": trailing,
            "trailingActivationPct": float(activation),
            "trailingDistance": float(distance),
            "entryThreshold": threshold,
        },
    }


def pick_best(rows, threshold):
    """Same ranking as scan_all + maybe_open: long wins ties, stable sort."""
    ordered = sorted(rows, key=lambda r: max(r["scoreLong"], r["scoreShort"]), reverse=True)
    longs = [r for r in ordered if r["scoreLong"] >= threshold and r["bias"] == "ALCISTA"]
    shorts = [r for r in ordered if r["scoreShort"] >= threshold and r["bias"] == "BAJISTA"]
    longs.sort(key=lambda r: r["scoreLong"], reverse=True)
    shorts.sort(key=lambda r: r["scoreShort"], reverse=True)
    return (longs[0] if longs else None, shorts[0] if shorts else None)


def _signal(row):
    if row is None:
        return None
    return {
        "pair": row["pair"],
        "price": row["price"],
        "recentLow": row["recentLow"],
        "recentHigh": row["recentHigh"],
        "scoreLong": row["scoreLong"],
        "scoreShort": row["scoreShort"],
    }


class Variant:
    def __init__(self, name, threshold, trailing, activation=1.0, distance=0.5):
        self.name = name
        self.threshold = threshold
        self.trailing = trailing
        self.activation = float(activation)
        self.distance = float(distance)
        self.state = new_state(threshold, trailing, activation, distance)
        self.trades = []
        self.rejected = 0
        self.peak = INITIAL_BALANCE
        self.max_dd = 0.0
        self.max_dd_pct = 0.0
        self.equity_samples = []
        self._open_meta = None
        self._last_sample_bucket = None

    def bind(self):
        server.STATE = self.state

    def note_equity(self, decision_ms, sample):
        pos = self.state["openPosition"]
        unreal = 0.0
        if pos is not None:
            unreal = float(pos.get("unrealized") or 0.0)
        eq = round(self.state["balance"] + unreal, 2)
        if eq > self.peak:
            self.peak = eq
        dd = self.peak - eq
        if dd > self.max_dd:
            self.max_dd = dd
            self.max_dd_pct = dd / self.peak if self.peak else 0.0
        if sample:
            bucket = decision_ms // (12 * 60 * MIN_MS)
            if bucket != self._last_sample_bucket:
                self._last_sample_bucket = bucket
                self.equity_samples.append((decision_ms, eq))

    def on_bar(self, pair, bar, prev_close, decision_ms):
        self.bind()
        pos = self.state["openPosition"]
        if not pos or pos["pair"] != pair:
            return
        closed = {}

        def on_close(reason, price):
            meta = self._open_meta or {}
            hist = self.state["history"][0]
            closed["row"] = {
                "pair": hist["pair"],
                "side": hist["side"],
                "entry_ms": meta.get("entry_ms"),
                "exit_ms": decision_ms,
                "entry": hist["entry"],
                "exit": hist["exit"],
                "reason": reason,
                "pnl": hist["pnl"],
                "score": meta.get("score"),
            }

        if prev_close is None:
            reason = mark(bar["o"])
            if reason:
                on_close(reason, bar["o"])
        else:
            drive(prev_close, bar["o"], gap=True, on_close=on_close)
        if self.state["openPosition"] is not None:
            points = path_of(bar)
            for left, right in zip(points, points[1:]):
                if self.state["openPosition"] is None:
                    break
                drive(left, right, gap=False, on_close=on_close)
        if closed:
            self.trades.append(closed["row"])
            self._open_meta = None
        self.note_equity(decision_ms, sample=False)

    def maybe_open(self, rows, decision_ms, picked=None):
        self.bind()
        if self.state["openPosition"] is not None:
            return
        if picked is None:
            best_l, best_s = pick_best(rows, self.threshold)
        else:
            best_l, best_s = picked
        self.state["bestLong"] = best_l
        self.state["bestShort"] = best_s
        server.maybe_open()
        pos = self.state["openPosition"]
        if pos is None:
            if self.state["bestLong"] or self.state["bestShort"]:
                self.rejected += 1
            return
        side = pos["side"]
        chosen = self.state["bestLong"] if side == "long" else self.state["bestShort"]
        self._open_meta = {
            "entry_ms": decision_ms,
            "score": chosen["scoreLong"] if side == "long" else chosen["scoreShort"],
        }
        self.note_equity(decision_ms, sample=False)


def summarize(variant, end_ms):
    trades = variant.trades
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] < 0]
    flats = [t for t in trades if t["pnl"] == 0]
    reasons = {}
    for t in trades:
        reasons[t["reason"]] = reasons.get(t["reason"], 0) + 1
    holds = []
    for t in trades:
        if t["entry_ms"] is not None and t["exit_ms"] is not None:
            holds.append((t["exit_ms"] - t["entry_ms"]) / 3_600_000)
    pos = variant.state["openPosition"]
    unreal = float(pos["unrealized"]) if pos else 0.0
    balance = variant.state["balance"]
    equity = round(balance + unreal, 2)
    variant.note_equity(end_ms, sample=True)
    if variant.equity_samples and variant.equity_samples[-1][0] != end_ms:
        variant.equity_samples.append((end_ms, equity))
    return {
        "name": variant.name,
        "trailing": variant.trailing,
        "activation": variant.activation if variant.trailing else None,
        "distance": variant.distance if variant.trailing else None,
        "trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "flats": len(flats),
        "win_rate": (len(wins) / len(trades)) if trades else None,
        "pnl": round(balance - INITIAL_BALANCE, 2),
        "pnl_pct": round((balance - INITIAL_BALANCE) / INITIAL_BALANCE * 100, 2),
        "balance": balance,
        "equity": equity,
        "open": None if not pos else {
            "pair": pos["pair"],
            "side": pos["side"],
            "entry": pos["entry"],
            "price": pos.get("price"),
            "unrealized": round(unreal, 2),
            "reason": "abierta",
            "entry_ms": (variant._open_meta or {}).get("entry_ms"),
            "sl": pos.get("sl"),
            "tp": pos.get("tp"),
            "trailing": bool(pos.get("trailingActive")),
        },
        "max_dd": round(variant.max_dd, 2),
        "max_dd_pct": round(variant.max_dd_pct * 100, 2),
        "avg_win": round(sum(t["pnl"] for t in wins) / len(wins), 2) if wins else None,
        "avg_loss": round(sum(t["pnl"] for t in losses) / len(losses), 2) if losses else None,
        "avg_hold_h": round(sum(holds) / len(holds), 2) if holds else None,
        "reasons": reasons,
        "rejected_wide_sl": variant.rejected,
        "equity_samples": variant.equity_samples,
        "trade_list": trades,
    }


def madrid(ms):
    if ms is None:
        return "-"
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone(MADRID).strftime("%Y-%m-%d %H:%M")


def _self_check_exits():
    """The harness must follow server.py: activation removes the fixed target."""
    book = new_state(100, True)
    server.STATE = book
    server.open_from_result({
        "pair": "TEST-USDT",
        "price": 100.0,
        "recentLow": 98.0,
        "recentHigh": 102.0,
        "scoreLong": 100,
        "scoreShort": 0,
    }, "long")
    pos = book["openPosition"]
    if pos is None or pos["tp"] is None:
        raise SystemExit("self-check: entry did not set a target")
    tp = pos["tp"]
    # 1% move activates; the fixed target is further away and must not fill.
    drive(100.0, tp + 1, gap=False, on_close=lambda *a: None)
    if book["openPosition"] is None:
        raise SystemExit("self-check: trailing variant closed at the fixed target")
    if not book["openPosition"].get("trailingActive") or book["openPosition"].get("tp") is not None:
        raise SystemExit("self-check: trailing did not take over")
    sl = book["openPosition"]["sl"]
    drive(tp + 1, sl - 1, gap=False, on_close=lambda *a: None)
    if not book["history"] or book["history"][0]["reason"] != "trailing":
        raise SystemExit("self-check: expected a trailing exit")
    if book["history"][0]["exit"] != sl:
        raise SystemExit("self-check: trailing fill %s != stop %s" % (book["history"][0]["exit"], sl))

    book = new_state(100, False)
    server.STATE = book
    server.open_from_result({
        "pair": "TEST-USDT",
        "price": 100.0,
        "recentLow": 98.0,
        "recentHigh": 102.0,
        "scoreLong": 100,
        "scoreShort": 0,
    }, "long")
    tp = book["openPosition"]["tp"]
    drive(100.0, tp + 1, gap=False, on_close=lambda *a: None)
    if not book["history"] or book["history"][0]["reason"] != "TP":
        raise SystemExit("self-check: fixed target did not fill")
    if book["history"][0]["exit"] != tp:
        raise SystemExit("self-check: target fill was not the target price")
    print("exit self-check ok", flush=True)


def load_market(start_ms, end_ms):
    # 4H window the EMA needs at the first scan: 150 bars ≈ 25 days, plus margin.
    h4_start = start_ms - 40 * DAY_MS
    d1_start = start_ms - 120 * DAY_MS
    w1_start = start_ms - 80 * WEEK_MS
    m1_start = bucket_open(start_ms, H4_MS)
    pairs = {}
    for pair in server.PAIRS:
        print("fetch %s" % pair, flush=True)
        last_err = None
        for attempt in range(4):
            try:
                m1 = fetch_blofin(pair, "1m", m1_start, end_ms)
                h4 = fetch_blofin(pair, "4H", h4_start, end_ms)
                d1 = fetch_blofin(pair, "1D", d1_start, end_ms)
                w1 = fetch_blofin(pair, "1W", w1_start, end_ms)
                last_err = None
                break
            except RuntimeError as exc:
                last_err = exc
                print("  retry %s (%s)" % (pair, exc), flush=True)
                time.sleep(2 * (attempt + 1))
        if last_err is not None:
            raise last_err
        if len(m1) < 1000 or len(h4) < 80 or len(d1) < 60 or len(w1) < 50:
            raise SystemExit("incomplete history for %s m1=%d h4=%d d1=%d w1=%d" % (
                pair, len(m1), len(h4), len(d1), len(w1)))
        pairs[pair] = PairData(pair, m1, h4, d1, w1)
        print("  1m %d (%s → %s) 4H %d 1D %d 1W %d" % (
            len(m1), madrid(m1[0]["ts"]), madrid(m1[-1]["ts"] + MIN_MS), len(h4), len(d1), len(w1)), flush=True)
    return pairs


def run():
    # Alignment self-check against Blofin's Monday week open.
    oct12 = int(datetime(2026, 10, 12, tzinfo=timezone.utc).timestamp() * 1000)
    oct4 = int(datetime(2026, 10, 4, tzinfo=timezone.utc).timestamp() * 1000)
    if week_open(oct12) != oct12:
        raise SystemExit("week alignment failed")
    if datetime.fromtimestamp(week_open(oct4) / 1000, tz=timezone.utc).date().isoformat() != "2026-09-28":
        raise SystemExit("week open for 4 Oct is not 28 Sep")

    now_ms = int(time.time() * 1000)
    # Last fully closed minute. A timestamp that lands on a minute is that minute's close.
    end_ms = now_ms - (now_ms % MIN_MS)
    if end_ms == now_ms:
        end_ms -= MIN_MS
    start_ms = int(START_LOCAL.timestamp() * 1000)
    print("window %s → %s Madrid" % (madrid(start_ms), madrid(end_ms)), flush=True)
    print("source Blofin %s" % BLOFIN, flush=True)
    _self_check_exits()
    # Candle open of the last closed minute. The close itself is end_ms.
    market = load_market(start_ms, end_ms - MIN_MS)

    # Common clock: every closed minute in the window that BTC printed.
    clock = []
    # decision time is the close of the minute, so the first decision is start if
    # start is a minute close; START is 00:00, which is the close of 23:59.
    # Begin at the first minute that closes AFTER the start, and also include
    # a decision exactly at start if that minute has closed.
    first_open = start_ms - (start_ms % MIN_MS)
    if first_open + MIN_MS < start_ms:
        first_open += MIN_MS
    # Walk minute opens whose close is inside (start, end].
    open_ts = first_open
    btc = market["BTC-USDT"]
    last_open = btc.m1[-1]["ts"]
    while open_ts <= last_open:
        decision = open_ts + MIN_MS
        if decision >= start_ms and decision <= end_ms and open_ts in btc.m1_by_ts:
            clock.append(open_ts)
        open_ts += MIN_MS
    print("minutes %d" % len(clock), flush=True)

    variants = [
        Variant("A threshold 100 + trailing", 100, True),
        Variant("B threshold 100 fixed SL/TP", 100, False),
        Variant("C threshold 75 fixed SL/TP", 75, False),
    ]
    scans = 0
    scans_100 = 0
    scans_75 = 0
    price_mismatches = 0
    prev_close = {p: None for p in server.PAIRS}
    started = time.time()
    for n, open_ts in enumerate(clock):
        decision = open_ts + MIN_MS
        bars = {}
        for pair, data in market.items():
            bar = data.minute(open_ts)
            if bar is not None:
                bars[pair] = bar
        for variant in variants:
            pos = variant.state["openPosition"]
            if pos and pos["pair"] in bars:
                variant.on_bar(pos["pair"], bars[pos["pair"]], prev_close[pos["pair"]], decision)
            variant.note_equity(decision, sample=True)
        rows = []
        for pair in server.PAIRS:
            if pair not in bars:
                continue
            data = market[pair]
            c4, cd, cw = data.window(decision)
            row = server.score_market(pair, c4, cd, cw, 0)
            if row is None:
                continue
            if abs(row["price"] - bars[pair]["c"]) > max(1e-6, abs(bars[pair]["c"]) * 1e-8):
                price_mismatches += 1
            rows.append(row)
        scans += 1
        if any(r["scoreLong"] >= 100 or r["scoreShort"] >= 100 for r in rows):
            scans_100 += 1
        if any(r["scoreLong"] >= 75 or r["scoreShort"] >= 75 for r in rows):
            scans_75 += 1
        if n == 0 and rows:
            top = max(rows, key=lambda r: max(r["scoreLong"], r["scoreShort"]))
            print("first scan %s top %s L=%s S=%s price=%s minute=%s" % (
                madrid(decision), top["pair"], top["scoreLong"], top["scoreShort"],
                top["price"], bars[top["pair"]]["c"]), flush=True)
        for variant in variants:
            variant.maybe_open(rows, decision)
        for pair, bar in bars.items():
            prev_close[pair] = bar["c"]
        if n and n % 2000 == 0:
            elapsed = time.time() - started
            print("  %d/%d minutes  %.0fs" % (n, len(clock), elapsed), flush=True)

    # Mark the last price onto any position that is still open so unrealized is current.
    if clock:
        last_open_ts = clock[-1]
        last_decision = last_open_ts + MIN_MS
        for variant in variants:
            pos = variant.state["openPosition"]
            if not pos:
                continue
            bar = market[pos["pair"]].minute(last_open_ts)
            if bar is None:
                continue
            variant.bind()
            pnl = server.position_pnl(pos, bar["c"])
            pos["price"] = bar["c"]
            if pnl is not None:
                pos["unrealized"] = round(pnl, 2)
            variant.note_equity(last_decision, sample=True)

    results = [summarize(v, clock[-1] + MIN_MS if clock else end_ms) for v in variants]
    report = {
        "source": "Blofin openapi.blofin.com candles (add-on primary; Binance not used)",
        "fees": "none (the add-on applies none)",
        "start": madrid(start_ms),
        "end": madrid(clock[-1] + MIN_MS) if clock else madrid(end_ms),
        "minutes": len(clock),
        "scans": scans,
        "scans_with_score_100": scans_100,
        "scans_with_score_75": scans_75,
        "score_price_mismatches": price_mismatches,
        "balance": INITIAL_BALANCE,
        "risk_pct": RISK_PCT,
        "leverage_stored": LEVERAGE,
        "leverage_affects_size": False,
        "results": results,
    }
    out = Path("/tmp/granmago-backtest-report.json")
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print_report(report)
    print("wrote %s" % out, flush=True)


def print_report(report):
    print()
    print("SOURCE %s" % report["source"])
    print("FEES %s" % report["fees"])
    print("WINDOW %s → %s  minutes=%d scans=%d" % (
        report["start"], report["end"], report["minutes"], report["scans"]))
    print("scans with some pair >= 100: %d (%.1f%%)" % (
        report["scans_with_score_100"],
        100.0 * report["scans_with_score_100"] / report["scans"] if report["scans"] else 0,
    ))
    print("scans with some pair >= 75: %d (%.1f%%)" % (
        report["scans_with_score_75"],
        100.0 * report["scans_with_score_75"] / report["scans"] if report["scans"] else 0,
    ))
    for row in report["results"]:
        print()
        print("== %s ==" % row["name"])
        print("trades %d  wins %d  losses %d  flats %d  win_rate %s" % (
            row["trades"], row["wins"], row["losses"], row["flats"],
            ("%.1f%%" % (row["win_rate"] * 100)) if row["win_rate"] is not None else "-",
        ))
        print("pnl %s USDT (%s%%)  balance %s  equity %s" % (
            row["pnl"], row["pnl_pct"], row["balance"], row["equity"]))
        print("max_dd %s USDT (%s%%)  avg_win %s  avg_loss %s  avg_hold_h %s" % (
            row["max_dd"], row["max_dd_pct"], row["avg_win"], row["avg_loss"], row["avg_hold_h"]))
        print("exits %s  wide_sl_rejected %d" % (row["reasons"], row["rejected_wide_sl"]))
        if row["open"]:
            op = row["open"]
            print("OPEN %s %s entry %s @ %s mark %s uPnL %s trailing %s" % (
                op["side"], op["pair"], madrid(op["entry_ms"]), op["entry"], op["price"], op["unrealized"], op["trailing"]))
        if row["name"].startswith("A "):
            print("trades:")
            for t in row["trade_list"]:
                print("  %s %s %s  entry %s  exit %s  %s  pnl %s  (%s → %s)" % (
                    t["pair"], t["side"], t["score"], t["entry"], t["exit"], t["reason"], t["pnl"],
                    madrid(t["entry_ms"]), madrid(t["exit_ms"]),
                ))


ACTIVATIONS = (0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0)
DISTANCES = (0.25, 0.5, 0.75, 1.0, 1.5, 2.0)
OOS_START = datetime(2026, 9, 20, 0, 0, tzinfo=MADRID)


def closed_now_ms():
    now_ms = int(time.time() * 1000)
    end_ms = now_ms - (now_ms % MIN_MS)
    if end_ms == now_ms:
        end_ms -= MIN_MS
    return end_ms


def make_clock(market, start_ms, end_ms):
    clock = []
    first_open = start_ms - (start_ms % MIN_MS)
    if first_open + MIN_MS < start_ms:
        first_open += MIN_MS
    open_ts = first_open
    btc = market["BTC-USDT"]
    last_open = btc.m1[-1]["ts"]
    while open_ts <= last_open:
        decision = open_ts + MIN_MS
        if decision >= start_ms and decision <= end_ms and open_ts in btc.m1_by_ts:
            clock.append(open_ts)
        open_ts += MIN_MS
    return clock


def precompute(market, clock, threshold):
    """Score every minute once. Entry ranking does not depend on the trail."""
    frames = []
    mismatches = 0
    started = time.time()
    for n, open_ts in enumerate(clock):
        decision = open_ts + MIN_MS
        bars = {}
        for pair, data in market.items():
            bar = data.minute(open_ts)
            if bar is not None:
                bars[pair] = bar
        rows = []
        for pair in server.PAIRS:
            if pair not in bars:
                continue
            c4, cd, cw = market[pair].window(decision)
            row = server.score_market(pair, c4, cd, cw, 0)
            if row is None:
                continue
            if abs(row["price"] - bars[pair]["c"]) > max(1e-6, abs(bars[pair]["c"]) * 1e-8):
                mismatches += 1
            rows.append(row)
        best_l, best_s = pick_best(rows, threshold)
        frames.append({
            "decision": decision,
            "bars": bars,
            "best_l": _signal(best_l),
            "best_s": _signal(best_s),
        })
        if n and n % 2000 == 0:
            print("  scored %d/%d  %.0fs" % (n, len(clock), time.time() - started), flush=True)
    hit = sum(1 for f in frames if f["best_l"] or f["best_s"])
    print("scored %d minutes, signals %d, price mismatches %d, %.0fs" % (
        len(frames), hit, mismatches, time.time() - started), flush=True)
    return frames, mismatches


def simulate(frames, variants, market):
    prev_close = {p: None for p in server.PAIRS}
    started = time.time()
    for n, frame in enumerate(frames):
        bars = frame["bars"]
        decision = frame["decision"]
        picked = (frame["best_l"], frame["best_s"])
        for variant in variants:
            pos = variant.state["openPosition"]
            if pos and pos["pair"] in bars:
                variant.on_bar(pos["pair"], bars[pos["pair"]], prev_close[pos["pair"]], decision)
            variant.note_equity(decision, sample=False)
            variant.maybe_open(None, decision, picked=picked)
        for pair, bar in bars.items():
            prev_close[pair] = bar["c"]
        if n and n % 5000 == 0:
            print("    sim %d/%d  %.0fs" % (n, len(frames), time.time() - started), flush=True)
    if frames:
        last = frames[-1]
        for variant in variants:
            pos = variant.state["openPosition"]
            if not pos:
                continue
            bar = last["bars"].get(pos["pair"])
            if bar is None:
                bar = market[pos["pair"]].minute(last["decision"] - MIN_MS)
            if bar is None:
                continue
            variant.bind()
            pnl = server.position_pnl(pos, bar["c"])
            pos["price"] = bar["c"]
            if pnl is not None:
                pos["unrealized"] = round(pnl, 2)
            variant.note_equity(last["decision"], sample=False)
    return [summarize(v, frames[-1]["decision"] if frames else 0) for v in variants]


def grid_specs():
    specs = [("fixed", False, 1.0, 0.5)]
    for act in ACTIVATIONS:
        for dist in DISTANCES:
            specs.append(("a%s_d%s" % (act, dist), True, act, dist))
    return specs


def fresh_variants():
    return [Variant(name, 100, trailing, act, dist) for name, trailing, act, dist in grid_specs()]


def _cell_key(row):
    if not row["trailing"]:
        return ("fixed", None)
    return (row["activation"], row["distance"])


def _neighbours(act, dist):
    ai = ACTIVATIONS.index(act)
    di = DISTANCES.index(dist)
    found = []
    for da, dd in ((-1, 0), (1, 0), (0, -1), (0, 1)):
        ia, id_ = ai + da, di + dd
        if 0 <= ia < len(ACTIVATIONS) and 0 <= id_ < len(DISTANCES):
            found.append((ACTIVATIONS[ia], DISTANCES[id_]))
    return found


def print_matrix(title, rows, field):
    by = {_cell_key(r): r for r in rows}
    print()
    print(title)
    header = "dist\\act".ljust(10) + "".join("%8s" % a for a in ACTIVATIONS)
    print(header)
    for dist in DISTANCES:
        line = ("%s" % dist).ljust(10)
        for act in ACTIVATIONS:
            row = by.get((act, dist))
            line += "%8s" % ("" if row is None else row[field])
        print(line)
    fixed = by.get(("fixed", None))
    if fixed:
        print("fixed %s" % fixed[field])


def run_grid():
    _self_check_exits()
    end_ms = closed_now_ms()
    oos_start = int(OOS_START.timestamp() * 1000)
    is_start = int(START_LOCAL.timestamp() * 1000)
    print("grid %s → %s Madrid, split %s" % (madrid(oos_start), madrid(end_ms), madrid(is_start)), flush=True)
    print("source Blofin. fees none. threshold 100. balance %s risk %s" % (INITIAL_BALANCE, RISK_PCT), flush=True)
    market = load_market(oos_start, end_ms - MIN_MS)
    clock = make_clock(market, oos_start, end_ms)
    if not clock:
        raise SystemExit("no minutes in the grid window")
    actual_start = clock[0] + MIN_MS
    actual_end = clock[-1] + MIN_MS
    print("clock %s → %s (%d minutes)" % (madrid(actual_start), madrid(actual_end), len(clock)), flush=True)
    frames, mismatches = precompute(market, clock, 100)
    windows = {
        "oos": [f for f in frames if f["decision"] <= is_start],
        "in_sample": [f for f in frames if f["decision"] > is_start],
        "combined": frames,
    }
    report = {
        "source": "Blofin openapi.blofin.com candles (add-on primary; Binance not used)",
        "fees": "none (the add-on applies none)",
        "threshold": 100,
        "balance": INITIAL_BALANCE,
        "risk_pct": RISK_PCT,
        "leverage_stored": LEVERAGE,
        "leverage_affects_size": False,
        "activations": list(ACTIVATIONS),
        "distances": list(DISTANCES),
        "price_mismatches": mismatches,
        "windows": {},
    }
    keyed = {}
    for name, subset in windows.items():
        print("simulate %s (%d minutes, %d settings)" % (name, len(subset), len(grid_specs())), flush=True)
        rows = simulate(subset, fresh_variants(), market)
        for row in rows:
            row["pnls"] = [t["pnl"] for t in row["trade_list"]]
            # The full path is large and not needed for the grid table.
            row["trade_list"] = [
                {"pair": t["pair"], "side": t["side"], "pnl": t["pnl"], "reason": t["reason"]}
                for t in row["trade_list"]
            ]
            del row["equity_samples"]
        report["windows"][name] = {
            "start": madrid(subset[0]["decision"]) if subset else None,
            "end": madrid(subset[-1]["decision"]) if subset else None,
            "minutes": len(subset),
            "results": rows,
        }
        keyed[name] = {_cell_key(r): r for r in rows}
        print_matrix("%s realized PnL" % name, rows, "pnl")
        print_matrix("%s end equity" % name, rows, "equity")
        print_matrix("%s max drawdown" % name, rows, "max_dd")
    # Neighbourhood: orthogonal cells on both the earlier window and this one.
    ranked = []
    for act in ACTIVATIONS:
        for dist in DISTANCES:
            ins = keyed["in_sample"].get((act, dist))
            oos = keyed["oos"].get((act, dist))
            both = keyed["combined"].get((act, dist))
            if not ins or not oos or not both:
                continue
            neigh = _neighbours(act, dist)
            def mean_field(window, field):
                vals = []
                for na, nd in neigh:
                    cell = keyed[window].get((na, nd))
                    if cell:
                        vals.append(cell[field])
                return sum(vals) / len(vals) if vals else None
            ranked.append({
                "activation": act,
                "distance": dist,
                "in_pnl": ins["pnl"],
                "oos_pnl": oos["pnl"],
                "all_pnl": both["pnl"],
                "in_equity": ins["equity"],
                "oos_equity": oos["equity"],
                "all_equity": both["equity"],
                "in_dd": ins["max_dd"],
                "oos_dd": oos["max_dd"],
                "all_dd": both["max_dd"],
                "in_trades": ins["trades"],
                "oos_trades": oos["trades"],
                "all_trades": both["trades"],
                "neigh_in_pnl": round(mean_field("in_sample", "pnl"), 2),
                "neigh_oos_pnl": round(mean_field("oos", "pnl"), 2),
                "worst": min(ins["pnl"], oos["pnl"]),
            })
    ranked.sort(key=lambda r: (r["worst"], r["neigh_in_pnl"] + r["neigh_oos_pnl"]), reverse=True)
    fixed_is = keyed["in_sample"][("fixed", None)]
    fixed_oos = keyed["oos"][("fixed", None)]
    fixed_all = keyed["combined"][("fixed", None)]
    print()
    print("BASELINE fixed  IS pnl %s eq %s dd %s trades %s | OOS pnl %s eq %s dd %s trades %s | ALL pnl %s eq %s" % (
        fixed_is["pnl"], fixed_is["equity"], fixed_is["max_dd"], fixed_is["trades"],
        fixed_oos["pnl"], fixed_oos["equity"], fixed_oos["max_dd"], fixed_oos["trades"],
        fixed_all["pnl"], fixed_all["equity"],
    ))
    print("TOP by worse-window realized PnL (neighbour mean beside it)")
    for row in ranked[:15]:
        print("  act %s dist %s  worst %s  IS %s (nbr %s)  OOS %s (nbr %s)  ALL %s  trades %s/%s/%s  dd %s/%s" % (
            row["activation"], row["distance"], row["worst"],
            row["in_pnl"], row["neigh_in_pnl"], row["oos_pnl"], row["neigh_oos_pnl"],
            row["all_pnl"], row["in_trades"], row["oos_trades"], row["all_trades"],
            row["in_dd"], row["oos_dd"],
        ))
    report["ranked"] = ranked
    report["baseline"] = {
        "in_sample": {k: fixed_is[k] for k in ("trades", "win_rate", "pnl", "equity", "max_dd", "reasons", "pnls")},
        "oos": {k: fixed_oos[k] for k in ("trades", "win_rate", "pnl", "equity", "max_dd", "reasons", "pnls")},
        "combined": {k: fixed_all[k] for k in ("trades", "win_rate", "pnl", "equity", "max_dd", "reasons", "pnls")},
    }
    out = Path("/tmp/granmago-grid.json")
    out.write_text(json.dumps(report), encoding="utf-8")
    print("wrote %s" % out, flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Replay GranMago or grid-search the trailing stop.")
    parser.add_argument("--grid", action="store_true", help="Grid activation x distance on two windows plus the combined period.")
    args = parser.parse_args()
    if args.grid:
        run_grid()
    else:
        run()
