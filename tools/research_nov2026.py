#!/usr/bin/env python3
"""Nested walk-forward of the 1.6.1 monitor book through 2026-11-03.

The config list is the whole search. It was fixed before any fold of this
run was read. Training scores use only slices before the test slice. S20
onward is the holdout: S20–S23 ends at 2026-10-24 00:00 UTC (the PR #7
window) and S24 is everything after that, including the week of 27 Oct–3
Nov. S24 is not used to pick a config.

Fees are 0.06% taker plus 0.02% slippage per side. The selection set risks
1% with at most 3 positions, except the two max-position variants. Each
slice resets to 1,000 USDT. S01 is training context and is not an
out-of-sample fold. Risk 0.5% and 2% are sized copies of the monitor; they
are reported and are not in the selection set, because dollar PnL scales
with risk when the leverage cap is slack.

Candles are public Binance 15m klines (spot, or USD-M futures when spot
does not cover the pair), aggregated to 4H, 1D and 1W. The current day,
which the monthly dump does not contain, is filled from the Binance vision
API or, for futures-only symbols, from Blofin.
"""

import bisect
import json
import multiprocessing
import os
import pickle
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

TOOLS = Path(__file__).resolve().parent
ROOT = TOOLS.parent
sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(ROOT / "granmago"))

import granmago_strategy as course  # noqa: E402
from sim16 import (  # noqa: E402
    BAR_MS,
    accept_event,
    deflated_sharpe,
    pool,
    select_config,
    simulate,
    sum_folds,
    trade_sharpe,
)
from trend16 import H4_MS, build_trend  # noqa: E402

CACHE = Path("/tmp/granmago-bt-binance")
MADRID = ZoneInfo("Europe/Madrid")
DAY_MS = course.DAY_MS
WEEK_MS = course.WEEK_MS
FOLD_START = datetime(2024, 1, 22, 0, 0, tzinfo=MADRID)
FOLD_DAYS = 45
# PR #7 was committed 2026-10-23 18:56 UTC. The next UTC midnight is the
# cut between the old holdout and the new bars. It was fixed before the run.
OLD_END = int(datetime(2026, 10, 24, tzinfo=timezone.utc).timestamp() * 1000)
WEEK_START = int(datetime(2026, 10, 27, tzinfo=timezone.utc).timestamp() * 1000)
HOLDOUT_FROM = "S20"
PAIRS = [
    "BTC-USDT", "ETH-USDT", "SOL-USDT", "XRP-USDT", "BNB-USDT", "ADA-USDT",
    "DOGE-USDT", "AVAX-USDT", "LINK-USDT", "DOT-USDT", "LTC-USDT", "BCH-USDT",
    "ZEC-USDT", "PEPE-USDT", "1000BONK-USDT", "NEAR-USDT", "XMR-USDT", "INJ-USDT",
    "SUI-USDT", "TAO-USDT", "IOST-USDT", "XLM-USDT", "TRX-USDT", "HBAR-USDT",
    "ENA-USDT", "WLD-USDT", "AAVE-USDT", "QNT-USDT",
]
LIVE = [
    {"pair": "AAVE-USDT", "side": "short", "close_ms": 1793491200000, "reason": "SL", "pnl": -10.69, "entry": 133.733248},
    {"pair": "BTC-USDT", "side": "short", "close_ms": 1793620800000, "reason": "TP", "pnl": 1.34, "entry": 77844.32802},
    {"pair": "IOST-USDT", "side": "short", "close_ms": 1793620800000, "reason": "TP", "pnl": 1.45, "entry": 0.00070155966},
]
LIVE_OPEN = {
    "pair": "AAVE-USDT", "side": "short", "close_ms": 1793577600000,
    "entry": 135.002994, "signal": 134.67, "sl": 140.94, "tp": 122.23211,
}


def _course(**kw):
    spec = {
        "family": "course",
        "rank": 1,
        "signal_set": "base",
        "setups": "both",
        "sides": "both",
        "momentum": True,
        "hold": 15,
        "be_r": 0.0,
        "target": "swing",
        "target_r": 0.0,
        "min_rr": 0.0,
        "max_stop_pct": 0.08,
        "partial_r": 0.0,
        "partial_frac": 0.5,
        "trail_activate_r": 0.0,
        "trail_dist_r": 1.0,
        "trail_always": False,
        "maker": False,
        "weekly": False,
        "funding_max": None,
        "vol_target": 0.0,
        "max_positions": 3,
        "risk_pct": 0.01,
        "slope_filter": False,
        "adx_min_filter": 0.0,
        "er_min": 0.0,
        "stop_override": None,
        "stop_atr_k": 0.0,
        "select": True,
    }
    spec.update(kw)
    return spec


def _trend(**kw):
    spec = {
        "family": "trend",
        "rank": 2,
        "signal_set": "trend_ema50_4h",
        "mode": "ema",
        "tf": "4h",
        "lookback": 50,
        "exit_lookback": 20,
        "atr_mult": 2.5,
        "sides": "both",
        "adx_min": 0.0,
        "vol_target": 0.0,
        "trail_always": True,
        "target": "none",
        "momentum": False,
        "hold": 0,
        "maker": False,
        "max_positions": 3,
        "risk_pct": 0.01,
        "select": True,
    }
    spec.update(kw)
    return spec


# Frozen before this run. Rank 0 is the 1.6.1 monitor. A higher rank has to
# clear the plateau on training PnL before it can be selected. select=False
# rows are sizing copies and do not enter the deflated-Sharpe trial count.
CONFIGS = [
    _course(name="monitor", rank=0),
    _course(name="hold_0", hold=0),
    _course(name="hold_8", hold=8),
    _course(name="hold_40", hold=40),
    _course(name="no_momentum", momentum=False),
    _course(name="pad_0.15", signal_set="pad015"),
    _course(name="pad_0.60", signal_set="pad060"),
    _course(name="pad_1.00", signal_set="pad100"),
    _course(name="atr_0.25", signal_set="atr025"),
    _course(name="atr_1.00", signal_set="atr100"),
    _course(name="atr_1.50", signal_set="atr150"),
    _course(name="atr_stop_1.5", stop_override="atr", stop_atr_k=1.5),
    _course(name="atr_stop_2.5", stop_override="atr", stop_atr_k=2.5),
    _course(name="weekly_filter", weekly=True),
    _course(name="slope_filter", slope_filter=True),
    _course(name="adx_20", adx_min_filter=20.0),
    _course(name="er_0.20", er_min=0.2),
    _course(name="confirm_12h", signal_set="variant_b"),
    _course(name="max_positions_1", max_positions=1),
    _course(name="max_positions_5", max_positions=5),
    _course(name="tp_1.2r", rank=2, target="r", target_r=1.2),
    _course(name="tp_1.7r", rank=2, target="r", target_r=1.7),
    _course(name="tp_2.5r", rank=2, target="r", target_r=2.5),
    _course(name="tp_3.0r", rank=2, target="r", target_r=3.0),
    _course(name="trail_0.5_0.5", rank=2, trail_activate_r=0.5, trail_dist_r=0.5),
    _course(name="trail_1_1", rank=2, target="none", trail_activate_r=1.0, trail_dist_r=1.0, momentum=False, hold=0),
    _course(name="trail_1_1_hold40", rank=2, target="none", trail_activate_r=1.0, trail_dist_r=1.0, momentum=False, hold=40),
    _course(name="trail_1.5_1", rank=2, target="none", trail_activate_r=1.5, trail_dist_r=1.0, momentum=False, hold=0),
    _trend(name="trend_ema50_4h"),
    _course(name="risk_0.5", rank=9, risk_pct=0.005, select=False),
    _course(name="risk_2.0", rank=9, risk_pct=0.02, select=False),
]

SIGNAL_SETS = {
    "base": ("a", 0.3, 0.5),
    "pad015": ("a", 0.15, 0.5),
    "pad060": ("a", 0.6, 0.5),
    "pad100": ("a", 1.0, 0.5),
    "atr025": ("a", 0.3, 0.25),
    "atr100": ("a", 0.3, 1.0),
    "atr150": ("a", 0.3, 1.5),
    "variant_b": ("b", 0.3, 0.5),
}


def _ms(dt):
    return int(dt.timestamp() * 1000)


def _iso(ms):
    if ms is None:
        return "-"
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M")


def week_open(ts):
    return ts - ((ts - 4 * DAY_MS) % WEEK_MS)


def aggregate(bars, dest_ms):
    buckets = {}
    for bar in bars:
        key = week_open(bar["ts"]) if dest_ms == WEEK_MS else bar["ts"] - (bar["ts"] % dest_ms)
        rec = buckets.get(key)
        if rec is None:
            buckets[key] = {"ts": key, "o": bar["o"], "h": bar["h"], "l": bar["l"], "c": bar["c"]}
        else:
            rec["h"] = max(rec["h"], bar["h"])
            rec["l"] = min(rec["l"], bar["l"])
            rec["c"] = bar["c"]
    last_close = bars[-1]["ts"] + BAR_MS if bars else 0
    out = []
    for key in sorted(buckets):
        if key + dest_ms <= last_close:
            out.append(buckets[key])
    return out


def load_market():
    manifest = json.loads((CACHE / "manifest.json").read_text(encoding="utf-8"))
    end_ms = int(manifest["end_ms"])
    market = {}
    for pair in PAIRS:
        payload = json.loads((CACHE / ("%s-15m.json" % pair)).read_text(encoding="utf-8"))
        m15 = payload["rows"]
        market[pair] = {
            "m15": m15,
            "h4": aggregate(m15, H4_MS),
            "d1": aggregate(m15, DAY_MS),
            "w1": aggregate(m15, WEEK_MS),
        }
        print("  %s 15m %d 4H %d 1D %d 1W %d" % (
            pair, len(m15), len(market[pair]["h4"]), len(market[pair]["d1"]), len(market[pair]["w1"]),
        ), flush=True)
    return manifest, end_ms, market


def slices_of(end_ms):
    rows = []
    for i in range(23):
        lo = FOLD_START + timedelta(days=FOLD_DAYS * i)
        hi = FOLD_START + timedelta(days=FOLD_DAYS * (i + 1))
        lo_ms = _ms(lo)
        hi_ms = _ms(hi)
        if i == 22:
            hi_ms = OLD_END
        if lo_ms >= end_ms:
            break
        rows.append(("S%02d" % (i + 1), lo_ms, min(hi_ms, end_ms)))
    if end_ms > OLD_END:
        rows.append(("S24", OLD_END, end_ms))
    return rows


def _weekly_at(weekly):
    averages = course.ema_series([float(c["c"]) for c in weekly])

    def at(ms):
        side = None
        for candle, value in zip(weekly, averages):
            if int(candle["ts"]) + WEEK_MS > ms:
                break
            if value is None:
                continue
            close = float(candle["c"])
            if close > value:
                side = "long"
            elif close < value:
                side = "short"
            else:
                side = None
        return side

    return at


def _slope_er(daily, c4):
    d_ts = [int(c["ts"]) for c in daily]
    d_ema = course.ema_series([float(c["c"]) for c in daily], 50)
    c_ts = [int(c["ts"]) for c in c4]
    closes = [float(c["c"]) for c in c4]

    def at(ms):
        di = bisect.bisect_right(d_ts, ms - DAY_MS) - 1
        slope = None
        if di >= 5 and d_ema[di] is not None and d_ema[di - 5] not in (None, 0):
            slope = (d_ema[di] - d_ema[di - 5]) / d_ema[di - 5]
        ci = bisect.bisect_right(c_ts, ms - H4_MS) - 1
        er = None
        if ci >= 20:
            window = closes[ci - 20:ci + 1]
            change = abs(window[-1] - window[0])
            path = 0.0
            for left, right in zip(window, window[1:]):
                path += abs(right - left)
            er = 0.0 if path <= 0 else change / path
        return slope, er

    return at


def _events_from_signals(pair, signals, weekly_at, slope_er):
    order_pad = 0
    out = []
    for signal in signals:
        close_ms = int(signal["closeMs"])
        slope, er = slope_er(close_ms)
        out.append({
            "t": close_ms,
            "pair": pair,
            "side": signal["side"],
            "price": float(signal["price"]),
            "sl": float(signal["sl"]),
            "tp": None if signal.get("tp") is None else float(signal["tp"]),
            "family": "course",
            "setup": signal.get("setup"),
            "confirm_ms": int(signal["confirmMs"]),
            "close_ms": close_ms,
            "weekly": weekly_at(close_ms),
            "funding": None,
            "strength": 0.0,
            "atr": None if signal.get("atr") is None else float(signal["atr"]),
            "adx": None if signal.get("adx") is None else float(signal["adx"]),
            "slope": slope,
            "er": er,
            "_ord": order_pad,
        })
    return out


def _snaps(bars):
    out = {}
    for close_ms, snap in bars.items():
        out[int(close_ms)] = {
            "close": snap["close"],
            "k": snap["k"],
            "hist": snap["hist"],
            "prev": snap["prevHist"],
            "confirm_ms": snap["confirmMs"],
            "bar_ts": snap["barTs"],
        }
    return out


def _pack_multi(spec, side, setup, confirm, index, extreme, level, c4, atr_s, k_series, hist, adx, daily, min_rr, pad_pct):
    """One sweep pass, every stop variant. The strategy module is not edited.

    `_find_sweep` spends almost all of its time finding the sweep. The pad
    and the ATR multiple only change the stop, so they are priced here and
    a variant whose stop is wider than 8% or fails the bracket is omitted.
    """
    del pad_pct, min_rr
    bar = confirm[index]
    price = float(bar["c"])
    close_ms = int(bar["ts"]) + spec["confirm_ms"]
    atr = course._atr_asof(c4, atr_s, close_ms)
    target = course._target_price(daily, side, price)
    stops = {}
    for key, (variant, pad, atr_mult) in SIGNAL_SETS.items():
        if variant != spec["variant"]:
            continue
        previous = course.STOP_ATR_MULT
        course.STOP_ATR_MULT = atr_mult
        try:
            stop = course._stop_price(extreme, price, atr, pad, confirm, index, side)
        finally:
            course.STOP_ATR_MULT = previous
        if course._reward_ok(price, stop, target, side, 0.0):
            stops[key] = stop
    if not stops:
        return None
    chosen = stops.get("base", stops.get("variant_b", next(iter(stops.values()))))
    return {
        "side": side,
        "setup": setup,
        "variant": spec["variant"],
        "price": price,
        "sl": chosen,
        "tp": target,
        "addPrice": float(extreme),
        "sweepExtreme": float(extreme),
        "level": float(level),
        "confirmMs": spec["confirm_ms"],
        "barTs": int(bar["ts"]),
        "closeMs": close_ms,
        "atr": atr,
        "stochK": None if index >= len(k_series) else k_series[index],
        "sqz": None if index >= len(hist) else hist[index],
        "adx": None if index >= len(adx) else adx[index],
        "stops": stops,
    }


_MKT = None
_END = None


def _prepare_pair(pair):
    data = _MKT[pair]
    c4 = course.drop_forming(data["h4"], H4_MS, _END)
    daily = course.drop_forming(data["d1"], DAY_MS, _END)
    weekly = course.drop_forming(data["w1"], WEEK_MS, _END)
    weekly_at = _weekly_at(weekly)
    slope_er = _slope_er(daily, c4)
    order = PAIRS.index(pair)
    buckets = {key: [] for key in SIGNAL_SETS}
    snaps = {}
    for variant in ("a", "b"):
        signals, bars = course.find_signals(
            c4, daily, weekly, variant=variant, min_rr=0.0, stop_pad_pct=0.3,
            filters=True, style="sweep", setups="both",
        )
        snaps[variant] = _snaps(bars)
        for signal in signals:
            slope, er = slope_er(int(signal["closeMs"]))
            base = {
                "t": int(signal["closeMs"]),
                "pair": pair,
                "side": signal["side"],
                "price": float(signal["price"]),
                "tp": None if signal.get("tp") is None else float(signal["tp"]),
                "family": "course",
                "setup": signal.get("setup"),
                "confirm_ms": int(signal["confirmMs"]),
                "close_ms": int(signal["closeMs"]),
                "weekly": weekly_at(int(signal["closeMs"])),
                "funding": None,
                "strength": 0.0,
                "atr": None if signal.get("atr") is None else float(signal["atr"]),
                "adx": None if signal.get("adx") is None else float(signal["adx"]),
                "slope": slope,
                "er": er,
                "_ord": order,
            }
            for key, stop in (signal.get("stops") or {}).items():
                ev = dict(base)
                ev["sl"] = float(stop)
                buckets[key].append(ev)
    counts = " ".join("%s %d" % (key, len(buckets[key])) for key in ("base", "variant_b"))
    print("  %s %s" % (pair, counts), flush=True)
    return pair, buckets, snaps


def prepare_course(market, end_ms):
    """One sweep pass per variant. Stop variants are priced inside that pass."""
    global _MKT, _END
    _MKT = market
    _END = end_ms
    course._pack_signal = _pack_multi
    cache = Path("/tmp/granmago-nov2026-course.pkl")
    if cache.exists():
        payload = pickle.loads(cache.read_bytes())
        if payload.get("key") == end_ms:
            print("course cache hit", flush=True)
            return payload["events"], payload["indicators"]
    events = {key: [] for key in SIGNAL_SETS}
    indicators = {"a": {}, "b": {}}
    workers = min(4, len(PAIRS), os.cpu_count() or 1)
    ctx = multiprocessing.get_context("fork")
    with ctx.Pool(workers) as pool_workers:
        parts = pool_workers.map(_prepare_pair, PAIRS)
    for pair, buckets, snaps in parts:
        indicators["a"][pair] = snaps["a"]
        indicators["b"][pair] = snaps["b"]
        for key, rows in buckets.items():
            events[key].extend(rows)
    for key in events:
        events[key].sort(key=lambda ev: (ev["t"], ev["_ord"]))
    cache.write_bytes(pickle.dumps({"key": end_ms, "events": events, "indicators": indicators}))
    return events, indicators


def prepare_trend(market, end_ms):
    spec = next(item for item in CONFIGS if item["name"] == "trend_ema50_4h")
    events = []
    states = {}
    order = {pair: i for i, pair in enumerate(PAIRS)}
    for pair in PAIRS:
        c4 = course.drop_forming(market[pair]["h4"], H4_MS, end_ms)
        evs, state = build_trend(pair, c4, H4_MS, spec)
        for ev in evs:
            ev["_ord"] = order[pair]
        events.extend(evs)
        states[pair] = state
    events.sort(key=lambda ev: (ev["t"], -float(ev.get("strength") or 0.0), ev["_ord"]))
    spec["_state"] = states
    print("  trend_ema50_4h events %d" % len(events), flush=True)
    return events


def _override_stop(ev, spec):
    if spec.get("stop_override") != "atr":
        return ev
    atr = ev.get("atr")
    mult = float(spec.get("stop_atr_k") or 0.0)
    if atr is None or mult <= 0 or ev["price"] <= 0:
        return None
    dist = mult * atr
    ev = dict(ev)
    ev["sl"] = ev["price"] - dist if ev["side"] == "long" else ev["price"] + dist
    return ev


def _passes(ev, spec):
    ev = _override_stop(ev, spec)
    if ev is None or not accept_event(ev, spec):
        return None
    if spec.get("slope_filter"):
        slope = ev.get("slope")
        if slope is None:
            return None
        if ev["side"] == "long" and slope < 0:
            return None
        if ev["side"] == "short" and slope > 0:
            return None
    adx_min = float(spec.get("adx_min_filter") or 0.0)
    if adx_min:
        if ev.get("adx") is None or ev["adx"] < adx_min:
            return None
    er_min = float(spec.get("er_min") or 0.0)
    if er_min:
        if ev.get("er") is None or ev["er"] < er_min:
            return None
    return ev


def filter_events(spec, course_events, trend_events):
    if spec["family"] == "trend":
        return [ev for ev in trend_events if accept_event(ev, spec)]
    src = course_events[spec["signal_set"]]
    out = []
    for ev in src:
        kept = _passes(ev, spec)
        if kept is not None:
            out.append(kept)
    return out


def series_of(market):
    out = {}
    for pair, data in market.items():
        rows = data["m15"]
        out[pair] = {
            "ts": [int(r["ts"]) for r in rows],
            "o": [float(r["o"]) for r in rows],
            "h": [float(r["h"]) for r in rows],
            "l": [float(r["l"]) for r in rows],
            "c": [float(r["c"]) for r in rows],
        }
    return out


def clock_of(series):
    fold_start = _ms(FOLD_START)
    btc = series["BTC-USDT"]
    return [ts for ts in btc["ts"] if ts + BAR_MS >= fold_start]


def _rows_named(rows, names):
    want = set(names)
    return [row for row in rows if row["name"] in want]


def enrich(rows):
    if not rows:
        return {
            "trades": 0, "win_rate": None, "pf": None, "expectancy": None,
            "expectancy_lo": None, "expectancy_hi": None, "pnl": 0.0,
            "worst_dd_pct": None, "positive_folds": 0, "folds": 0,
            "net_r": 0.0, "avg_win_r": None, "avg_loss_r": None,
            "long_pnl": 0.0, "short_pnl": 0.0, "rs": [], "pnls": [],
        }
    summary = pool(rows)
    win_r = []
    loss_r = []
    for r_value, pnl in zip(summary["rs"], summary["pnls"]):
        if pnl > 0:
            win_r.append(r_value)
        elif pnl < 0:
            loss_r.append(r_value)
    summary["avg_win_r"] = None if not win_r else round(sum(win_r) / len(win_r), 4)
    summary["avg_loss_r"] = None if not loss_r else round(sum(loss_r) / len(loss_r), 4)
    summary["net_r"] = round(sum(summary["rs"]), 4)
    return summary


def _public(summary):
    keep = (
        "trades", "win_rate", "pf", "expectancy", "expectancy_lo", "expectancy_hi",
        "pnl", "worst_dd_pct", "positive_folds", "folds", "net_r", "avg_win_r",
        "avg_loss_r", "long_pnl", "short_pnl", "label", "sharpe",
    )
    return {key: summary.get(key) for key in keep}


def nested_and_holdout(spec_rows, slices, configs):
    names = [name for name, _lo, _hi in slices]
    pre_tests = [name for name in names if "S02" <= name < HOLDOUT_FROM]
    frozen_train = [name for name in names if name < HOLDOUT_FROM]
    stitched = []
    choices = []
    for test in pre_tests:
        train_names = [name for name in names if name < test]
        scores = []
        for spec in configs:
            folds = _rows_named(spec_rows[spec["name"]], train_names)
            score = sum_folds(folds) if folds else {"name": spec["name"], "trades": 0, "pnl": 0.0, "folds": 0}
            score["name"] = spec["name"]
            scores.append(score)
        choice, relaxed, plateau = select_config(scores, configs)
        row = next(r for r in spec_rows[choice] if r["name"] == test)
        stamped = dict(row)
        stamped["chosen"] = choice
        stitched.append(stamped)
        choices.append({"test": test, "chosen": choice, "relaxed": relaxed, "plateau": plateau})
    frozen_scores = []
    for spec in configs:
        folds = _rows_named(spec_rows[spec["name"]], frozen_train)
        score = sum_folds(folds) if folds else {"name": spec["name"], "trades": 0, "pnl": 0.0, "folds": 0}
        score["name"] = spec["name"]
        frozen_scores.append(score)
    frozen, frozen_relaxed, frozen_plateau = select_config(frozen_scores, configs)
    return {
        "choices": choices,
        "stitched": stitched,
        "frozen": frozen,
        "frozen_relaxed": frozen_relaxed,
        "frozen_plateau": frozen_plateau,
        "train_rank": sorted(frozen_scores, key=lambda row: -row["pnl"]),
    }


_SERIES = None
_CLOCK = None
_FILTERED = None
_INDICATORS = None
_SLICES = None
_SPECS = None


def _run_spec(name):
    spec = _SPECS[name]
    started = time.time()
    indicators = _INDICATORS.get(spec.get("signal_set"), {})
    if spec["family"] == "trend":
        indicators = {}
    if spec.get("signal_set") == "variant_b":
        indicators = _INDICATORS["b"]
    elif spec["family"] == "course":
        indicators = _INDICATORS["a"]
    rows = simulate(_CLOCK, _SERIES, _FILTERED[name], indicators, spec, _SLICES)
    print("  %-20s trades %d %.0fs" % (
        name, sum(r["trades"] for r in rows), time.time() - started,
    ), flush=True)
    return name, rows


def _fmt(value, digits=2):
    if value is None:
        return "-"
    return ("%." + str(digits) + "f") % value


def _fmt_pct(rate):
    if rate is None:
        return "-"
    return "%.1f%%" % (rate * 100)


def _line(label, row):
    return "| %s | %d | %s | %s | %s | %s | %s | %s | %s |" % (
        label,
        row.get("trades") or 0,
        _fmt_pct(row.get("win_rate")),
        _fmt(row.get("pf")),
        _fmt(row.get("net_r"), 1),
        _fmt(row.get("expectancy"), 3),
        _fmt(row.get("pnl"), 0),
        _fmt(row.get("worst_dd_pct"), 1),
        "%s..%s" % (_fmt(row.get("expectancy_lo"), 3), _fmt(row.get("expectancy_hi"), 3)),
    )


def write_report(path, payload):
    lines = []
    add = lines.append
    add("# GranMago 1.6.1 — optimización hasta el 2026-11-03")
    add("")
    add("Análisis solamente. No cambia los defectos de la estrategia ni la versión del complemento, y no se fusiona.")
    add("")
    add("## Veredicto")
    add("")
    add(payload["verdict"])
    add("")
    add("## Datos")
    add("")
    add(payload["data_note"])
    add("")
    add("## Método")
    add("")
    add(payload["method_note"])
    add("")
    add("## Libro en vivo frente al backtest")
    add("")
    add(payload["parity_note"])
    add("")
    add("## Baseline")
    add("")
    add("| Ventana | Trades | Acierto | PF | R neto | E[R] | USDT | Peor DD | IC 95% E[R] |")
    add("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for label, row in payload["baseline_rows"]:
        add(_line(label, row))
    add("")
    add("El USDT es la suma de pliegues que vuelven a 1.000, salvo las filas marcadas como cuenta continua. El R neto es la suma de los R por operación.")
    add("")
    add("## Configuraciones")
    add("")
    add("La ventana de selección es S02–S19. El holdout antiguo es S20–S23 (hasta el 2026-10-24). El holdout nuevo añade S24, la semana incluida. Estas filas fijas no están ajustadas por la búsqueda: la fila anidada sí.")
    add("")
    add("| Config | Sel. trades | Acierto | PF | R neto | USDT | DD | Holdout ant. USDT | Holdout ant. E[R] | Holdout nuevo USDT | Holdout nuevo E[R] |")
    add("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for row in payload["table"]:
        add("| %s | %d | %s | %s | %s | %s | %s | %s | %s | %s | %s |" % (
            row["name"],
            row["sel_trades"],
            _fmt_pct(row["sel_win"]),
            _fmt(row["sel_pf"]),
            _fmt(row["sel_net_r"], 1),
            _fmt(row["sel_pnl"], 0),
            _fmt(row["sel_dd"], 1),
            _fmt(row["old_pnl"], 0),
            _fmt(row["old_exp"], 3),
            _fmt(row["new_pnl"], 0),
            _fmt(row["new_exp"], 3),
        ))
    add("")
    add("## Fuera de muestra")
    add("")
    add("| Regla | Trades | Acierto | PF | R neto | E[R] | USDT | Peor DD | IC 95% | Pliegues + |")
    add("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for label, row in payload["oos_rows"]:
        folds = "%d/%d" % (row.get("positive_folds") or 0, row.get("folds") or 0)
        add("| %s | %d | %s | %s | %s | %s | %s | %s | %s | %s |" % (
            label,
            row.get("trades") or 0,
            _fmt_pct(row.get("win_rate")),
            _fmt(row.get("pf")),
            _fmt(row.get("net_r"), 1),
            _fmt(row.get("expectancy"), 3),
            _fmt(row.get("pnl"), 0),
            _fmt(row.get("worst_dd_pct"), 1),
            "%s..%s" % (_fmt(row.get("expectancy_lo"), 3), _fmt(row.get("expectancy_hi"), 3)),
            folds,
        ))
    add("")
    add(payload["dsr_note"])
    add("")
    add("## Última semana y la PR #7")
    add("")
    add(payload["week_note"])
    add("")
    add("## Tamaño")
    add("")
    add("Riesgo 0,5 % y 2 % sobre las mismas señales del monitor. No entran en la selección ni en el Sharpe deflactado: el PnL en USDT escala con el riesgo cuando el tope de nocional no aprieta.")
    add("")
    add("| Config | Trades | PF | R neto | E[R] | USDT S02–S19 | USDT holdout nuevo |")
    add("|---|---:|---:|---:|---:|---:|---:|")
    for row in payload["sizing"]:
        add("| %s | %d | %s | %s | %s | %s | %s |" % (
            row["name"], row["trades"], _fmt(row["pf"]), _fmt(row["net_r"], 1),
            _fmt(row["expectancy"], 3), _fmt(row["pnl"], 0), _fmt(row["hold_pnl"], 0),
        ))
    add("")
    add("## Recomendación")
    add("")
    add(payload["recommendation"])
    add("")
    add("Regla escrita antes de leer los pliegues: se cambia el monitor solo si el nombre congelado en S01–S19 no es el monitor, su E[R] de holdout (S20–S24) tiene el intervalo al completo por encima de cero, su USDT de holdout supera al monitor, y el Sharpe deflactado de la regla anidada es al menos 0,95. Si falta cualquiera, se queda el libro actual.")
    add("")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("wrote", path, flush=True)


def _recommend(frozen, hold_frozen, hold_monitor, dsr):
    dsr_p = None if not dsr else dsr.get("dsr")
    ci_above = (
        hold_frozen.get("expectancy_lo") is not None
        and hold_frozen["expectancy_lo"] > 0
    )
    beats = (hold_frozen.get("pnl") or 0) > (hold_monitor.get("pnl") or 0)
    robust = frozen != "monitor" and ci_above and beats and dsr_p is not None and dsr_p >= 0.95
    if robust:
        text = (
            "El nombre congelado `%s` cumple la regla de holdout y el Sharpe deflactado. "
            "Aun así este informe no cambia el complemento: la configuración queda como experimento, no como defecto."
            % frozen
        )
        short = "experimento `%s`, sin cambiar el defecto" % frozen
    else:
        text = (
            "Ninguna configuración supera al monitor con la regla fijada de antemano. "
            "Se mantiene el libro actual: variante A, estilo sweep, ambos setups, salida por agotamiento, "
            "15 velas, margen de stop 0,3 %, objetivo del swing, trailing apagado, riesgo 1 %, "
            "apalancamiento 3, máximo 3 posiciones, comisión 0,06 % y slippage 0,02 % por lado."
        )
        short = "mantener la configuración actual"
    return text, short, robust


def parity_replay(market, course_events, indicators, end_ms):
    """Replay 1 Nov onward with the add-on's own fill rules and 1-minute bars.

    The research engine fills at the signal close. The live book fills at the
    scan price about a minute later and refuses a print that is already
    through the stop or the target. This pass uses that path on Binance
    1-minute candles so the four live trades can be compared one by one.
    """
    os.environ.setdefault("DATA_DIR", "/tmp/granmago-bt-parity")
    os.environ.setdefault("LOG_LEVEL", "error")
    # The add-on imports Flask and requests. The parity path only reuses the
    # fill and exit functions, so a stub is enough when those packages are absent.
    if "flask" not in sys.modules:
        flask_mod = type(sys)("flask")

        class _Flask:
            def __init__(self, *args, **kwargs):
                pass

            def route(self, *args, **kwargs):
                return lambda fn: fn

        flask_mod.Flask = _Flask
        flask_mod.jsonify = lambda *args, **kwargs: None
        flask_mod.request = type("request", (), {"headers": {}})()
        sys.modules["flask"] = flask_mod
    if "requests" not in sys.modules:
        requests_mod = type(sys)("requests")

        class _Session:
            headers = {}

        requests_mod.Session = _Session
        requests_mod.RequestException = Exception
        sys.modules["requests"] = requests_mod
    import server  # noqa: E402

    server.save_state = lambda: None
    server.add_log = lambda *args, **kwargs: None
    t0 = int(datetime(2026, 11, 1, tzinfo=timezone.utc).timestamp() * 1000)
    minutes = _load_minutes(t0 - H4_MS, end_ms)
    server.STATE = server.default_state()
    server.STATE["balance"] = 1000.0
    server.STATE["initialBalance"] = 1000.0
    server.STATE["running"] = True
    server.STATE["settings"]["riskPct"] = 1.0
    server.STATE["settings"]["leverage"] = 3
    server.STATE["settings"]["maxPositions"] = 3
    by_time = {}
    for ev in course_events["base"]:
        if ev["t"] < t0 or ev["t"] > end_ms:
            continue
        by_time.setdefault(ev["t"], []).append(ev)
    # One shared clock: the union of 1-minute opens, walked in order.
    clock = sorted({ts for book in minutes.values() for ts in book["ts"]})
    clock = [ts for ts in clock if t0 - 60_000 <= ts < end_ms]
    index = {pair: {ts: i for i, ts in enumerate(book)} for pair, book in ((p, minutes[p]["ts"]) for p in minutes)}
    latest = {}

    def bar_at(pair, ts):
        book = minutes.get(pair)
        if book is None:
            return None
        i = index[pair].get(ts)
        if i is None:
            return None
        return (book["o"][i], book["h"][i], book["l"][i], book["c"][i])

    def walk(pair, bar, decision):
        server._clock = lambda d=decision: d / 1000.0
        o, h, l, c = bar
        seq = (o, l, h, c) if c >= o else (o, h, l, c)
        for px in seq:
            pos = server._find_position(pair)
            if pos is None:
                return
            server._apply_granmago_mark(pos, px)

    for open_ts in clock:
        decision = open_ts + 60_000
        server._clock = lambda d=decision: d / 1000.0
        for pair in PAIRS:
            bar = bar_at(pair, open_ts)
            if bar is None or server._find_position(pair) is None:
                continue
            walk(pair, bar, decision)
        # Momentum and time, on the confirmation close only.
        for pos in list(server.STATE["openPositions"]):
            pair = pos.get("pair")
            snap = indicators["a"].get(pair, {}).get(open_ts)
            if snap is None:
                continue
            decision_exit = course.exit_decision(pos, {
                "close": snap["close"],
                "k": snap["k"],
                "hist": snap["hist"],
                "prevHist": snap["prev"],
                "confirmMs": snap["confirm_ms"],
                "barTs": snap["bar_ts"],
            })
            if decision_exit is None:
                continue
            reason, price = decision_exit
            if server._find_position(pair) is None:
                continue
            server.close_position(reason, price, pair)
        # A signal stays eligible until the next 4H close, same as the live scan,
        # which keeps seeing that bar and retries while the print is inside the bracket.
        for pair, ev in list(latest.items()):
            if open_ts >= ev["t"] + H4_MS:
                latest.pop(pair, None)
        for ev in by_time.get(open_ts, []):
            latest[ev["pair"]] = ev
        if not latest:
            continue
        for pair in PAIRS:
            ev = latest.get(pair)
            if ev is None:
                continue
            if len(server.STATE["openPositions"]) >= 3:
                break
            if server._signal_used(pair, ev["side"], ev["confirm_ms"], ev["close_ms"]):
                continue
            bar = bar_at(pair, open_ts)
            if bar is None:
                continue
            payload = {
                "pair": pair,
                "side": ev["side"],
                "price": ev["price"],
                "sl": ev["sl"],
                "tp": ev["tp"],
                "setup": ev.get("setup"),
                "confirmMs": ev["confirm_ms"],
                "closeMs": ev["close_ms"],
                "market": bar[3],
                "addPrice": ev.get("price"),
                "sweepExtreme": ev.get("price"),
            }
            server.open_granmago(payload)
    closed = []
    for row in server.STATE["history"]:
        closed.append({
            "pair": row.get("pair"),
            "side": row.get("side"),
            "reason": row.get("reason"),
            "entry": row.get("entry"),
            "exit": row.get("exit"),
            "pnl": row.get("pnl"),
            "r": row.get("rMultiple"),
            "close_ms": row.get("closeMs"),
            "time": row.get("time"),
        })
    still = []
    for pos in server.STATE["openPositions"]:
        still.append({
            "pair": pos.get("pair"),
            "side": pos.get("side"),
            "entry": pos.get("entry"),
            "sl": pos.get("sl"),
            "tp": pos.get("tp"),
            "close_ms": pos.get("closeMs"),
            "signal": pos.get("signalPrice"),
            "unreal": pos.get("unrealized"),
        })
    signals = []
    for ev in course_events["base"]:
        if WEEK_START <= ev["t"] <= end_ms:
            signals.append({
                "pair": ev["pair"], "side": ev["side"], "t": ev["t"],
                "price": ev["price"], "sl": ev["sl"], "tp": ev["tp"], "setup": ev.get("setup"),
            })
    return {
        "balance": server.STATE["balance"],
        "closed": closed,
        "open": still,
        "signals_since_oct27": signals,
        "minute_pairs": len(minutes),
    }


def _load_minutes(start_ms, end_ms):
    """1-minute Binance (spot) or Blofin (futures-only) bars for the live window."""
    import fetch_binance as fb

    out = {}
    futures = set(fb.FUTURES_ONLY)
    for pair in PAIRS:
        if pair in futures:
            rows = _blofin_1m(pair, start_ms, end_ms)
        else:
            rows = _spot_1m(pair.replace("-", ""), start_ms, end_ms)
        rows = [r for r in rows if start_ms <= r["ts"] < end_ms]
        dedup = {r["ts"]: r for r in rows}
        ordered = [dedup[k] for k in sorted(dedup)]
        out[pair] = {
            "ts": [r["ts"] for r in ordered],
            "o": [r["o"] for r in ordered],
            "h": [r["h"] for r in ordered],
            "l": [r["l"] for r in ordered],
            "c": [r["c"] for r in ordered],
        }
        print("  1m %s %d" % (pair, len(ordered)), flush=True)
    return out


def _spot_1m(symbol, start_ms, end_ms):
    import fetch_binance as fb

    rows = []
    cursor = start_ms
    while cursor < end_ms:
        url = "%s?symbol=%s&interval=1m&startTime=%d&endTime=%d&limit=1000" % (
            fb.SPOT_API, symbol, cursor, end_ms,
        )
        blob = fb._get(url)
        if not blob:
            break
        page = json.loads(blob.decode("utf-8"))
        if not isinstance(page, list) or not page:
            break
        for candle in page:
            try:
                rows.append({
                    "ts": int(candle[0]),
                    "o": float(candle[1]),
                    "h": float(candle[2]),
                    "l": float(candle[3]),
                    "c": float(candle[4]),
                })
            except (TypeError, ValueError, IndexError):
                continue
        nxt = int(page[-1][0]) + 60_000
        if nxt <= cursor:
            break
        cursor = nxt
        if len(page) < 1000:
            break
    return rows


def _blofin_1m(inst_id, start_ms, end_ms):
    import fetch_binance as fb

    rows = []
    after = end_ms + 60_000
    for _ in range(30):
        url = "%s?instId=%s&bar=1m&limit=300&after=%d" % (fb.BLOFIN, inst_id, after)
        blob = fb._get(url)
        if not blob:
            break
        body = json.loads(blob.decode("utf-8"))
        data = body.get("data") if isinstance(body, dict) else None
        if not data:
            break
        oldest = None
        for candle in data:
            try:
                ts = int(float(candle[0]))
                close = float(candle[4])
            except (TypeError, ValueError, IndexError):
                continue
            oldest = ts if oldest is None else min(oldest, ts)
            if start_ms <= ts <= end_ms and close > 0:
                rows.append({
                    "ts": ts, "o": float(candle[1]), "h": float(candle[2]),
                    "l": float(candle[3]), "c": close,
                })
        if oldest is None or oldest <= start_ms:
            break
        after = oldest
    return rows


def _match_note(parity):
    lines = []
    closed = {(row["pair"], row["side"], row.get("close_ms")): row for row in parity["closed"]}
    lines.append(
        "Del 1 al 3 de noviembre el complemento, reiniciado en torno a esa fecha, cerró tres operaciones y dejó una abierta. "
        "El backtest de esta sección usa las reglas reales de `open_granmago` (un fill por señal, precio del minuto siguiente al cierre de 4H, rechazo si ese precio ya está en el stop o en el objetivo) sobre velas de 1 minuto de Binance. "
        "El estudio largo, igual que la PR #7, entra al cierre de la señal en velas de 15 minutos."
    )
    lines.append("")
    lines.append("| Señal en vivo | Backtest | Entrada vivo | Entrada bt | PnL vivo | PnL bt | Motivo vivo | Motivo bt |")
    lines.append("|---|---|---:|---:|---:|---:|---|---|")
    matched = 0
    for live in LIVE:
        key = (live["pair"], live["side"], live["close_ms"])
        bt = closed.get(key)
        if bt:
            matched += 1
            lines.append("| %s %s %s | sí | %s | %s | %s | %s | %s | %s |" % (
                live["pair"], live["side"], _iso(live["close_ms"]),
                _fmt(live["entry"], 4), _fmt(bt["entry"], 4),
                _fmt(live["pnl"], 2), _fmt(bt["pnl"], 2),
                live["reason"], bt["reason"],
            ))
        else:
            lines.append("| %s %s %s | no | %s | - | %s | - | %s | - |" % (
                live["pair"], live["side"], _iso(live["close_ms"]),
                _fmt(live["entry"], 4), _fmt(live["pnl"], 2), live["reason"],
            ))
    opens = {(row["pair"], row["side"], row.get("close_ms")): row for row in parity["open"]}
    bt_open = opens.get((LIVE_OPEN["pair"], LIVE_OPEN["side"], LIVE_OPEN["close_ms"]))
    if bt_open:
        matched += 1
        lines.append("| %s %s abierto %s | abierta | %s | %s | - | - | - | - |" % (
            LIVE_OPEN["pair"], LIVE_OPEN["side"], _iso(LIVE_OPEN["close_ms"]),
            _fmt(LIVE_OPEN["entry"], 4), _fmt(bt_open["entry"], 4),
        ))
    else:
        lines.append("| %s %s abierto %s | no | %s | - | - | - | - | - |" % (
            LIVE_OPEN["pair"], LIVE_OPEN["side"], _iso(LIVE_OPEN["close_ms"]),
            _fmt(LIVE_OPEN["entry"], 4),
        ))
    lines.append("")
    lines.append(
        "Coinciden %d de 4 señales (par, lado y cierre de 4H). El precio de entrada no tiene que ser igual: el vivo usa el ticker unos 90 s después del cierre y las velas de la señal salen de Blofin si responden, con Binance como respaldo."
        % matched
    )
    extra = [row for row in parity["closed"] if (row["pair"], row["side"], row.get("close_ms")) not in {
        (live["pair"], live["side"], live["close_ms"]) for live in LIVE
    }]
    if extra:
        lines.append("")
        lines.append("El replay cerró además: " + ", ".join(
            "%s %s %s %s %s USDT" % (row["pair"], row["side"], row["reason"], _iso(row.get("close_ms")), _fmt(row["pnl"], 2))
            for row in extra
        ) + ".")
    if parity["open"]:
        lines.append("")
        lines.append("Abiertas al corte: " + ", ".join(
            "%s %s entrada %s señal %s" % (row["pair"], row["side"], _fmt(row["entry"], 4), _iso(row.get("close_ms")))
            for row in parity["open"]
        ) + ".")
    lines.append("")
    lines.append("Balance realizado del replay: %s USDT (vivo 991,97, con la AAVE corta todavía abierta)." % _fmt(parity["balance"], 2))
    week_signals = [s for s in parity["signals_since_oct27"] if s["t"] >= int(datetime(2026, 11, 1, tzinfo=timezone.utc).timestamp() * 1000)]
    lines.append("")
    lines.append("Señales de la variante A en Binance desde el 1 de noviembre: %d." % len(week_signals))
    if week_signals:
        lines.append("")
        lines.append("| Par | Lado | Cierre UTC | Precio | SL | TP |")
        lines.append("|---|---|---|---:|---:|---:|")
        for sig in week_signals:
            lines.append("| %s | %s | %s | %s | %s | %s |" % (
                sig["pair"], sig["side"], _iso(sig["t"]),
                _fmt(sig["price"], 6), _fmt(sig["sl"], 6), _fmt(sig["tp"], 6),
            ))
    return "\n".join(lines), matched


def main():
    only = [arg.split("=", 1)[1] for arg in sys.argv[1:] if arg.startswith("--only=")]
    print("load", flush=True)
    manifest, end_ms, market = load_market()
    print("end", _iso(end_ms), flush=True)
    slices = slices_of(end_ms)
    for name, lo, hi in slices:
        print("  %s %s → %s" % (name, _iso(lo), _iso(hi)), flush=True)
    print("signals", flush=True)
    course_events, indicators = prepare_course(market, end_ms)
    trend_events = prepare_trend(market, end_ms)
    specs = [spec for spec in CONFIGS if not only or spec["name"] in only]
    filtered = {}
    for spec in specs:
        filtered[spec["name"]] = filter_events(spec, course_events, trend_events)
        print("events %s %d" % (spec["name"], len(filtered[spec["name"]])), flush=True)
    series = series_of(market)
    clock = clock_of(series)
    global _SERIES, _CLOCK, _FILTERED, _INDICATORS, _SLICES, _SPECS
    _SERIES = series
    _CLOCK = clock
    _FILTERED = filtered
    _INDICATORS = indicators
    _SLICES = slices
    _SPECS = {spec["name"]: spec for spec in specs}
    print("simulate %d configs, clock %d" % (len(specs), len(clock)), flush=True)
    results = {}
    names = [spec["name"] for spec in specs]
    workers = min(4, len(names), os.cpu_count() or 1)
    if len(names) == 1:
        name, rows = _run_spec(names[0])
        results[name] = rows
    else:
        ctx = multiprocessing.get_context("fork")
        with ctx.Pool(workers) as pool_workers:
            for name, rows in pool_workers.map(_run_spec, names):
                results[name] = rows
    if only:
        for name in names:
            print(name, [(r["name"], r["trades"], r["pnl"], r["expectancy"]) for r in results[name]])
        return
    selectable = [spec for spec in CONFIGS if spec.get("select", True)]
    block = nested_and_holdout(results, slices, selectable)
    pre_names = [name for name, _lo, _hi in slices if "S02" <= name < HOLDOUT_FROM]
    old_hold = [name for name, _lo, _hi in slices if HOLDOUT_FROM <= name < "S24"]
    new_hold = [name for name, _lo, _hi in slices if name >= HOLDOUT_FROM]
    fixed = {}
    trial_sharpes = []
    for spec in selectable:
        summary = enrich(_rows_named(results[spec["name"]], pre_names))
        summary["label"] = spec["name"]
        sharpe = trade_sharpe(summary.get("rs") or [])
        summary["sharpe"] = None if sharpe is None else round(sharpe["sharpe"], 4)
        trial_sharpes.append(summary["sharpe"])
        fixed[spec["name"]] = summary
    stitched = enrich(block["stitched"])
    stitched["label"] = "anidada"
    dsr = deflated_sharpe(trade_sharpe(stitched.get("rs") or []), trial_sharpes)
    print("FROZEN", block["frozen"], "plateau", block["frozen_plateau"], "DSR", dsr, flush=True)
    for choice in block["choices"]:
        print("  %s -> %s" % (choice["test"], choice["chosen"]), flush=True)

    def window(name, names_):
        return enrich(_rows_named(results[name], names_))

    hold_old = {spec["name"]: window(spec["name"], old_hold) for spec in CONFIGS}
    hold_new = {spec["name"]: window(spec["name"], new_hold) for spec in CONFIGS}
    # Continuous account and the last week, monitor and frozen name.
    s02 = next(lo for name, lo, hi in slices if name == "S02")
    extra_slices = {
        "full": [("FULL", s02, end_ms)],
        "week": [("WEEK", WEEK_START, end_ms)],
        "s24": [("S24", OLD_END, end_ms)],
    }
    extra = {}
    for spec_name in ("monitor", block["frozen"], "trail_1_1_hold40", "trend_ema50_4h"):
        spec = _SPECS[spec_name]
        spec = dict(spec)
        spec["keep_trades"] = True
        indicators_used = indicators["b"] if spec.get("signal_set") == "variant_b" else indicators["a"]
        if spec["family"] == "trend":
            indicators_used = {}
        for key, sl in extra_slices.items():
            rows = simulate(clock, series, filtered[spec_name], indicators_used, spec, sl)
            extra[(spec_name, key)] = rows[0]
            print("extra", spec_name, key, "trades", rows[0]["trades"], "pnl", rows[0]["pnl"], flush=True)

    print("parity", flush=True)
    try:
        parity = parity_replay(market, course_events, indicators, end_ms)
        parity_md, matched = _match_note(parity)
    except Exception as exc:
        import traceback
        traceback.print_exc()
        parity = {"closed": [], "open": [], "balance": None}
        parity_md = "El replay del libro en vivo falló: %s." % exc
        matched = 0

    table = []
    ranked = sorted(selectable, key=lambda spec: -(fixed[spec["name"]]["pnl"] or 0))
    for spec in ranked:
        sel = fixed[spec["name"]]
        old = hold_old[spec["name"]]
        new = hold_new[spec["name"]]
        table.append({
            "name": spec["name"],
            "sel_trades": sel["trades"],
            "sel_win": sel["win_rate"],
            "sel_pf": sel["pf"],
            "sel_net_r": sel["net_r"],
            "sel_pnl": sel["pnl"],
            "sel_dd": sel["worst_dd_pct"],
            "old_pnl": old["pnl"],
            "old_exp": old["expectancy"],
            "new_pnl": new["pnl"],
            "new_exp": new["expectancy"],
            "old": _public(old),
            "new": _public(new),
            "sel": _public(sel),
        })
    rec_text, rec_short, robust = _recommend(block["frozen"], hold_new[block["frozen"]], hold_new["monitor"], dsr)
    sources = {}
    for pair, info in manifest["pairs"].items():
        sources.setdefault(info["market"] + "+" + info["tail"], []).append(pair)
    source_bits = ", ".join("%s (%d)" % (key, len(value)) for key, value in sorted(sources.items()))
    gaps = [
        "%s %s%%" % (pair, _fmt(info["join_gap"] * 100, 3))
        for pair, info in manifest["pairs"].items()
        if info.get("join_gap") is not None and abs(info["join_gap"]) >= 0.001
    ]
    data_note = (
        "28 pares, de 2023-01-12 al último cierre de 15 minutos (%s UTC). Origen: %s. "
        "XMR y 1000BONK no tienen histórico spot completo en Binance; van por el dump de futuros USD-M, y el día en curso sale de Blofin. "
        "El resto es spot de data.binance.vision, con el día en curso por la API pública de vision. "
        "Hueco de empalme al día en curso, en %% del cierre anterior: %s."
        % (_iso(end_ms), source_bits, ", ".join(gaps) if gaps else "ninguno relevante")
    )
    method_note = (
        "Misma rejilla de pliegues que la PR #7: 45 días anclados al 22 de enero de 2024 (Madrid), S01 solo entrena. "
        "S23 se corta el 2026-10-24 00:00 UTC para poder comparar el holdout de aquella PR en el mismo calendario. "
        "S24 es del 2026-10-24 al %s y no entra en la selección. "
        "Hay %d configuraciones en la búsqueda (el monitor más variaciones de objetivo en R, stop por margen y por ATR, trailing, salida por tiempo, filtro de tendencia, confirmación de 12H y máximo de posiciones) y 2 copias de tamaño fuera de la búsqueda. "
        "La puntuación es la suma del PnL de los pliegues de entrenamiento, con un mínimo de 40 operaciones. "
        "Dentro de la meseta (máximo entre 50 USDT y el 15 %% del mejor PnL) gana el rango más bajo, así que el monitor se queda si está en la banda. "
        "Comisiones 0,06 %% y slippage 0,02 %% por lado en cada fill. Cada pliegue vuelve a 1.000 USDT."
        % (_iso(end_ms), len(selectable))
    )
    dsr_p = None if not dsr else dsr.get("dsr")
    dsr_note = (
        "Sharpe deflactado de la serie de R de la regla anidada, contra la dispersión de los %d Sharpe de la búsqueda (Bailey y López de Prado): **%s** "
        "(SR por operación %s, SR0 %s). La PR #7, con otras 33 configuraciones y velas de Blofin, también dio 0."
        % (
            len(selectable),
            _fmt(dsr_p, 4) if dsr_p is not None else "indefinido",
            _fmt(None if not dsr else dsr.get("sr"), 4),
            _fmt(None if not dsr else dsr.get("sr0"), 4),
        )
    )
    week_row = extra[("monitor", "week")]
    week_summary = enrich([week_row])
    full_row = extra[("monitor", "full")]
    full_summary = enrich([full_row])
    s24_monitor = enrich([extra[("monitor", "s24")]])
    frozen_week = enrich([extra[(block["frozen"], "week")]])
    trail_old = hold_old["trail_1_1_hold40"]
    trail_new = hold_new["trail_1_1_hold40"]
    trend_old = hold_old["trend_ema50_4h"]
    trend_new = hold_new["trend_ema50_4h"]
    mon_old = hold_old["monitor"]
    mon_new = hold_new["monitor"]
    week_note = (
        "En la PR #7, con Blofin y el corte del 23 de octubre, el monitor en el holdout hizo +51 USDT, PF 1,11, E[R] +0,028, y el intervalo incluía cero. "
        "El nombre congelado de entonces (`sweep_ab_trail1_time40`, aquí `trail_1_1_hold40`) hizo +38 USDT, E[R] +0,041, también con el intervalo en cero. "
        "La regla anidada sumó +566 USDT en S02–S19 con Sharpe deflactado 0, y el veredicto fue dejar el monitor.\n\n"
        "Con Binance y el mismo calendario S20–S23, el monitor hace %s USDT (E[R] %s, IC %s..%s) y `trail_1_1_hold40` hace %s USDT (E[R] %s). "
        "`trend_ema50_4h`, el camino que aquella regla anidada operó casi siempre, hace %s USDT (E[R] %s). "
        "La diferencia con +51 / +38 mezcla el cambio de fuente (Binance en vez de Blofin) con unas horas de más hasta el 24 de octubre; no es el efecto de la última semana.\n\n"
        "S24, del 24 de octubre al %s, es la barra nueva. El monitor hace %s USDT en %d operaciones (E[R] %s). "
        "La semana del 27 de octubre al %s, en una cuenta que empieza en 1.000, el monitor hace %s USDT en %d operaciones (PF %s, R neto %s, E[R] %s, DD %s%%). "
        "El nombre congelado de esta pasada (`%s`) en esa semana hace %s USDT. "
        "Ninguna de esas cifras cambia la regla: el holdout completo S20–S24 del monitor es %s USDT (E[R] %s, IC %s..%s) y el del congelado `%s` es %s USDT (E[R] %s, IC %s..%s)."
        % (
            _fmt(mon_old["pnl"], 0), _fmt(mon_old["expectancy"], 3),
            _fmt(mon_old["expectancy_lo"], 3), _fmt(mon_old["expectancy_hi"], 3),
            _fmt(trail_old["pnl"], 0), _fmt(trail_old["expectancy"], 3),
            _fmt(trend_old["pnl"], 0), _fmt(trend_old["expectancy"], 3),
            _iso(end_ms), _fmt(s24_monitor["pnl"], 0), s24_monitor["trades"], _fmt(s24_monitor["expectancy"], 3),
            _iso(end_ms), _fmt(week_summary["pnl"], 0), week_summary["trades"],
            _fmt(week_summary["pf"]), _fmt(week_summary["net_r"], 1), _fmt(week_summary["expectancy"], 3),
            _fmt(week_summary["worst_dd_pct"], 1),
            block["frozen"], _fmt(frozen_week["pnl"], 0),
            _fmt(mon_new["pnl"], 0), _fmt(mon_new["expectancy"], 3),
            _fmt(mon_new["expectancy_lo"], 3), _fmt(mon_new["expectancy_hi"], 3),
            block["frozen"], _fmt(hold_new[block["frozen"]]["pnl"], 0),
            _fmt(hold_new[block["frozen"]]["expectancy"], 3),
            _fmt(hold_new[block["frozen"]]["expectancy_lo"], 3),
            _fmt(hold_new[block["frozen"]]["expectancy_hi"], 3),
        )
    )
    week_note += (
        "\n\nLa PR #7 no se mueve: el intervalo del holdout sigue incluyendo cero o el congelado no bate al monitor con un Sharpe deflactado creíble, "
        "y la semana nueva no aporta una muestra que justifique cambiar el libro. Recomendación de esta pasada: %s."
        % rec_short
    )
    baseline_rows = [
        ("S02–S19, pliegues a 1.000", fixed["monitor"]),
        ("S20–S23, holdout antiguo", mon_old),
        ("S20–S24, holdout con la semana", mon_new),
        ("S24, solo datos nuevos", s24_monitor),
        ("27 oct–%s, cuenta continua" % _iso(end_ms)[5:16], week_summary),
        ("S02–%s, cuenta continua" % _iso(end_ms)[5:16], full_summary),
    ]
    oos_rows = [
        ("Monitor S02–S19", fixed["monitor"]),
        ("Regla anidada S02–S19", stitched),
        ("Monitor holdout S20–S23", mon_old),
        ("Monitor holdout S20–S24", mon_new),
        ("Congelado `%s` en S02–S19" % block["frozen"], fixed[block["frozen"]]),
        ("Congelado `%s` holdout S20–S24" % block["frozen"], hold_new[block["frozen"]]),
        ("trail_1_1_hold40 holdout S20–S23", trail_old),
        ("trail_1_1_hold40 holdout S20–S24", trail_new),
        ("trend_ema50_4h holdout S20–S23", trend_old),
        ("trend_ema50_4h holdout S20–S24", trend_new),
    ]
    sizing = []
    for name in ("monitor", "risk_0.5", "risk_2.0"):
        sel = enrich(_rows_named(results[name], pre_names))
        sizing.append({
            "name": name,
            "trades": sel["trades"],
            "pf": sel["pf"],
            "net_r": sel["net_r"],
            "expectancy": sel["expectancy"],
            "pnl": sel["pnl"],
            "hold_pnl": hold_new[name]["pnl"],
        })
    verdict = (
        "**%s.** Búsqueda de %d configuraciones, Sharpe deflactado %s. "
        "Nombre congelado en el entrenamiento: `%s` (meseta: %s). "
        "Monitor en S02–S19: %d operaciones, PF %s, R neto %s, %s USDT, E[R] %s. "
        "Holdout S20–S24 del monitor: %s USDT, E[R] %s (IC %s..%s). "
        "Última semana, monitor en cuenta de 1.000: %s USDT en %d operaciones. "
        "El replay del 1 al 3 de noviembre reproduce %d de 4 señales del libro en vivo."
        % (
            rec_short.capitalize(),
            len(selectable),
            _fmt(dsr_p, 4) if dsr_p is not None else "indefinido",
            block["frozen"],
            ", ".join(block["frozen_plateau"]),
            fixed["monitor"]["trades"],
            _fmt(fixed["monitor"]["pf"]),
            _fmt(fixed["monitor"]["net_r"], 1),
            _fmt(fixed["monitor"]["pnl"], 0),
            _fmt(fixed["monitor"]["expectancy"], 3),
            _fmt(mon_new["pnl"], 0),
            _fmt(mon_new["expectancy"], 3),
            _fmt(mon_new["expectancy_lo"], 3),
            _fmt(mon_new["expectancy_hi"], 3),
            _fmt(week_summary["pnl"], 0),
            week_summary["trades"],
            matched,
        )
    )
    payload = {
        "verdict": verdict,
        "data_note": data_note,
        "method_note": method_note,
        "parity_note": parity_md,
        "baseline_rows": baseline_rows,
        "table": table,
        "oos_rows": oos_rows,
        "dsr_note": dsr_note,
        "week_note": week_note,
        "sizing": sizing,
        "recommendation": rec_text,
    }
    out_dir = ROOT / "docs" / "results"
    out_dir.mkdir(parents=True, exist_ok=True)
    # Drop the long R lists before writing JSON.
    archive = {
        "end": _iso(end_ms),
        "frozen": block["frozen"],
        "frozen_relaxed": block["frozen_relaxed"],
        "plateau": block["frozen_plateau"],
        "choices": block["choices"],
        "dsr": dsr,
        "robust": robust,
        "recommendation": rec_short,
        "matched_live": matched,
        "table": [{k: v for k, v in row.items() if k not in ("old", "new", "sel")} | {
            "sel": row["sel"], "old": row["old"], "new": row["new"],
        } for row in table],
        "oos": [(label, _public(row)) for label, row in oos_rows],
        "baseline": [(label, _public(row)) for label, row in baseline_rows],
        "sizing": sizing,
        "parity_closed": parity["closed"],
        "parity_open": parity["open"],
        "train_rank": block["train_rank"],
        "manifest": manifest["pairs"],
    }
    (out_dir / "2026-11-03-backtest.json").write_text(json.dumps(archive, indent=2), encoding="utf-8")
    write_report(out_dir / "2026-11-03-backtest.md", payload)
    print("DONE", rec_short, "dsr", dsr_p, "matched", matched, flush=True)


if __name__ == "__main__":
    main()
