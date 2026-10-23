#!/usr/bin/env python3
"""Nested walk-forward for GranMago 1.6.0.

The config list below is the whole search. It was fixed before any of these
folds were read. Training scores use only slices before the test slice.
Slices from S20 on (about 27 May 2026 through the last bar) are a holdout:
they are not used to pick a config, and they are scored once at the end.

Fees stay 0.06% taker plus 0.02% slippage per side, except the one maker
entry config (0.02% maker, no entry slippage, taker exit). Risk is 1% and
leverage is 3 with at most 3 positions, which is the 1.5.0 paper book.
Each test slice resets to 1,000 USDT. S01 is training context and is not
an out-of-sample fold. The published 1.5.0 table used risk 2%, so its
dollar PnL is not the comparison; trade count, profit factor and E[R] are.
"""

import json
import os
import pickle
import sys
import time
from pathlib import Path

os.environ["BACKTEST_CACHE"] = "/tmp/granmago-bt-cache-long"
os.environ.setdefault("DATA_DIR", "/tmp/granmago-bt-data")
os.environ.setdefault("LOG_LEVEL", "error")

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(TOOLS.parent / "granmago"))

import granmago_strategy as course  # noqa: E402
import walkforward_sweep as ws  # noqa: E402
from sim16 import (  # noqa: E402
    BAR_MS,
    EXTREME_FUNDING,
    accept_event,
    bootstrap_ci,
    deflated_sharpe,
    last_funding,
    pool,
    select_config,
    simulate,
    sum_folds,
    trade_sharpe,
)
from trend16 import DAY_MS, H4_MS, build_trend  # noqa: E402

HOLDOUT_FROM = "S20"
# Published S02 targets from the 1.5.0 study, used only to check this engine.
CALIBRATE = {
    "core12": {"trades": 17, "pf": 0.68, "pnl_pct": -2.5},
    "extended28": {"trades": 35, "pf": 0.92, "pnl_pct": -1.3},
}


def _course(**kw):
    spec = {
        "family": "course",
        "rank": 1,
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
    }
    spec.update(kw)
    return spec


def _trend(**kw):
    spec = {
        "family": "trend",
        "rank": 1,
        "mode": "ema",
        "tf": "1d",
        "lookback": 50,
        "exit_lookback": 20,
        "atr_mult": 3.0,
        "sides": "both",
        "adx_min": 0.0,
        "vol_target": 0.0,
        "trail_always": True,
        "target": "none",
        "momentum": False,
        "hold": 0,
        "maker": False,
    }
    spec.update(kw)
    return spec


# Frozen before the run. Rank 0 is the 1.5.0 monitor book. A higher rank has
# to clear the plateau band on training PnL before it can be selected.
CONFIGS = [
    _course(name="sweep_ab", rank=0),
    _course(name="sweep_ab_no_mom", momentum=False),
    _course(name="sweep_ab_no_time", hold=0),
    _course(name="sweep_ab_hold40", hold=40),
    _course(name="sweep_ab_be1", be_r=1.0),
    _course(name="sweep_ab_minrr1", min_rr=1.0),
    _course(name="sweep_ab_minrr15", min_rr=1.5),
    _course(name="sweep_ab_stop4", max_stop_pct=0.04),
    _course(name="sweep_ab_long", sides="long"),
    _course(name="sweep_ab_weekly", weekly=True),
    _course(name="sweep_only", setups="sweep"),
    _course(name="sweep_ab_funding", funding_max=EXTREME_FUNDING),
    _course(name="sweep_ab_tp2r", rank=2, target="r", target_r=2.0, momentum=False, hold=0),
    _course(name="sweep_ab_tp3r", rank=2, target="r", target_r=3.0, momentum=False, hold=0),
    _course(name="sweep_ab_partial", rank=2, partial_r=1.0, momentum=False, hold=30),
    _course(name="sweep_ab_trail1", rank=2, target="none", trail_activate_r=1.0, trail_dist_r=1.0, momentum=False, hold=0),
    _course(name="sweep_ab_trail1_time40", rank=2, target="none", trail_activate_r=1.0, trail_dist_r=1.0, momentum=False, hold=40),
    _course(name="sweep_ab_nomom_notime", rank=2, momentum=False, hold=0),
    _course(name="sweep_ab_maker", rank=2, maker=True),
    _trend(name="trend_ema50_d", mode="ema", tf="1d", lookback=50, atr_mult=3.0),
    _trend(name="trend_ema50_d_long", mode="ema", tf="1d", lookback=50, atr_mult=3.0, sides="long"),
    _trend(name="trend_ema100_d", mode="ema", tf="1d", lookback=100, atr_mult=3.0),
    _trend(name="trend_ema200_d", mode="ema", tf="1d", lookback=200, atr_mult=3.0),
    _trend(name="donchian_20_10_d", mode="donchian", tf="1d", lookback=20, exit_lookback=10, atr_mult=3.0, trail_always=False),
    _trend(name="donchian_55_20_d", mode="donchian", tf="1d", lookback=55, exit_lookback=20, atr_mult=3.0, trail_always=False),
    _trend(name="tsmom_30_d", mode="tsmom", tf="1d", lookback=30, atr_mult=3.0),
    _trend(name="tsmom_90_d", mode="tsmom", tf="1d", lookback=90, atr_mult=3.0),
    _trend(name="tsmom_90_d_long", mode="tsmom", tf="1d", lookback=90, atr_mult=3.0, sides="long"),
    _trend(name="trend_ema20_d", rank=2, mode="ema", tf="1d", lookback=20, atr_mult=3.0),
    _trend(name="trend_ema50_4h", rank=2, mode="ema", tf="4h", lookback=50, atr_mult=2.5),
    _trend(name="donchian_20_10_4h", rank=2, mode="donchian", tf="4h", lookback=20, exit_lookback=10, atr_mult=2.5, trail_always=False),
    _trend(name="trend_ema50_d_adx", rank=2, mode="ema", tf="1d", lookback=50, atr_mult=3.0, adx_min=20.0),
    _trend(name="tsmom_90_d_vol", rank=2, mode="tsmom", tf="1d", lookback=90, atr_mult=3.0, vol_target=0.20),
]


def _series(rows):
    return {
        "ts": [int(r["ts"]) for r in rows],
        "o": [float(r["o"]) for r in rows],
        "h": [float(r["h"]) for r in rows],
        "l": [float(r["l"]) for r in rows],
        "c": [float(r["c"]) for r in rows],
    }


def _weekly_at(weekly):
    averages = course.ema_series([float(c["c"]) for c in weekly])

    def at(ms):
        side = None
        for candle, value in zip(weekly, averages):
            if int(candle["ts"]) + course.WEEK_MS > ms:
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


def _load_funding():
    path = Path("/tmp/granmago-funding.json")
    if not path.exists():
        import fetch_funding
        fetch_funding.main()
    raw = json.loads(path.read_text(encoding="utf-8"))
    out = {}
    for pair, rows in raw.items():
        out[pair] = [(int(ts), float(rate)) for ts, rate in rows]
    return out


def prepare(market, pairs, end_ms, funding):
    cache = Path("/tmp/granmago-v160-course.pkl")
    key = (end_ms, tuple(pairs), len(funding))
    if cache.exists():
        payload = pickle.loads(cache.read_bytes())
        if payload.get("key") == key:
            print("course cache hit", flush=True)
            return payload["events"], payload["indicators"]
    events = []
    indicators = {}
    order = {pair: i for i, pair in enumerate(pairs)}
    for pair in pairs:
        data = market[pair]
        c4 = course.drop_forming(data.h4, course.H4_MS, end_ms)
        daily = course.drop_forming(data.d1, course.DAY_MS, end_ms)
        weekly = course.drop_forming(data.w1, course.WEEK_MS, end_ms)
        signals, bars = course.find_signals(
            c4, daily, weekly, variant="a", min_rr=0.0, stop_pad_pct=0.3,
            filters=True, style="sweep", setups="both",
        )
        by_close = {}
        for signal in signals:
            by_close[signal["closeMs"]] = signal
        side_of = _weekly_at(weekly)
        fund = funding.get(pair) or []
        snaps = {}
        for close_ms, snap in bars.items():
            snaps[int(close_ms)] = {
                "close": snap["close"],
                "k": snap["k"],
                "hist": snap["hist"],
                "prev": snap["prevHist"],
                "confirm_ms": snap["confirmMs"],
                "bar_ts": snap["barTs"],
            }
        indicators[pair] = snaps
        for close_ms, signal in by_close.items():
            events.append({
                "t": int(close_ms),
                "pair": pair,
                "side": signal["side"],
                "price": float(signal["price"]),
                "sl": float(signal["sl"]),
                "tp": float(signal["tp"]) if signal.get("tp") is not None else None,
                "family": "course",
                "setup": signal.get("setup"),
                "confirm_ms": int(signal["confirmMs"]),
                "close_ms": int(signal["closeMs"]),
                "weekly": side_of(int(close_ms)),
                "funding": last_funding(fund, int(close_ms)),
                "strength": 0.0,
                "ann_vol": None,
                "_ord": order[pair],
            })
        print("  course %s signals %d" % (pair, len(by_close)), flush=True)
    events.sort(key=lambda ev: (ev["t"], ev["_ord"]))
    cache.write_bytes(pickle.dumps({"key": key, "events": events, "indicators": indicators}))
    print("wrote", cache, flush=True)
    return events, indicators


def prepare_trend(market, pairs, end_ms):
    """Events and state maps keyed by config name."""
    cache = Path("/tmp/granmago-v160-trend.pkl")
    names = tuple(spec["name"] for spec in CONFIGS if spec["family"] == "trend")
    key = (end_ms, tuple(pairs), names)
    if cache.exists():
        payload = pickle.loads(cache.read_bytes())
        if payload.get("key") == key:
            print("trend cache hit", flush=True)
            return payload["events"], payload["states"]
    events = {spec["name"]: [] for spec in CONFIGS if spec["family"] == "trend"}
    states = {spec["name"]: {} for spec in CONFIGS if spec["family"] == "trend"}
    for pair in pairs:
        data = market[pair]
        c4 = course.drop_forming(data.h4, H4_MS, end_ms)
        daily = course.drop_forming(data.d1, DAY_MS, end_ms)
        for spec in CONFIGS:
            if spec["family"] != "trend":
                continue
            candles = daily if spec["tf"] == "1d" else c4
            bar_ms = DAY_MS if spec["tf"] == "1d" else H4_MS
            evs, state = build_trend(pair, candles, bar_ms, spec)
            events[spec["name"]].extend(evs)
            states[spec["name"]][pair] = state
        print("  trend %s" % pair, flush=True)
    order = {pair: i for i, pair in enumerate(pairs)}
    for name, evs in events.items():
        evs.sort(key=lambda ev: (ev["t"], -float(ev.get("strength") or 0.0), order.get(ev["pair"], 99)))
    cache.write_bytes(pickle.dumps({"key": key, "events": events, "states": states}))
    print("wrote", cache, flush=True)
    return events, states


def _filter(spec, course_events, trend_events, pairs):
    allowed = set(pairs)
    if spec["family"] == "course":
        src = course_events
    else:
        src = trend_events[spec["name"]]
    out = [ev for ev in src if ev["pair"] in allowed and accept_event(ev, spec)]
    return out


def _rows_named(rows, names):
    want = set(names)
    return [row for row in rows if row["name"] in want]


def _attach_rl(summary, rows):
    win_r = []
    loss_r = []
    for row in rows:
        for r, pnl in zip(row["rs"], row["pnls"]):
            if pnl > 0:
                win_r.append(r)
            elif pnl < 0:
                loss_r.append(r)
    summary["avg_win_r"] = None if not win_r else round(sum(win_r) / len(win_r), 4)
    summary["avg_loss_r"] = None if not loss_r else round(sum(loss_r) / len(loss_r), 4)
    return summary


def _public(summary):
    out = dict(summary)
    out.pop("rs", None)
    out.pop("pnls", None)
    out.pop("_win_rows", None)
    return out


def _run_spec(spec, series, events, indicators, slices):
    started = time.time()
    rows = simulate(clock_of(series), series, events, indicators, spec, slices)
    print("  %-24s folds %d trades %d %.0fs" % (
        spec["name"], len(rows), sum(r["trades"] for r in rows), time.time() - started,
    ), flush=True)
    return rows


_CLOCK = None


def clock_of(series):  # noqa: F811
    global _CLOCK
    if _CLOCK is None:
        btc = series["BTC-USDT"]
        fold_start = ws._ms(ws.FOLD_START)
        _CLOCK = [ts for ts in btc["ts"] if ts + BAR_MS >= fold_start]
    return _CLOCK


def nested_and_holdout(spec_rows, slices):
    """Selection rule on one universe. spec_rows[name] is the fold list."""
    names = [name for name, _lo, _hi in slices]
    pre_tests = [name for name in names if "S02" <= name < HOLDOUT_FROM]
    frozen_train = [name for name in names if name < HOLDOUT_FROM]
    hold_names = [name for name in names if name >= HOLDOUT_FROM]
    stitched = []
    choices = []
    for test in pre_tests:
        train_names = [name for name in names if name < test]
        scores = []
        for spec in CONFIGS:
            folds = _rows_named(spec_rows[spec["name"]], train_names)
            score = sum_folds(folds) if folds else {"name": spec["name"], "trades": 0, "pnl": 0.0, "folds": 0}
            score["name"] = spec["name"]
            scores.append(score)
        choice, relaxed, plateau = select_config(scores, CONFIGS)
        row = next(r for r in spec_rows[choice] if r["name"] == test)
        stamped = dict(row)
        stamped["chosen"] = choice
        stamped["relaxed"] = relaxed
        stamped["plateau"] = plateau
        stitched.append(stamped)
        choices.append({"test": test, "chosen": choice, "relaxed": relaxed, "plateau": plateau})
    frozen_scores = []
    for spec in CONFIGS:
        folds = _rows_named(spec_rows[spec["name"]], frozen_train)
        score = sum_folds(folds) if folds else {"name": spec["name"], "trades": 0, "pnl": 0.0, "folds": 0}
        score["name"] = spec["name"]
        frozen_scores.append(score)
    frozen, frozen_relaxed, frozen_plateau = select_config(frozen_scores, CONFIGS)
    hold_rows = _rows_named(spec_rows[frozen], hold_names)
    pre_rows = _rows_named(spec_rows[frozen], pre_tests)
    return {
        "choices": choices,
        "stitched": stitched,
        "frozen": frozen,
        "frozen_relaxed": frozen_relaxed,
        "frozen_plateau": frozen_plateau,
        "frozen_train": sorted(frozen_scores, key=lambda row: -row["pnl"]),
        "hold_rows": hold_rows,
        "pre_rows": pre_rows,
    }


def _fmt_pct(rate):
    if rate is None:
        return "-"
    return "%.0f%%" % (rate * 100)


def _fmt(value, digits=2):
    if value is None:
        return "-"
    return ("%." + str(digits) + "f") % value


def render_summary(row):
    ci = "%s..%s" % (_fmt(row.get("expectancy_lo"), 3), _fmt(row.get("expectancy_hi"), 3))
    return "| %s | %d | %s | %s | %s | %s | %s | %s | %s | %s | %d/%d |" % (
        row["label"],
        row["trades"],
        _fmt_pct(row.get("win_rate")),
        _fmt(row.get("pf")),
        _fmt(row.get("expectancy"), 3),
        ci,
        _fmt(row.get("avg_win_r"), 2),
        _fmt(row.get("avg_loss_r"), 2),
        _fmt(row.get("pnl"), 0),
        _fmt(row.get("worst_dd_pct"), 1),
        row.get("positive_folds") or 0,
        row.get("folds") or 0,
    )


def buy_and_hold(series, pairs, clock, slices, leverage):
    """Equal-weight long book, reset each slice, same taker costs, no stop."""
    from sim16 import INITIAL, SLIP, TAKER, _worse
    rows = []
    for name, t0, t1 in slices:
        if name == "S01":
            continue
        first = None
        last = None
        for open_ts in clock:
            decision = open_ts + BAR_MS
            if decision < t0 or decision >= t1:
                if decision >= t1:
                    break
                continue
            bar_ok = False
            for pair in pairs:
                # linear scan is too slow; use the series cursor via bisect once per edge
                bar_ok = True
                break
            if bar_ok and first is None:
                first = open_ts
            last = open_ts
        if first is None or last is None:
            rows.append({
                "name": name, "trades": 0, "wins": 0, "losses": 0, "win_rate": None,
                "pf": None, "expectancy": None, "avg_win_r": None, "avg_loss_r": None,
                "pnl": 0.0, "pnl_pct": 0.0, "max_dd_pct": 0.0, "reasons": {},
                "long_pnl": 0.0, "short_pnl": 0.0, "long_n": 0, "short_n": 0,
                "rs": [], "pnls": [],
            })
            continue
        import bisect
        priced = []
        for pair in pairs:
            book = series[pair]
            i = bisect.bisect_left(book["ts"], first)
            j = bisect.bisect_left(book["ts"], last)
            if i >= len(book["ts"]) or book["ts"][i] != first:
                continue
            if j >= len(book["ts"]) or book["ts"][j] != last:
                continue
            priced.append((pair, book["c"][i], book["c"][j], book["c"][i:j + 1]))
        balance = INITIAL
        if not priced:
            rows.append({
                "name": name, "trades": 0, "wins": 0, "losses": 0, "win_rate": None,
                "pf": None, "expectancy": None, "avg_win_r": None, "avg_loss_r": None,
                "pnl": 0.0, "pnl_pct": 0.0, "max_dd_pct": 0.0, "reasons": {},
                "long_pnl": 0.0, "short_pnl": 0.0, "long_n": 0, "short_n": 0,
                "rs": [], "pnls": [],
            })
            continue
        room = balance * leverage
        each = room / len(priced)
        positions = []
        for pair, entry_px, _exit_px, path in priced:
            entry = _worse("long", entry_px, closing=False, slip=SLIP)
            notional = round(each, 2)
            fee = notional * TAKER
            balance = round(balance - fee, 2)
            positions.append({"pair": pair, "entry": entry, "notional": notional, "fee": fee, "path": path})
        peak = INITIAL
        max_dd = 0.0
        # Mark on the shared index is not aligned across pairs. Use each
        # path's own closes, stepping the shortest one. Good enough for a beta check.
        steps = min(len(p["path"]) for p in positions)
        for k in range(steps):
            unreal = 0.0
            for pos in positions:
                px = pos["path"][k]
                unreal += (px - pos["entry"]) / pos["entry"] * pos["notional"]
            equity = balance + unreal
            if equity > peak:
                peak = equity
            max_dd = max(max_dd, peak - equity)
        pnls = []
        for pos in positions:
            exit_px = _worse("long", pos["path"][-1], closing=True, slip=SLIP)
            gross = (exit_px - pos["entry"]) / pos["entry"] * pos["notional"]
            exit_fee = pos["notional"] * TAKER
            net = round(gross - exit_fee - pos["fee"], 2)
            balance = round(balance + gross - exit_fee, 2)
            pnls.append(net)
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]
        gross_win = sum(wins)
        gross_loss = -sum(losses)
        pf = (gross_win / gross_loss) if gross_loss > 0 else None
        rows.append({
            "name": name,
            "trades": len(pnls),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": (len(wins) / len(pnls)) if pnls else None,
            "pf": None if pf is None else round(pf, 4),
            "expectancy": None,
            "avg_win_r": None,
            "avg_loss_r": None,
            "pnl": round(balance - INITIAL, 2),
            "pnl_pct": round((balance - INITIAL) / INITIAL * 100.0, 2),
            "max_dd_pct": round((max_dd / peak * 100.0) if peak else 0.0, 2),
            "reasons": {"hold": len(pnls)},
            "long_pnl": round(sum(pnls), 2),
            "short_pnl": 0.0,
            "long_n": len(pnls),
            "short_n": 0,
            "rs": [],
            "pnls": pnls,
        })
    return rows


def main():
    only = [arg.split("=", 1)[1] for arg in sys.argv[1:] if arg.startswith("--only=")]
    calibrate = "--calibrate" in sys.argv
    end_open = ws._last_open_ms()
    end_close = end_open + BAR_MS
    print("load market", flush=True)
    market = ws.load_market(ws.EXTENDED, ws._ms(ws.FETCH_START), end_open)
    funding = _load_funding()
    series = {pair: _series(market[pair].m1) for pair in ws.EXTENDED}
    slices = ws.slices_of(end_close)
    print("slices %d %s → %s" % (
        len(slices), slices[0][0], slices[-1][0],
    ), flush=True)
    for name, lo, hi in slices:
        print("  %s %s → %s" % (name, ws.backtest.madrid(lo), ws.backtest.madrid(hi)), flush=True)
    specs = list(CONFIGS)
    if only:
        specs = [spec for spec in CONFIGS if spec["name"] in only]
    if calibrate:
        specs = [spec for spec in CONFIGS if spec["name"] == "sweep_ab"]
    print("course signals", flush=True)
    course_events, indicators = prepare(market, ws.EXTENDED, end_close, funding)
    trend_events, trend_states = {}, {}
    if any(spec["family"] == "trend" for spec in specs):
        print("trend signals", flush=True)
        trend_events, trend_states = prepare_trend(market, ws.EXTENDED, end_close)
        for spec in CONFIGS:
            if spec["family"] == "trend":
                spec["_state"] = trend_states[spec["name"]]
    clock = clock_of(series)
    print("clock %d configs %d" % (len(clock), len(specs)), flush=True)
    universes = (("core12", ws.CORE), ("extended28", ws.EXTENDED))
    results = {spec["name"]: {} for spec in specs}
    for universe, pairs in universes:
        sub = {pair: series[pair] for pair in pairs}
        for spec in specs:
            events = _filter(spec, course_events, trend_events, pairs)
            print("%s %s events %d" % (universe, spec["name"], len(events)), flush=True)
            results[spec["name"]][universe] = _run_spec(spec, sub, events, indicators, slices)
    if calibrate:
        for universe, _pairs in universes:
            row = next(r for r in results["sweep_ab"][universe] if r["name"] == "S02")
            target = CALIBRATE[universe]
            print("CALIBRATE %s trades %d (published %d) pf %s (published %s) pnl%% %s (published %s) E[R] %s win %s" % (
                universe, row["trades"], target["trades"], row["pf"], target["pf"],
                row["pnl_pct"], target["pnl_pct"], row["expectancy"], row["win_rate"],
            ), flush=True)
            print("  reasons", row["reasons"], "avg", row["avg_win_r"], row["avg_loss_r"], flush=True)
        return
    report = {"configs": len(CONFIGS), "holdout_from": HOLDOUT_FROM, "universes": {}}
    for universe, pairs in universes:
        block = nested_and_holdout(
            {spec["name"]: results[spec["name"]][universe] for spec in CONFIGS},
            slices,
        )
        pre_names = [name for name, _lo, _hi in slices if "S02" <= name < HOLDOUT_FROM]
        hold_names = [name for name, _lo, _hi in slices if name >= HOLDOUT_FROM]
        fixed = []
        trial_sharpes = []
        for spec in CONFIGS:
            rows = _rows_named(results[spec["name"]][universe], pre_names)
            summary = _attach_rl(pool(rows), rows)
            summary["label"] = spec["name"]
            summary["role"] = "fixed_pre_holdout"
            sharpe = trade_sharpe(summary.get("rs") or [])
            trial_sharpes.append(None if sharpe is None else sharpe["sharpe"])
            summary["sharpe"] = None if sharpe is None else round(sharpe["sharpe"], 4)
            fixed.append(summary)
        stitched = _attach_rl(pool(block["stitched"]), block["stitched"])
        stitched["label"] = "nested_rule"
        stitched_sharpe = trade_sharpe(stitched.get("rs") or [])
        dsr = deflated_sharpe(stitched_sharpe, trial_sharpes)
        hold = _attach_rl(pool(block["hold_rows"]), block["hold_rows"])
        hold["label"] = "frozen_%s_holdout" % block["frozen"]
        monitor_pre = next(row for row in fixed if row["label"] == "sweep_ab")
        monitor_hold_rows = _rows_named(results["sweep_ab"][universe], hold_names)
        monitor_hold = _attach_rl(pool(monitor_hold_rows), monitor_hold_rows)
        monitor_hold["label"] = "sweep_ab_holdout"
        bh = buy_and_hold(series, pairs, clock, slices, leverage=3)
        bh_pre = _attach_rl(pool(_rows_named(bh, pre_names)), _rows_named(bh, pre_names))
        bh_pre["label"] = "buyhold_3x_pre"
        bh_hold = _attach_rl(pool(_rows_named(bh, hold_names)), _rows_named(bh, hold_names))
        bh_hold["label"] = "buyhold_3x_holdout"
        print("\n## %s" % universe, flush=True)
        print("frozen %s relaxed %s plateau %s" % (
            block["frozen"], block["frozen_relaxed"], ",".join(block["frozen_plateau"]),
        ), flush=True)
        print("DSR", dsr, flush=True)
        header = "| Config | Trades | Win | PF | E[R] | 95% CI | Avg win R | Avg loss R | PnL | Worst DD | Folds +"
        print(header)
        print("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
        for row in (monitor_pre, stitched, hold, monitor_hold, bh_pre, bh_hold):
            print(render_summary(row))
        print("choices", flush=True)
        for choice in block["choices"]:
            print("  %s -> %s%s" % (
                choice["test"], choice["chosen"], " (relaxed)" if choice["relaxed"] else "",
            ), flush=True)
        report["universes"][universe] = {
            "frozen": block["frozen"],
            "frozen_relaxed": block["frozen_relaxed"],
            "frozen_plateau": block["frozen_plateau"],
            "choices": block["choices"],
            "dsr": dsr,
            "nested": _public(stitched),
            "frozen_holdout": _public(hold),
            "frozen_pre_holdout_insample": _public(_attach_rl(pool(block["pre_rows"]), block["pre_rows"])),
            "monitor_pre": _public(monitor_pre),
            "monitor_holdout": _public(monitor_hold),
            "buyhold_pre": _public(bh_pre),
            "buyhold_holdout": _public(bh_hold),
            "fixed_pre_holdout": [_public(row) for row in sorted(fixed, key=lambda r: -(r["pnl"] or 0))],
            "train_rank": block["frozen_train"],
        }
    dest = Path("/tmp/granmago-v160.json")
    dest.write_text(json.dumps(report), encoding="utf-8")
    print("wrote", dest, flush=True)


if __name__ == "__main__":
    main()
