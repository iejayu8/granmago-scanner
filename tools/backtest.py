#!/usr/bin/env python3
"""Backtest the GranMago paper loop over a fixed window.

Reuses scoring, sizing, the notional cap, SL/TP, partials and trailing from
granmago/server.py. Fees and slippage follow the settings on the variant
(the add-on defaults are 0). Candles come from Blofin, the source the
add-on requests before Binance.

The scan is evaluated on each closed 1-minute bar (the live loop scans
about every 90s and checks open trades about every 20s). Scoring uses
only candles that had closed, plus a synthetic in-progress 4H/1D/1W bar
built from prices up to that minute, which is what the exchange returns
for the forming candle. No future close is used.

The default run compares max_positions 1, 3 and 5 from 20 Sep 2026
(Europe/Madrid) through the last closed minute, with the live entry
settings: threshold 100, trailing activation 1% and distance 0.25, risk 2%.
"""

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

START_LOCAL = datetime(2026, 9, 20, 0, 0, tzinfo=MADRID)
INITIAL_BALANCE = 1000.0
RISK_PCT = 2.0
LEVERAGE = 5
THRESHOLD = 100
ACTIVATION_PCT = 1.0
TRAIL_DISTANCE = 0.25
MAX_POSITIONS = (1, 3, 5)
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
    # 1m history for half a year is about 900 pages of 300. The cap has to sit above that.
    while pages < 2000:
        params = {"instId": inst_id, "bar": bar, "limit": "300"}
        if cursor is not None:
            params["after"] = str(cursor)
        url = BLOFIN + "?" + urllib.parse.urlencode(params)
        body = _fetch_page(url)
        pages += 1
        if pages == 1 or pages % 100 == 0:
            print("  %s %s page %d" % (inst_id, bar, pages), flush=True)
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


def _just_past(pos, price):
    """A hair beyond `price` in the profit direction, so a >= check fills."""
    if price is None:
        return None
    if pos["side"] == "long":
        return math.nextafter(price, math.inf)
    return math.nextafter(price, 0.0)


def activation_price(pos):
    return _just_past(pos, server.trailing_activation_price(pos))


def partial_level(pos):
    return _just_past(pos, server.partial_target_price(pos))


def _bt_position(pair):
    for pos in server.STATE.get("openPositions") or []:
        if isinstance(pos, dict) and pos.get("pair") == pair:
            return pos
    return None


def mark(pair, price):
    """One live monitor tick for `pair` at `price`. Returns the exit reason, or None.

    Uses server._apply_mark, including partial exits. 'partial' means the
    position is still open.
    """
    pos = _bt_position(pair)
    if not pos:
        return None
    return server._apply_mark(pos, price)


def _forward_levels(pos, p0, p1):
    levels = []
    for level in (
        server._as_price(pos.get("sl")),
        server._as_price(pos.get("tp")),
        partial_level(pos),
        activation_price(pos),
    ):
        if level is None:
            continue
        if p1 > p0 and p0 < level < p1:
            levels.append(level)
        elif p1 < p0 and p1 < level < p0:
            levels.append(level)
    levels.sort(reverse=p1 < p0)
    unique = []
    for level in levels:
        if not unique or abs(unique[-1] - level) > 1e-12:
            unique.append(level)
    return unique


def drive(pair, p0, p1, gap, on_close):
    """Move one pair from an already-processed price p0 to p1.

    A gap fill uses p1 itself (the next print). Inside a 1-minute bar the
    high and low did trade, so a stop or target between the two prices fills
    at that level. Levels are visited in price order, so an activation or a
    partial is applied before a further target. A partial leaves the position
    open and the path continues. Other open pairs are not touched.
    """
    if _bt_position(pair) is None:
        return

    def step(px):
        reason = mark(pair, px)
        if _bt_position(pair) is None:
            on_close(reason, px)
            return True
        return False

    if gap or p0 == p1:
        step(p1)
        return
    for px in _forward_levels(_bt_position(pair), p0, p1) + [p1]:
        if _bt_position(pair) is None:
            return
        if step(px):
            return


def path_of(bar):
    o, h, l, c = bar["o"], bar["h"], bar["l"], bar["c"]
    if c >= o:
        return [o, l, h, c]
    return [o, h, l, c]


def new_state(threshold, trailing, max_positions=1, activation=1.0, distance=0.5):
    return {
        "running": True,
        "balance": INITIAL_BALANCE,
        "initialBalance": INITIAL_BALANCE,
        "openPositions": [],
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
            "trailingActivationPct": activation,
            "trailingActivationR": 0.0,
            "trailingKeepTp": False,
            "trailingDistance": distance,
            "entryThreshold": threshold,
            "maxPositions": max_positions,
            "feePctPerSide": 0.0,
            "slippagePctPerSide": 0.0,
            "takeProfitR": 1.7,
            "partialTpR": 0.0,
            "partialTpFraction": 0.5,
            "partialMoveBreakeven": True,
            "stopMode": "swing",
            "stopAtrPeriod": 14,
            "stopAtrMult": 2.0,
            "pairCooldownMinutes": 0,
            "maxSameDirectionRiskR": 0.0,
            "regimeFilter": "off",
            "regimeAdxMin": 20.0,
            "regimeErMin": 0.2,
        },
    }


class Variant:
    def __init__(self, name, threshold, trailing, max_positions=1, activation=1.0, distance=0.5):
        self.name = name
        self.threshold = threshold
        self.trailing = trailing
        self.max_positions = max_positions
        self.state = new_state(threshold, trailing, max_positions, activation, distance)
        self.trades = []
        self.rejected = 0
        self.capped_entries = 0
        self.peak_positions = 0
        self.peak = INITIAL_BALANCE
        self.max_dd = 0.0
        self.max_dd_pct = 0.0
        self.equity_samples = []
        self._open_meta = {}
        self._last_sample_bucket = None

    def bind(self):
        server.STATE = self.state

    def note_equity(self, decision_ms, sample):
        positions = self.state.get("openPositions") or []
        if len(positions) > self.peak_positions:
            self.peak_positions = len(positions)
        unreal = 0.0
        for pos in positions:
            unreal += float(pos.get("unrealized") or 0.0)
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
        if _bt_position(pair) is None:
            return
        closed = {}

        def on_close(reason, price):
            meta = self._open_meta.pop(pair, {})
            hist = self.state["history"][0] if self.state["history"] else {}
            pnl = hist.get("tradePnl")
            if pnl is None:
                pnl = hist.get("pnl")
            r_mult = hist.get("tradeR")
            if r_mult is None:
                r_mult = hist.get("rMultiple")
            closed["row"] = {
                "pair": hist.get("pair", pair),
                "side": hist.get("side"),
                "entry_ms": meta.get("entry_ms"),
                "exit_ms": decision_ms,
                "entry": hist.get("entry"),
                "exit": hist.get("exit"),
                "reason": reason,
                "pnl": pnl,
                "r": r_mult,
                "score": meta.get("score"),
                "capped": bool(meta.get("capped")),
                "tradeId": hist.get("tradeId"),
            }

        if prev_close is None:
            reason = mark(pair, bar["o"])
            if reason:
                on_close(reason, bar["o"])
        else:
            drive(pair, prev_close, bar["o"], gap=True, on_close=on_close)
        if _bt_position(pair) is not None:
            points = path_of(bar)
            for left, right in zip(points, points[1:]):
                if _bt_position(pair) is None:
                    break
                drive(pair, left, right, gap=False, on_close=on_close)
        if closed:
            self.trades.append(closed["row"])
        self.note_equity(decision_ms, sample=False)

    def maybe_open(self, rows, decision_ms):
        self.bind()
        before = {p["pair"] for p in self.state["openPositions"]}
        tagged = []
        for row in rows:
            copy = dict(row)
            copy["validLong"] = row["scoreLong"] >= self.threshold and row["bias"] == "ALCISTA"
            copy["validShort"] = row["scoreShort"] >= self.threshold and row["bias"] == "BAJISTA"
            tagged.append(copy)
        tagged.sort(key=lambda r: max(r["scoreLong"], r["scoreShort"]), reverse=True)
        best_l = sorted((r for r in tagged if r["validLong"]), key=lambda r: r["scoreLong"], reverse=True)
        best_s = sorted((r for r in tagged if r["validShort"]), key=lambda r: r["scoreShort"], reverse=True)
        self.state["lastScan"] = tagged
        self.state["bestLong"] = best_l[0] if best_l else None
        self.state["bestShort"] = best_s[0] if best_s else None
        server.maybe_open()
        opened = [p for p in self.state["openPositions"] if p["pair"] not in before]
        if not opened and (best_l or best_s) and len(before) < self.max_positions:
            self.rejected += 1
        for pos in opened:
            side = pos["side"]
            chosen = next((r for r in tagged if r["pair"] == pos["pair"]), None)
            score = None
            if chosen is not None:
                score = chosen["scoreLong"] if side == "long" else chosen["scoreShort"]
            self._open_meta[pos["pair"]] = {
                "entry_ms": decision_ms,
                "score": score,
                "capped": bool(pos.get("notionalCapped")),
                "tradeId": pos.get("tradeId"),
            }
            if pos.get("notionalCapped"):
                self.capped_entries += 1
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
    positions = variant.state.get("openPositions") or []
    unreal = 0.0
    for pos in positions:
        unreal += float(pos.get("unrealized") or 0.0)
    balance = variant.state["balance"]
    equity = round(balance + unreal, 2)
    variant.note_equity(end_ms, sample=True)
    if variant.equity_samples and variant.equity_samples[-1][0] != end_ms:
        variant.equity_samples.append((end_ms, equity))
    return {
        "name": variant.name,
        "max_positions": variant.max_positions,
        "peak_positions": variant.peak_positions,
        "capped_entries": variant.capped_entries,
        "trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "flats": len(flats),
        "win_rate": (len(wins) / len(trades)) if trades else None,
        "pnl": round(balance - INITIAL_BALANCE, 2),
        "pnl_pct": round((balance - INITIAL_BALANCE) / INITIAL_BALANCE * 100, 2),
        "balance": balance,
        "equity": equity,
        "open": [
            {
                "pair": pos["pair"],
                "side": pos["side"],
                "entry": pos["entry"],
                "price": pos.get("price"),
                "unrealized": round(float(pos.get("unrealized") or 0.0), 2),
                "reason": "abierta",
                "entry_ms": (variant._open_meta.get(pos["pair"]) or {}).get("entry_ms"),
                "sl": pos.get("sl"),
                "tp": pos.get("tp"),
                "trailing": bool(pos.get("trailingActive")),
                "capped": bool(pos.get("notionalCapped")),
            }
            for pos in positions
        ],
        "max_dd": round(variant.max_dd, 2),
        "max_dd_pct": round(variant.max_dd_pct * 100, 2),
        "avg_win": round(sum(t["pnl"] for t in wins) / len(wins), 2) if wins else None,
        "avg_loss": round(sum(t["pnl"] for t in losses) / len(losses), 2) if losses else None,
        "avg_hold_h": round(sum(holds) / len(holds), 2) if holds else None,
        "reasons": reasons,
        "minutes_with_signal_but_no_new_entry": variant.rejected,
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
    pos = book["openPositions"][0] if book["openPositions"] else None
    if pos is None or pos["tp"] is None:
        raise SystemExit("self-check: entry did not set a target")
    tp = pos["tp"]
    # 1% move activates; the fixed target is further away and must not fill.
    drive("TEST-USDT", 100.0, tp + 1, gap=False, on_close=lambda *a: None)
    if not book["openPositions"]:
        raise SystemExit("self-check: trailing variant closed at the fixed target")
    if not book["openPositions"][0].get("trailingActive") or book["openPositions"][0].get("tp") is not None:
        raise SystemExit("self-check: trailing did not take over")
    sl = book["openPositions"][0]["sl"]
    drive("TEST-USDT", tp + 1, sl - 1, gap=False, on_close=lambda *a: None)
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
    tp = book["openPositions"][0]["tp"]
    drive("TEST-USDT", 100.0, tp + 1, gap=False, on_close=lambda *a: None)
    if not book["history"] or book["history"][0]["reason"] != "TP":
        raise SystemExit("self-check: fixed target did not fill")
    if book["history"][0]["exit"] != tp:
        raise SystemExit("self-check: target fill was not the target price")
    print("exit self-check ok", flush=True)


def _load_one_pair(pair, m1_start, h4_start, d1_start, w1_start, end_ms):
    print("fetch %s" % pair, flush=True)
    last_err = None
    m1 = h4 = d1 = w1 = None
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
        raise RuntimeError("incomplete history for %s m1=%d h4=%d d1=%d w1=%d" % (
            pair, len(m1), len(h4), len(d1), len(w1)))
    print("  1m %d (%s → %s) 4H %d 1D %d 1W %d" % (
        len(m1), madrid(m1[0]["ts"]), madrid(m1[-1]["ts"] + MIN_MS), len(h4), len(d1), len(w1)), flush=True)
    return pair, PairData(pair, m1, h4, d1, w1)


def load_market(start_ms, end_ms):
    # 4H window the EMA needs at the first scan: 150 bars ≈ 25 days, plus margin.
    h4_start = start_ms - 40 * DAY_MS
    d1_start = start_ms - 120 * DAY_MS
    w1_start = start_ms - 80 * WEEK_MS
    m1_start = bucket_open(start_ms, H4_MS)
    # A few pairs at a time. Each 1m history is hundreds of pages; sequential
    # fetches dominated the walk-forward. Cached pairs return immediately.
    from concurrent.futures import ThreadPoolExecutor, as_completed
    pairs = {}
    workers = min(3, len(server.PAIRS))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = [
            pool.submit(_load_one_pair, pair, m1_start, h4_start, d1_start, w1_start, end_ms)
            for pair in server.PAIRS
        ]
        for fut in as_completed(futs):
            pair, data = fut.result()
            pairs[pair] = data
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
        Variant(
            "max_positions %d" % slots,
            THRESHOLD,
            True,
            max_positions=slots,
            activation=ACTIVATION_PCT,
            distance=TRAIL_DISTANCE,
        )
        for slots in MAX_POSITIONS
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
            for pos in list(variant.state["openPositions"]):
                pair = pos.get("pair")
                if pair in bars:
                    variant.on_bar(pair, bars[pair], prev_close[pair], decision)
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
            variant.bind()
            for pos in variant.state["openPositions"]:
                bar = market[pos["pair"]].minute(last_open_ts)
                if bar is None:
                    continue
                pnl = server.position_pnl(pos, bar["c"])
                pos["price"] = bar["c"]
                if pnl is not None:
                    pos["unrealized"] = round(pnl, 2)
            variant.note_equity(last_decision, sample=True)

    results = [summarize(v, clock[-1] + MIN_MS if clock else end_ms) for v in variants]
    report = {
        "source": "Blofin openapi.blofin.com candles (add-on primary; Binance not used)",
        "fees": "none in this comparison (fee_pct_per_side and slippage are 0)",
        "start": madrid(start_ms),
        "end": madrid(clock[-1] + MIN_MS) if clock else madrid(end_ms),
        "minutes": len(clock),
        "scans": scans,
        "scans_with_score_100": scans_100,
        "scans_with_score_75": scans_75,
        "score_price_mismatches": price_mismatches,
        "balance": INITIAL_BALANCE,
        "risk_pct": RISK_PCT,
        "leverage": LEVERAGE,
        "entry_threshold": THRESHOLD,
        "trailing_activation_pct": ACTIVATION_PCT,
        "trailing_distance": TRAIL_DISTANCE,
        "max_positions": list(MAX_POSITIONS),
        "notional_cap": "new entries shrink so total notional stays within balance x leverage; leverage does not multiply size",
        "results": results,
    }
    out = Path("/tmp/granmago-multipos-report.json")
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
        print("max_dd %s USDT (%s%%)  peak_positions %s  capped_entries %s" % (
            row["max_dd"], row["max_dd_pct"], row["peak_positions"], row["capped_entries"]))
        print("avg_win %s  avg_loss %s  avg_hold_h %s" % (
            row["avg_win"], row["avg_loss"], row["avg_hold_h"]))
        print("exits %s  minutes_with_signal_but_no_new_entry %d" % (
            row["reasons"], row["minutes_with_signal_but_no_new_entry"]))
        for op in row["open"]:
            print("OPEN %s %s entry %s @ %s mark %s uPnL %s trailing %s" % (
                op["side"], op["pair"], madrid(op["entry_ms"]), op["entry"], op["price"], op["unrealized"], op["trailing"]))
        print("trades:")
        for t in row["trade_list"]:
            print("  %s %s %s  entry %s  exit %s  %s  pnl %s  (%s → %s)" % (
                t["pair"], t["side"], t["score"], t["entry"], t["exit"], t["reason"], t["pnl"],
                madrid(t["entry_ms"]), madrid(t["exit_ms"]),
            ))


if __name__ == "__main__":
    run()
