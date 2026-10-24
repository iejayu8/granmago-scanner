#!/usr/bin/env python3
"""Walk-forward of the relaxed sweep mode on a longer Blofin history.

Stops and the scanner are marked on closed 15-minute bars. Blofin has
1-minute candles back to 2023, but a 1-minute path for this many pairs
does not fit the run. Inside a 15-minute bar the path is the same
open-to-extreme rule the 1-minute study uses inside one minute. Fees stay
0.06% taker plus 0.02% slippage per side.

Folds are 45 days, the same length as the 1.4.0 slices, anchored from
22 Jan 2024 (Madrid). The first slice is train-only context and is not a
test. Nothing here was chosen by looking at these test windows. The level
rules were fixed from the gate-rejection counts on the earlier six-month
cache.

The extended universe is the bot's 12 pairs plus the next liquid USDT
swaps by 24h quote volume on 23 Oct 2026 that were listed on or before
23 Oct 2024. Tokenized equities, metals, and newer listings were left out.
"""

import json
import os
import random
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

os.environ["BACKTEST_CACHE"] = "/tmp/granmago-bt-cache-long"

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(TOOLS.parent / "granmago"))

import backtest  # noqa: E402
import granmago_strategy as course  # noqa: E402
import server  # noqa: E402
import walkforward as wf  # noqa: E402

MADRID = backtest.MADRID
BAR_MS = 15 * 60 * 1000
INITIAL = backtest.INITIAL_BALANCE
FETCH_START = datetime(2023, 1, 12, 0, 0, tzinfo=backtest.timezone.utc)
FOLD_START = datetime(2024, 1, 22, 0, 0, tzinfo=MADRID)
FOLD_DAYS = 45

# Frozen before this run. CORE is the original 12-pair book. EXTENDED is
# the 28-pair liquid book (server.LIQUID_PAIRS), which is the add-on
# default since 1.5.1. This study still runs both.
CORE = list(server.DEFAULT_PAIRS)
EXTENDED = list(server.LIQUID_PAIRS)

# name, kind, variant, setups, max_adds. Fixed before the run.
RUNS = [
    ("scanner_thr95", "scanner", None, None, None),
    ("sweep_a", "course", "a", "sweep", 0),
    ("sweep_a_add1", "course", "a", "sweep", 1),
    ("sweep_a_pull", "course", "a", "both", 0),
    ("sweep_a_pull_add1", "course", "a", "both", 1),
    ("sweep_b", "course", "b", "sweep", 0),
    ("sweep_b_add1", "course", "b", "sweep", 1),
    ("sweep_b_pull", "course", "b", "both", 0),
    ("sweep_b_pull_add1", "course", "b", "both", 1),
    ("strict_a", "course", "a", "both", 0),
    ("strict_b", "course", "b", "both", 0),
]


def _ms(dt):
    return int(dt.timestamp() * 1000)


def _last_open_ms():
    now = int(time.time() * 1000)
    boundary = now - (now % BAR_MS)
    return boundary - BAR_MS


def load_market(pairs, start_ms, end_ms):
    from concurrent.futures import ThreadPoolExecutor, as_completed

    def one(pair):
        print("fetch %s" % pair, flush=True)
        last = None
        m15 = h4 = d1 = w1 = None
        for attempt in range(4):
            try:
                m15 = backtest.fetch_blofin(pair, "15m", start_ms, end_ms, allow_short=True)
                h4 = backtest.fetch_blofin(pair, "4H", start_ms, end_ms, allow_short=True)
                d1 = backtest.fetch_blofin(pair, "1D", start_ms, end_ms, allow_short=True)
                w1 = backtest.fetch_blofin(pair, "1W", start_ms, end_ms, allow_short=True)
                last = None
                break
            except RuntimeError as exc:
                last = exc
                print("  retry %s (%s)" % (pair, exc), flush=True)
                time.sleep(2 * (attempt + 1))
        if last is not None:
            raise last
        print("  %s 15m %d (%s → %s) 4H %d 1D %d 1W %d" % (
            pair, len(m15),
            backtest.madrid(m15[0]["ts"]),
            backtest.madrid(m15[-1]["ts"] + BAR_MS),
            len(h4), len(d1), len(w1),
        ), flush=True)
        return pair, backtest.PairData(pair, m15, h4, d1, w1)

    market = {}
    workers = min(4, len(pairs))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = [pool.submit(one, pair) for pair in pairs]
        for fut in as_completed(futs):
            pair, data = fut.result()
            market[pair] = data
    return market


def slices_of(end_close_ms):
    slices = []
    i = 0
    while i < 40:
        lo = FOLD_START + timedelta(days=FOLD_DAYS * i)
        hi = FOLD_START + timedelta(days=FOLD_DAYS * (i + 1))
        lo_ms = _ms(lo)
        hi_ms = _ms(hi)
        if lo_ms >= end_close_ms:
            break
        hi_ms = min(hi_ms, end_close_ms + 1)
        if hi_ms <= lo_ms:
            break
        slices.append(("S%02d" % (i + 1), lo_ms, hi_ms))
        i += 1
    return slices


def _course_settings(max_adds):
    return {
        "strategy": "granmago",
        "granmagoVariant": "a",
        "granmagoStyle": "sweep",
        "granmagoSetups": "both",
        "granmagoMaxAdds": max_adds,
        "granmagoMomentumExit": True,
        "granmagoMaxHoldBars": 15,
        "granmagoBreakevenR": 0.0,
        "granmagoMinRr": 0.0,
        "granmagoStopPadPct": 0.3,
        "riskPct": 2.0,
        "leverage": 3,
        "maxPositions": 3,
        "trailingEnabled": False,
        "trailingActivationPct": 1.0,
        "trailingActivationR": 0.0,
        "trailingKeepTp": False,
        "trailingDistance": 0.5,
        "entryThreshold": 100,
        "feePctPerSide": 0.06,
        "slippagePctPerSide": 0.02,
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
    }


def prepare(market, pairs, end_ms):
    prepared = {}
    for pair in pairs:
        data = market[pair]
        c4 = course.drop_forming(data.h4, course.H4_MS, end_ms)
        daily = course.drop_forming(data.d1, course.DAY_MS, end_ms)
        weekly = course.drop_forming(data.w1, course.WEEK_MS, end_ms)
        book = {"signals": {}}
        for variant in ("a", "b"):
            for style, setups, key in (
                ("sweep", "sweep", "sweep_%s" % variant),
                ("sweep", "both", "sweep_%s_both" % variant),
                ("strict", "both", "strict_%s" % variant),
            ):
                signals, bars = course.find_signals(
                    c4, daily, weekly, variant=variant, min_rr=0.0,
                    stop_pad_pct=0.3, filters=True, style=style, setups=setups,
                )
                book["bars_%s" % variant] = bars
                by_close = {}
                for signal in signals:
                    signal = dict(signal)
                    signal["pair"] = pair
                    by_close[signal["closeMs"]] = signal
                book["signals"][key] = by_close
        prepared[pair] = book
        counts = " ".join("%s %d" % (key, len(book["signals"][key])) for key in book["signals"])
        print("  %s %s" % (pair, counts), flush=True)
    return prepared


_MARKET = None
_CLOCK = None
_PAIRS = None


def _score_pair(pair):
    data = _MARKET[pair]
    clock = _CLOCK
    server.STATE = {"settings": {"stopAtrPeriod": 14}}
    out = {}
    total = len(clock)
    for n, open_ts in enumerate(clock):
        if data.minute(open_ts) is None:
            continue
        decision = open_ts + BAR_MS
        c4, cd, cw = data.window(decision)
        row = server.score_market(pair, c4, cd, cw, 0)
        if row is None:
            continue
        if row["scoreLong"] >= 95 or row["scoreShort"] >= 95:
            out[open_ts] = wf._slim(row)
        if n and n % 20000 == 0:
            print("  score %s %d/%d" % (pair, n, total), flush=True)
    return pair, out


def score_scanner(market, pairs, clock):
    global _MARKET, _CLOCK, _PAIRS
    import multiprocessing
    import pickle
    cache = Path("/tmp/granmago-sweep-scores-15m.pkl")
    key = (clock[0], clock[-1], len(clock), tuple(pairs), "15m-thr95")
    if cache.exists():
        payload = pickle.loads(cache.read_bytes())
        if payload.get("key") == key:
            print("score cache hit", flush=True)
            return payload["signals"]
    started = time.time()
    _MARKET = market
    _CLOCK = clock
    _PAIRS = pairs
    signals = {}
    ctx = multiprocessing.get_context("fork")
    workers = min(4, len(pairs), os.cpu_count() or 1)
    print("scoring %d x %d on %d workers" % (len(clock), len(pairs), workers), flush=True)
    with ctx.Pool(workers) as pool:
        for pair, rows in pool.imap_unordered(_score_pair, pairs):
            signals[pair] = rows
            print("  scored %s hot %d %.0fs" % (pair, len(rows), time.time() - started), flush=True)
    cache.write_bytes(pickle.dumps({"key": key, "signals": signals}))
    print("wrote %s" % cache, flush=True)
    return signals


def _prime(clock, market, pairs, t0):
    """Last close before the test window. One bar, not the whole history."""
    import bisect
    prev = {pair: None for pair in pairs}
    idx = bisect.bisect_left(clock, t0 - BAR_MS)
    if idx <= 0:
        return prev
    open_ts = clock[idx - 1]
    for pair in pairs:
        bar = market[pair].minute(open_ts)
        if bar is not None:
            prev[pair] = bar["c"]
    return prev


def replay_scanner(name, settings, signals, market, pairs, clock, t0, t1):
    variant = wf._make_variant(name, settings)
    prev_close = _prime(clock, market, pairs, t0)
    for open_ts in clock:
        decision = open_ts + BAR_MS
        if decision < t0:
            continue
        if decision >= t1:
            break
        server._clock = lambda d=decision: d / 1000.0
        bars = {}
        for pair in pairs:
            bar = market[pair].minute(open_ts)
            if bar is not None:
                bars[pair] = bar
        for pos in list(variant.state["openPositions"]):
            pair = pos.get("pair")
            if pair in bars:
                variant.on_bar(pair, bars[pair], prev_close[pair], decision)
        rows = []
        for pair in pairs:
            slim = signals.get(pair, {}).get(open_ts)
            if slim is not None and (slim[0] >= 95 or slim[1] >= 95):
                rows.append(wf._row(pair, slim))
        if rows:
            variant.maybe_open(rows, decision)
        for pair, bar in bars.items():
            prev_close[pair] = bar["c"]
        if variant.state["openPositions"]:
            variant.note_equity(decision, sample=False)
    server._clock = None
    return _finish(name, variant, t1)


def replay_course(name, variant_name, setups, max_adds, prepared, market, pairs, clock, t0, t1):
    variant = wf._make_variant(name, _course_settings(max_adds))
    key = ("sweep_%s" % variant_name) if setups == "sweep" else (
        "strict_%s" % variant_name if name.startswith("strict") else "sweep_%s_both" % variant_name
    )
    if name.startswith("strict"):
        key = "strict_%s" % variant_name
    prev_close = _prime(clock, market, pairs, t0)
    for open_ts in clock:
        decision = open_ts + BAR_MS
        if decision < t0:
            continue
        if decision >= t1:
            break
        server._clock = lambda d=decision: d / 1000.0
        bars = {}
        for pair in pairs:
            bar = market[pair].minute(open_ts)
            if bar is not None:
                bars[pair] = bar
        for pos in list(variant.state["openPositions"]):
            pair = pos.get("pair")
            if pair in bars:
                variant.on_bar(pair, bars[pair], prev_close[pair], decision)
        variant.bind()
        for pos in list(variant.state["openPositions"]):
            pair = pos.get("pair")
            snap = prepared[pair]["bars_%s" % variant_name].get(decision)
            if snap is None or pos.get("strategy") != "granmago":
                continue
            if int(pos.get("confirmMs") or 0) != int(snap.get("confirmMs") or 0):
                continue
            decision_exit = course.exit_decision(pos, snap)
            if decision_exit is None:
                continue
            reason, price = decision_exit
            if server._find_position(pair) is None:
                continue
            server.close_position(reason, price, pair)
            _record_course(variant, pair, decision)
        occupied = {p.get("pair") for p in variant.state["openPositions"]}
        if len(occupied) < variant.max_positions:
            for pair in pairs:
                if len(variant.state["openPositions"]) >= variant.max_positions:
                    break
                signal = prepared[pair]["signals"][key].get(decision)
                if signal is None or pair in occupied:
                    continue
                if server.open_granmago(signal):
                    pos = server._find_position(pair)
                    variant._open_meta[pair] = {
                        "entry_ms": decision,
                        "score": None,
                        "capped": bool(pos and pos.get("notionalCapped")),
                        "tradeId": None if pos is None else pos.get("tradeId"),
                    }
                    occupied.add(pair)
        for pair, bar in bars.items():
            prev_close[pair] = bar["c"]
        if variant.state["openPositions"]:
            variant.note_equity(decision, sample=False)
    server._clock = None
    return _finish(name, variant, t1)


def _record_course(variant, pair, decision_ms):
    """on_bar records price exits. A signal exit has to be recorded here."""
    meta = variant._open_meta.pop(pair, {})
    hist = variant.state["history"][0] if variant.state["history"] else {}
    pnl = hist.get("tradePnl")
    if pnl is None:
        pnl = hist.get("pnl")
    r_mult = hist.get("tradeR")
    if r_mult is None:
        r_mult = hist.get("rMultiple")
    variant.trades.append({
        "pair": hist.get("pair", pair),
        "side": hist.get("side"),
        "entry_ms": meta.get("entry_ms"),
        "exit_ms": decision_ms,
        "entry": hist.get("entry"),
        "exit": hist.get("exit"),
        "reason": hist.get("reason"),
        "pnl": pnl,
        "r": r_mult,
        "score": meta.get("score"),
        "capped": bool(meta.get("capped")),
        "tradeId": hist.get("tradeId"),
    })


def _finish(name, variant, t1):
    summary = backtest.summarize(variant, t1)
    unreal = sum(float(p.get("unrealized") or 0.0) for p in summary["open"])
    metrics = wf._metrics(
        variant.trades, summary["balance"], summary["equity"], summary["max_dd_pct"],
        len(summary["open"]), unreal,
    )
    win_r = [t["r"] for t in variant.trades if t.get("r") is not None and t.get("pnl") is not None and t["pnl"] > 0]
    loss_r = [t["r"] for t in variant.trades if t.get("r") is not None and t.get("pnl") is not None and t["pnl"] < 0]
    avg_win = (sum(win_r) / len(win_r)) if win_r else None
    avg_loss = (sum(loss_r) / len(loss_r)) if loss_r else None
    metrics["avg_win_r"] = None if avg_win is None else round(avg_win, 4)
    metrics["avg_loss_r"] = None if avg_loss is None else round(avg_loss, 4)
    metrics["name"] = name
    metrics["reasons"] = summary["reasons"]
    metrics["rs"] = [t["r"] for t in variant.trades if t.get("r") is not None]
    metrics["pnls"] = [t["pnl"] for t in variant.trades if t.get("pnl") is not None]
    return metrics


def bootstrap_ci(values, draws=4000, seed=1500):
    values = [float(v) for v in values if v is not None]
    n = len(values)
    if n == 0:
        return {"n": 0, "mean": None, "lo": None, "hi": None}
    mean = sum(values) / n
    if n == 1:
        return {"n": 1, "mean": round(mean, 4), "lo": None, "hi": None}
    rng = random.Random(seed)
    means = []
    for _ in range(draws):
        total = 0.0
        for _j in range(n):
            total += values[rng.randrange(n)]
        means.append(total / n)
    means.sort()
    lo_i = int(0.025 * (draws - 1))
    hi_i = int(0.975 * (draws - 1))
    return {
        "n": n,
        "mean": round(mean, 4),
        "lo": round(means[lo_i], 4),
        "hi": round(means[hi_i], 4),
    }


def _public_row(row):
    out = dict(row)
    out.pop("rs", None)
    out.pop("pnls", None)
    return out


def _fmt_pct(win_rate):
    if win_rate is None:
        return "-"
    return "%.0f%%" % (win_rate * 100)


def render_fold(row):
    return "| %s | %s | %d | %s | %s | %s | %s | %s | %s | %s |" % (
        row["name"],
        row.get("window") or "",
        row["trades"],
        _fmt_pct(row.get("win_rate")),
        wf._fmt_pf(row.get("pf")),
        wf._fmt_num(row.get("expectancy"), 3),
        wf._fmt_num(row.get("avg_win_r"), 2),
        wf._fmt_num(row.get("avg_loss_r"), 2),
        wf._fmt_num(row.get("pnl_pct")),
        wf._fmt_num(row.get("max_dd_pct")),
    )


def main():
    end_open = _last_open_ms()
    end_close = end_open + BAR_MS
    start_ms = _ms(FETCH_START)
    print("fetch %s → %s" % (FETCH_START.isoformat(), backtest.madrid(end_close)), flush=True)
    market = load_market(EXTENDED, start_ms, end_open)
    btc = market["BTC-USDT"].m1
    clock = [bar["ts"] for bar in btc if bar["ts"] + BAR_MS >= _ms(FOLD_START) and bar["ts"] <= end_open]
    print("15m bars in the fold span %d" % len(clock), flush=True)
    print("course signals", flush=True)
    prepared = prepare(market, EXTENDED, end_close)
    print("scanner scores", flush=True)
    scores = score_scanner(market, EXTENDED, clock)
    fold_slices = slices_of(end_close)
    for name, lo, hi in fold_slices:
        print("  %s %s → %s" % (name, backtest.madrid(lo), backtest.madrid(hi)), flush=True)
    plan = wf.folds_from(fold_slices)
    scanner_settings = dict(wf.LIVE, entryThreshold=95)
    universes = (("core12", CORE), ("extended28", EXTENDED))
    # strict rows only on the bot's 12 pairs. The extended book is the sweep test.
    core_only = {"strict_a", "strict_b"}
    rows = []
    for universe, pairs in universes:
        for fold in plan:
            t0, t1 = fold["test"]
            print("fold %s %s" % (universe, fold["name"]), flush=True)
            for name, kind, variant, setups, adds in RUNS:
                if universe != "core12" and name in core_only:
                    continue
                started = time.time()
                if kind == "scanner":
                    metrics = replay_scanner(name, scanner_settings, scores, market, pairs, clock, t0, t1)
                else:
                    metrics = replay_course(
                        name, variant, setups, adds, prepared, market, pairs, clock, t0, t1,
                    )
                metrics["window"] = fold["name"].split("test_")[-1]
                metrics["role"] = "test"
                metrics["universe"] = universe
                rows.append(metrics)
                print("  %-18s trades %4d win %s pf %s E[R] %s pnl %s dd %s %.0fs" % (
                    name, metrics["trades"], _fmt_pct(metrics.get("win_rate")),
                    wf._fmt_pf(metrics.get("pf")), wf._fmt_num(metrics.get("expectancy"), 3),
                    wf._fmt_num(metrics.get("pnl_pct")), wf._fmt_num(metrics.get("max_dd_pct")),
                    time.time() - started,
                ), flush=True)
    pooled = []
    for universe, _pairs in universes:
        names = []
        for row in rows:
            if row["universe"] == universe and row["name"] not in names:
                names.append(row["name"])
        for name in names:
            group = [r for r in rows if r["universe"] == universe and r["name"] == name]
            rs = [value for r in group for value in r["rs"]]
            pnls = [value for r in group for value in r["pnls"]]
            ci = bootstrap_ci(rs)
            wins = [p for p in pnls if p > 0]
            losses = [p for p in pnls if p < 0]
            gross_win = sum(wins)
            gross_loss = -sum(losses)
            if gross_loss > 0:
                pf = gross_win / gross_loss
            elif gross_win > 0:
                pf = None
            else:
                pf = None
            pooled.append({
                "universe": universe,
                "name": name,
                "trades": len(rs),
                "win_rate": (len(wins) / len(pnls)) if pnls else None,
                "pf": None if pf is None else round(pf, 4),
                "expectancy": ci["mean"],
                "expectancy_lo": ci["lo"],
                "expectancy_hi": ci["hi"],
                "pnl_pct_sum": round(sum(r["pnl_pct"] for r in group), 2),
                "worst_dd_pct": round(max(r["max_dd_pct"] for r in group), 2) if group else None,
                "positive_folds": sum(1 for r in group if r.get("expectancy") is not None and r["expectancy"] > 0),
                "folds": len(group),
            })
            print("POOLED %s %-18s n %d E[R] %s CI %s..%s" % (
                universe, name, ci["n"], ci["mean"], ci["lo"], ci["hi"],
            ), flush=True)
    header = "| Config | Fold | Trades | Win | PF | E[R] | Avg win R | Avg loss R | PnL % | Max DD |"
    print(header)
    print("|---|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for row in rows:
        print(render_fold(row))
    out = {
        "fees": "0.06% taker + 0.02% slippage per side",
        "bar": "15m",
        "fold_days": FOLD_DAYS,
        "fold_start": FOLD_START.isoformat(),
        "fetch_start": FETCH_START.isoformat(),
        "end": backtest.madrid(end_close),
        "core": CORE,
        "extended": EXTENDED,
        "slices": [
            {"name": name, "start": backtest.madrid(lo), "end": backtest.madrid(hi)}
            for name, lo, hi in fold_slices
        ],
        "pooled": pooled,
        "rows": [_public_row(row) for row in rows],
        "note": (
            "Each test fold resets to 1000 USDT. E[R] CI is a 4000-draw bootstrap "
            "of the pooled out-of-sample trade R, seed 1500, 2.5 and 97.5 percentiles. "
            "scanner_thr95 is the 1.4.0 book (threshold 95, leverage 5, trailing). "
            "Course rows use leverage 3, risk 2%, max 3 positions, no trailing. "
            "Marks are 15-minute bars."
        ),
    }
    dest = Path("/tmp/granmago-sweep-wf.json")
    dest.write_text(json.dumps(out), encoding="utf-8")
    print("wrote %s" % dest, flush=True)


if __name__ == "__main__":
    main()
