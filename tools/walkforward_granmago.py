#!/usr/bin/env python3
"""Out-of-sample comparison of the scanner and the two Gran Mago variants.

Same six-month Blofin window, same three anchored folds and the same
0.06% taker plus 0.02% slippage per side as tools/walkforward.py. The
scanner row is the 1.4.0 running book (threshold 95). The course rows use
risk 2%, leverage 3 and at most three positions. Adds, when enabled, are
one extra fill at the swept extreme. Nothing here is chosen by looking at
the test windows.
"""

import json
import os
import pickle
import sys
import time
from datetime import datetime
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(TOOLS.parent / "granmago"))

import backtest  # noqa: E402
import granmago_strategy as course  # noqa: E402
import server  # noqa: E402
import walkforward as wf  # noqa: E402

MADRID = backtest.MADRID
MIN_MS = backtest.MIN_MS
INITIAL = backtest.INITIAL_BALANCE
CACHE_1M = Path("/tmp/granmago-bt-cache/BTC-USDT-1m.json")
SIGNAL_CACHE = wf.SIGNAL_CACHE


def pinned_end_ms():
    """Last cached minute close, so this run reuses the 1.4.0 candles."""
    payload = json.loads(CACHE_1M.read_text(encoding="utf-8"))
    rows = payload["rows"]
    return int(rows[-1]["ts"]) + MIN_MS


def _avg(values):
    if not values:
        return None
    return sum(values) / len(values)


def _with_r(metrics, trades):
    win_r = [t["r"] for t in trades if t.get("r") is not None and t.get("pnl") is not None and t["pnl"] > 0]
    loss_r = [t["r"] for t in trades if t.get("r") is not None and t.get("pnl") is not None and t["pnl"] < 0]
    avg_win = _avg(win_r)
    avg_loss = _avg(loss_r)
    ratio = None
    if avg_win is not None and avg_loss not in (None, 0):
        ratio = avg_win / abs(avg_loss)
    metrics["avg_win_r"] = None if avg_win is None else round(avg_win, 4)
    metrics["avg_loss_r"] = None if avg_loss is None else round(avg_loss, 4)
    metrics["win_loss_ratio"] = None if ratio is None else round(ratio, 4)
    return metrics


def _record(variant, pair, decision_ms):
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


def prepare(market, end_ms):
    """Signals and confirmation-bar snapshots for both variants. One pass each."""
    prepared = {}
    for pair in server.PAIRS:
        data = market[pair]
        c4 = course.drop_forming(data.h4, course.H4_MS, end_ms)
        daily = course.drop_forming(data.d1, course.DAY_MS, end_ms)
        weekly = course.drop_forming(data.w1, course.WEEK_MS, end_ms)
        prepared[pair] = {}
        for variant in ("a", "b"):
            signals, bars = course.find_signals(
                c4, daily, weekly, variant=variant, min_rr=0.0, stop_pad_pct=0.3, filters=True,
            )
            by_close = {}
            for signal in signals:
                signal = dict(signal)
                signal["pair"] = pair
                by_close[signal["closeMs"]] = signal
            prepared[pair][variant] = {"signals": by_close, "bars": bars}
        print("  %s A %d  B %d" % (
            pair, len(prepared[pair]["a"]["signals"]), len(prepared[pair]["b"]["signals"]),
        ), flush=True)
    return prepared


def _course_settings(variant, max_adds):
    return {
        "strategy": "granmago",
        "granmagoVariant": variant,
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


def replay_course(name, variant_name, max_adds, prepared, market, clock, t0, t1):
    variant = wf._make_variant(name, _course_settings(variant_name, max_adds))
    book = prepared
    prev_close = {pair: None for pair in server.PAIRS}
    for open_ts in clock:
        decision = open_ts + MIN_MS
        if decision >= t0:
            break
        for pair in server.PAIRS:
            bar = market[pair].minute(open_ts)
            if bar is not None:
                prev_close[pair] = bar["c"]
    for n, open_ts in enumerate(clock):
        decision = open_ts + MIN_MS
        if decision < t0:
            continue
        if decision >= t1:
            break
        server._clock = lambda d=decision: d / 1000.0
        bars = {}
        for pair in server.PAIRS:
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
            snap = book[pair][variant_name]["bars"].get(decision)
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
            _record(variant, pair, decision)
        occupied = {p.get("pair") for p in variant.state["openPositions"]}
        if len(occupied) < variant.max_positions:
            for pair in server.PAIRS:
                if len(variant.state["openPositions"]) >= variant.max_positions:
                    break
                signal = book[pair][variant_name]["signals"].get(decision)
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
        if n and n % 40000 == 0:
            print("    %s %d/%d open %d" % (
                name, n, len(clock), len(variant.state["openPositions"]),
            ), flush=True)
    server._clock = None
    summary = backtest.summarize(variant, t1)
    unreal = sum(float(p.get("unrealized") or 0.0) for p in summary["open"])
    metrics = wf._metrics(
        variant.trades, summary["balance"], summary["equity"], summary["max_dd_pct"],
        len(summary["open"]), unreal,
    )
    metrics["name"] = name
    metrics["reasons"] = summary["reasons"]
    return _with_r(metrics, variant.trades)


def _load_scanner_signals():
    if not SIGNAL_CACHE.exists():
        raise SystemExit("missing scanner signal cache %s" % SIGNAL_CACHE)
    payload = pickle.loads(SIGNAL_CACHE.read_bytes())
    print("scanner signal cache %s key %s" % (SIGNAL_CACHE, payload.get("key")), flush=True)
    return payload["signals"]


def _fmt_row(row):
    win = ("%.0f%%" % (row["win_rate"] * 100)) if row.get("win_rate") is not None else "-"
    ratio = row.get("win_loss_ratio")
    ratio_txt = "-" if ratio is None else "%.2f" % ratio
    return "| %s | %s | %d | %s | %s | %s | %s | %s | %s | %s | %d |" % (
        row["name"],
        row.get("window") or "",
        row["trades"],
        win,
        wf._fmt_pf(row.get("pf")),
        wf._fmt_num(row.get("expectancy"), 3),
        ratio_txt,
        wf._fmt_num(row.get("pnl_pct")),
        wf._fmt_num(row.get("max_dd_pct")),
        wf._fmt_num(row.get("avg_win_r"), 3),
        row.get("open") or 0,
    )


def main():
    end_ms = pinned_end_ms()
    start_ms = wf._ms(datetime(2026, 4, 23, 0, 0, tzinfo=MADRID))
    print("window %s → %s Madrid" % (backtest.madrid(start_ms), backtest.madrid(end_ms)), flush=True)
    market = backtest.load_market(start_ms, end_ms - MIN_MS)
    btc = market["BTC-USDT"]
    clock = []
    open_ts = start_ms - (start_ms % MIN_MS)
    last_open = btc.m1[-1]["ts"]
    while open_ts <= last_open:
        decision = open_ts + MIN_MS
        if decision >= start_ms and decision <= end_ms and open_ts in btc.m1_by_ts:
            clock.append(open_ts)
        open_ts += MIN_MS
    print("minutes %d" % len(clock), flush=True)
    print("course signals", flush=True)
    prepared = prepare(market, end_ms)
    _start, slices = wf.slice_bounds(end_ms)
    plan = wf.folds_from(slices)
    for name, lo, hi in slices:
        print("  %s %s → %s" % (name, backtest.madrid(lo), backtest.madrid(hi)), flush=True)

    scanner = _load_scanner_signals()
    covered = 0
    for open_ts in clock:
        if open_ts in scanner.get("BTC-USDT", {}):
            covered += 1
    print("scanner cache covers %d/%d minutes" % (covered, len(clock)), flush=True)

    runs = [
        ("current_thr95", "scanner", None),
        ("granmago_a", "a", 0),
        ("granmago_b", "b", 0),
        ("granmago_a_add1", "a", 1),
        ("granmago_b_add1", "b", 1),
    ]
    rows = []
    current_settings = dict(wf.LIVE, entryThreshold=95)
    for fold in plan:
        t0, t1 = fold["test"]
        print("fold %s test %s → %s" % (
            fold["name"], backtest.madrid(t0), backtest.madrid(t1),
        ), flush=True)
        for name, kind, adds in runs:
            started = time.time()
            if kind == "scanner":
                metrics = wf.replay(name, current_settings, scanner, market, clock, t0, t1)
            else:
                metrics = replay_course(name, kind, adds, prepared, market, clock, t0, t1)
            metrics["window"] = fold["name"] + " test"
            metrics["role"] = "test"
            rows.append(metrics)
            print("  %-16s trades %3d win %s pf %s E[R] %s avgW %s avgL %s ratio %s pnl %s dd %s  %.0fs" % (
                name, metrics["trades"],
                ("%.0f%%" % (metrics["win_rate"] * 100)) if metrics.get("win_rate") is not None else "-",
                wf._fmt_pf(metrics.get("pf")),
                wf._fmt_num(metrics.get("expectancy"), 3),
                wf._fmt_num(metrics.get("avg_win_r"), 3),
                wf._fmt_num(metrics.get("avg_loss_r"), 3),
                wf._fmt_num(metrics.get("win_loss_ratio"), 2),
                wf._fmt_num(metrics.get("pnl_pct")),
                wf._fmt_num(metrics.get("max_dd_pct")),
                time.time() - started,
            ), flush=True)
            print("    reasons %s" % (metrics.get("reasons") or {}), flush=True)

    # Scanner rows were filled without per-trade R. Recompute those three folds
    # is expensive; do it once here only if the ratio is missing, using the
    # same replay but we need the trades. Handled by replay_scanner below if
    # the first pass left them empty. See main's scanner branch note.
    out = {
        "end": backtest.madrid(end_ms),
        "fees": "0.06% taker + 0.02% slippage per side",
        "rows": rows,
        "note": (
            "current_thr95 is the 1.4.0 running book (threshold 95, leverage 5, "
            "trailing distance 0.25, max 3). Course rows use leverage 3, risk 2%, "
            "max 3, no trailing. Folds are the same anchored test windows."
        ),
    }
    dest = Path("/tmp/granmago-walkforward-1.5.0.json")
    dest.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print("wrote %s" % dest, flush=True)
    header = "| Config | Window | Trades | Win | PF | E[R] | Avg win/|loss| | PnL % | Max DD | Avg win R | Open |"
    print(header)
    print("|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for row in rows:
        print(_fmt_row(row))


if __name__ == "__main__":
    main()
