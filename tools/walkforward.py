#!/usr/bin/env python3
"""Walk-forward test of a small, pre-specified set of GranMago configs.

Candles come from Blofin, the add-on's first source. Scoring uses
server.score_market on each closed 1-minute bar, with the same no-lookahead
window as tools/backtest.py. Exits use the live server path (fees, slippage,
partials, trailing, cooldown, regime).

Folds are anchored. A preset is chosen on the train window by closed-trade
expectancy in R, with a minimum trade count, and that choice is scored only
on the following test window. The no-fee run is a diagnostic and is never
chosen. Nothing here is tuned on the test windows.
"""

import json
import multiprocessing
import os
import sys
import time
from datetime import datetime
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))

import backtest  # noqa: E402
import server  # noqa: E402

MADRID = backtest.MADRID
MIN_MS = backtest.MIN_MS
INITIAL = backtest.INITIAL_BALANCE
CACHE_VERSION = "1.4.0-1"
SIGNAL_CACHE = Path(os.environ.get(
    "WALKFORWARD_SIGNAL_CACHE", "/tmp/granmago-wf-cache/signals.pkl"))
MIN_TRAIN_TRADES = 15
# Below this many closed trades, an out-of-sample fold is too small to call an edge.
MIN_OOS_TRADES = 10

# Live book the user is running, plus the fee model under test.
# entry threshold 100 is the repaired score (stoch bonus required).
# Threshold 95 is the previous "score 100" set: full setup with or without the bonus.
LIVE = {
    "riskPct": 2.0,
    "leverage": 5,
    "trailingEnabled": True,
    "trailingActivationPct": 1.0,
    "trailingActivationR": 0.0,
    "trailingKeepTp": False,
    "trailingDistance": 0.25,
    "entryThreshold": 100,
    "maxPositions": 3,
    "feePctPerSide": 0.06,
    "slippagePctPerSide": 0.02,
    "takeProfitR": 1.7,
    "partialTpR": 0.0,
    "partialTpFraction": 0.5,
    "partialMoveBreakeven": True,
    "stopMode": "swing",
    "stopAtrPeriod": 14,
    "stopAtrMult": 1.5,
    "pairCooldownMinutes": 0,
    "maxSameDirectionRiskR": 0.0,
    "regimeFilter": "off",
    "regimeAdxMin": 20.0,
    "regimeErMin": 0.20,
}

# Named before any test result is known. Do not add a preset after looking at a fold.
PRESETS = [
    ("current_thr100", dict(LIVE, entryThreshold=100)),
    ("current_thr95", dict(LIVE, entryThreshold=95)),
    ("trail_1r_keep", dict(
        LIVE, entryThreshold=95, trailingActivationR=1.0, trailingKeepTp=True,
    )),
    ("partial_swing", dict(
        LIVE, entryThreshold=95, maxPositions=2, trailingEnabled=False,
        partialTpR=1.0, pairCooldownMinutes=240, maxSameDirectionRiskR=2.0,
    )),
    ("atr_partial", dict(
        LIVE, entryThreshold=95, maxPositions=2, trailingEnabled=False,
        stopMode="atr", stopAtrMult=1.5, takeProfitR=1.5, partialTpR=1.0,
        pairCooldownMinutes=240, maxSameDirectionRiskR=2.0,
    )),
    ("atr_partial_slope", dict(
        LIVE, entryThreshold=95, maxPositions=2, trailingEnabled=False,
        stopMode="atr", stopAtrMult=1.5, takeProfitR=1.5, partialTpR=1.0,
        pairCooldownMinutes=240, maxSameDirectionRiskR=2.0, regimeFilter="slope",
    )),
    ("atr_partial_adx", dict(
        LIVE, entryThreshold=95, maxPositions=2, trailingEnabled=False,
        stopMode="atr", stopAtrMult=1.5, takeProfitR=1.5, partialTpR=1.0,
        pairCooldownMinutes=240, maxSameDirectionRiskR=2.0, regimeFilter="adx",
    )),
    ("atr_partial_er", dict(
        LIVE, entryThreshold=95, maxPositions=2, trailingEnabled=False,
        stopMode="atr", stopAtrMult=1.5, takeProfitR=1.5, partialTpR=1.0,
        pairCooldownMinutes=240, maxSameDirectionRiskR=2.0, regimeFilter="er",
    )),
    ("wider_tp", dict(
        LIVE, entryThreshold=95, maxPositions=2, trailingEnabled=False,
        takeProfitR=2.5, pairCooldownMinutes=240, maxSameDirectionRiskR=2.0,
    )),
]
DIAGNOSTIC = ("current_thr95_nofee", dict(LIVE, entryThreshold=95, feePctPerSide=0.0, slippagePctPerSide=0.0))
ALL_RUNS = PRESETS + [DIAGNOSTIC]
SELECTABLE = {name for name, _ in PRESETS}


def _ms(dt):
    return int(dt.timestamp() * 1000)


def slice_bounds(data_end_ms):
    """Four contiguous slices covering 23 Apr 2026 through the last closed minute."""
    start = datetime(2026, 4, 23, 0, 0, tzinfo=MADRID)
    cuts = [
        datetime(2026, 4, 23, 0, 0, tzinfo=MADRID),
        datetime(2026, 6, 7, 0, 0, tzinfo=MADRID),
        datetime(2026, 7, 22, 0, 0, tzinfo=MADRID),
        datetime(2026, 9, 5, 0, 0, tzinfo=MADRID),
        datetime(2026, 10, 24, 0, 0, tzinfo=MADRID),
    ]
    # Exclusive ends. The last slice includes the final closed minute.
    end_cap = min(data_end_ms, _ms(cuts[-1])) + 1
    slices = []
    for i in range(4):
        lo = _ms(cuts[i])
        hi = _ms(cuts[i + 1]) if i < 3 else end_cap
        hi = min(hi, end_cap)
        if hi <= lo:
            continue
        slices.append(("S%d" % (i + 1), lo, hi))
    return start, slices


def folds_from(slices):
    """Anchored walk-forward: train is everything before the test slice."""
    by_name = {name: (lo, hi) for name, lo, hi in slices}
    plan = []
    for test_name in [s[0] for s in slices][1:]:
        test_i = [s[0] for s in slices].index(test_name)
        train_lo = slices[0][1]
        train_hi = slices[test_i - 1][2]
        plan.append({
            "name": "train_%s_test_%s" % ("".join(s[0] for s in slices[:test_i]), test_name),
            "train": (train_lo, train_hi),
            "test": by_name[test_name],
        })
    return plan


def select_preset(results, min_trades=MIN_TRAIN_TRADES, selectable=None):
    """Pick the train result with the highest expectancy in R.

    Presets under `min_trades` are ignored while any preset clears the bar.
    `relaxed` is True when the trade minimum had to be dropped. Diagnostic
    names that are not in `selectable` are never chosen. Returns (name, relaxed).
    """
    allowed = selectable if selectable is not None else SELECTABLE
    pool = [r for r in results if r["name"] in allowed and r.get("expectancy") is not None]
    if not pool:
        return None, True
    enough = [r for r in pool if r.get("trades", 0) >= min_trades]
    relaxed = not enough
    pool = enough or pool

    def key(row):
        exp = row["expectancy"]
        pf = row["pf"] if row.get("pf") is not None else -1.0
        return (exp, pf, -row.get("trades", 0))

    pool.sort(key=key, reverse=True)
    return pool[0]["name"], relaxed


def _metrics(trades, balance, equity, max_dd_pct, open_count, open_unreal):
    wins = [t for t in trades if t["pnl"] is not None and t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] is not None and t["pnl"] < 0]
    gross_win = sum(t["pnl"] for t in wins)
    gross_loss = -sum(t["pnl"] for t in losses)
    if gross_loss > 0:
        pf = gross_win / gross_loss
    elif gross_win > 0:
        pf = None
    else:
        pf = None
    rs = [t["r"] for t in trades if t.get("r") is not None]
    expectancy = (sum(rs) / len(rs)) if rs else None
    return {
        "trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": (len(wins) / len(trades)) if trades else None,
        "pf": None if pf is None else round(pf, 4),
        "expectancy": None if expectancy is None else round(expectancy, 4),
        "pnl": round(balance - INITIAL, 2),
        "pnl_pct": round((balance - INITIAL) / INITIAL * 100.0, 2),
        "equity_pct": round((equity - INITIAL) / INITIAL * 100.0, 2),
        "max_dd_pct": round(max_dd_pct, 2),
        "open": open_count,
        "open_unreal": round(open_unreal, 2),
    }


def _slim(row):
    return (
        row["scoreLong"],
        row["scoreShort"],
        row["bias"],
        row["price"],
        row["recentLow"],
        row["recentHigh"],
        row.get("atr"),
        row.get("adx"),
        row.get("er"),
        row.get("emaSlope"),
    )


def _row(pair, slim):
    score_l, score_s, bias, price, recent_low, recent_high, atr, adx, er, slope = slim
    return {
        "pair": pair,
        "scoreLong": score_l,
        "scoreShort": score_s,
        "bias": bias,
        "price": price,
        "recentLow": recent_low,
        "recentHigh": recent_high,
        "atr": atr,
        "adx": adx,
        "er": er,
        "emaSlope": slope,
    }


# Set in the parent before the fork. Workers read them and do not pickle the candles.
_MARKET = None
_CLOCK = None


def _score_pair(pair):
    data = _MARKET[pair]
    clock = _CLOCK
    # ATR period 14 matches every preset. Don't touch the parent's state.
    server.STATE = {"settings": {"stopAtrPeriod": 14}}
    out = {}
    mismatches = 0
    total = len(clock)
    for n, open_ts in enumerate(clock):
        bar = data.minute(open_ts)
        if bar is None:
            continue
        decision = open_ts + MIN_MS
        c4, cd, cw = data.window(decision)
        row = server.score_market(pair, c4, cd, cw, 0)
        if row is None:
            continue
        if abs(row["price"] - bar["c"]) > max(1e-6, abs(bar["c"]) * 1e-8):
            mismatches += 1
        out[open_ts] = _slim(row)
        if n and n % 20000 == 0:
            print("  score %s %d/%d" % (pair, n, total), flush=True)
    return pair, out, mismatches


def precompute(market, clock):
    global _MARKET, _CLOCK
    SIGNAL_CACHE.parent.mkdir(parents=True, exist_ok=True)
    key = (CACHE_VERSION, clock[0], clock[-1], len(clock), tuple(server.PAIRS))
    if SIGNAL_CACHE.exists():
        import pickle
        payload = pickle.loads(SIGNAL_CACHE.read_bytes())
        if payload.get("key") == key:
            print("signal cache hit %s" % SIGNAL_CACHE, flush=True)
            return payload["signals"], payload["mismatches"]
    started = time.time()
    signals = {}
    mismatches = 0
    _MARKET = market
    _CLOCK = clock
    ctx = multiprocessing.get_context("fork")
    workers = min(4, len(server.PAIRS), os.cpu_count() or 1)
    print("scoring %d minutes x %d pairs on %d workers" % (len(clock), len(server.PAIRS), workers), flush=True)
    with ctx.Pool(workers) as pool:
        for pair, rows, bad in pool.imap_unordered(_score_pair, server.PAIRS):
            signals[pair] = rows
            mismatches += bad
            print("  scored %s (%d bars, %d mismatches) %.0fs" % (
                pair, len(rows), bad, time.time() - started), flush=True)
    import pickle
    SIGNAL_CACHE.write_bytes(pickle.dumps({
        "key": key,
        "signals": signals,
        "mismatches": mismatches,
    }))
    print("wrote %s" % SIGNAL_CACHE, flush=True)
    return signals, mismatches


def _make_variant(name, settings):
    variant = backtest.Variant(
        name,
        settings["entryThreshold"],
        settings["trailingEnabled"],
        max_positions=settings["maxPositions"],
        activation=settings["trailingActivationPct"],
        distance=settings["trailingDistance"],
    )
    variant.state["settings"].update(settings)
    variant.state["balance"] = INITIAL
    variant.state["initialBalance"] = INITIAL
    variant.state["cooldowns"] = {}
    variant.state["tradeSeq"] = 0
    return variant


def replay(name, settings, signals, market, clock, t0, t1):
    """Closed-trade result of one preset on decisions in (t0, t1]."""
    variant = _make_variant(name, settings)
    prev_close = {pair: None for pair in server.PAIRS}
    # Prime the previous close so the first managed bar inside the window
    # still gaps from the real prior print. Nothing is open yet.
    for open_ts in clock:
        decision = open_ts + MIN_MS
        if decision >= t0:
            break
        for pair in server.PAIRS:
            bar = market[pair].minute(open_ts)
            if bar is not None:
                prev_close[pair] = bar["c"]
    for open_ts in clock:
        decision = open_ts + MIN_MS
        if decision < t0 or decision >= t1:
            if decision >= t1:
                break
            continue
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
        threshold = settings["entryThreshold"]
        hot_pairs = []
        for pair in server.PAIRS:
            slim = signals.get(pair, {}).get(open_ts)
            if slim is None:
                continue
            if slim[0] >= threshold or slim[1] >= threshold:
                hot_pairs.append((pair, slim))
        if hot_pairs or variant.state["openPositions"]:
            # Pass every pair so a lower score cannot hide, but only bother
            # building rows when something can open or a position needs the book.
            rows = []
            for pair in server.PAIRS:
                slim = signals.get(pair, {}).get(open_ts)
                if slim is not None:
                    rows.append(_row(pair, slim))
            variant.maybe_open(rows, decision)
        for pair, bar in bars.items():
            prev_close[pair] = bar["c"]
        if variant.state["openPositions"]:
            variant.note_equity(decision, sample=False)
    server._clock = None
    end_ms = t1
    summary = backtest.summarize(variant, end_ms)
    unreal = sum(float(p.get("unrealized") or 0.0) for p in summary["open"])
    metrics = _metrics(
        variant.trades,
        summary["balance"],
        summary["equity"],
        summary["max_dd_pct"],
        len(summary["open"]),
        unreal,
    )
    metrics["name"] = name
    metrics["reasons"] = summary["reasons"]
    return metrics


def _fmt_pct(value):
    if value is None:
        return "-"
    return "%.1f%%" % value


def _fmt_num(value, digits=2):
    if value is None:
        return "-"
    return ("%." + str(digits) + "f") % value


def _fmt_pf(value):
    if value is None:
        return "-"
    return "%.2f" % value


def render_table(rows):
    header = "| Config | Window | Trades | Win | PF | E[R] | PnL % | Equity % | Max DD | Open |"
    line = "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|"
    body = [header, line]
    for row in rows:
        win = ("%.0f%%" % (row["win_rate"] * 100)) if row.get("win_rate") is not None else "-"
        body.append("| %s | %s | %d | %s | %s | %s | %s | %s | %s | %d |" % (
            row["name"],
            row["window"],
            row["trades"],
            win,
            _fmt_pf(row.get("pf")),
            _fmt_num(row.get("expectancy"), 3),
            _fmt_num(row.get("pnl_pct")),
            _fmt_num(row.get("equity_pct")),
            _fmt_num(row.get("max_dd_pct")),
            row.get("open") or 0,
        ))
    return "\n".join(body)


def _self_check_partial():
    book = backtest.new_state(100, False)
    book["settings"]["partialTpR"] = 1.0
    book["settings"]["partialTpFraction"] = 0.5
    book["settings"]["partialMoveBreakeven"] = True
    book["settings"]["trailingEnabled"] = False
    book["settings"]["takeProfitR"] = 2.0
    server.STATE = book
    book["openPositions"] = [{
        "pair": "TEST-USDT",
        "side": "long",
        "entry": 100.0,
        "sl": 90.0,
        "tp": 120.0,
        "notional": 1000.0,
        "initialSl": 90.0,
        "initialTp": 120.0,
        "initialRiskDist": 10.0,
        "initialRiskAmt": 100.0,
        "openRiskAmt": 100.0,
        "riskAmt": 100.0,
        "entryFee": 0.0,
        "partialTaken": False,
        "trailingActive": False,
        "bestPrice": 100.0,
        "tradeId": 1,
        "price": 100.0,
    }]
    backtest.drive("TEST-USDT", 100.0, 125.0, gap=False, on_close=lambda *a: None)
    if book["openPositions"]:
        raise SystemExit("partial self-check: position still open")
    reasons = [h["reason"] for h in book["history"]]
    if "partial" not in reasons or "TP" not in reasons:
        raise SystemExit("partial self-check: reasons %s" % reasons)
    final = book["history"][0]
    if final.get("tradePnl") is None or final["tradePnl"] <= 0:
        raise SystemExit("partial self-check: trade pnl %s" % final.get("tradePnl"))
    print("partial self-check ok", flush=True)


def _self_check_select():
    chosen, relaxed = select_preset([
        {"name": "wider_tp", "trades": 5, "expectancy": 1.0, "pf": 2.0},
        {"name": "atr_partial", "trades": 20, "expectancy": 0.1, "pf": 1.1},
        {"name": "current_thr95_nofee", "trades": 40, "expectancy": 0.5, "pf": 1.4},
    ])
    if chosen != "atr_partial" or relaxed:
        raise SystemExit("select self-check failed: %s relaxed=%s" % (chosen, relaxed))
    chosen, relaxed = select_preset([
        {"name": "wider_tp", "trades": 4, "expectancy": -0.2, "pf": 0.8},
        {"name": "atr_partial", "trades": 3, "expectancy": 0.4, "pf": 1.2},
    ])
    if chosen != "atr_partial" or not relaxed:
        raise SystemExit("select relaxed self-check failed: %s relaxed=%s" % (chosen, relaxed))
    print("select self-check ok", flush=True)


def evaluate(signals, market, clock, data_end_ms):
    start, slices = slice_bounds(data_end_ms)
    plan = folds_from(slices)
    print("slices:", flush=True)
    for name, lo, hi in slices:
        print("  %s %s → %s" % (name, backtest.madrid(lo), backtest.madrid(hi)), flush=True)
    rows = []
    fold_reports = []
    # Full-sample reference for the two "current" books and the diagnostic. In sample.
    full_lo = slices[0][1]
    full_hi = slices[-1][2]
    for name, settings in (
        ("current_thr100", dict(LIVE, entryThreshold=100)),
        ("current_thr95", dict(LIVE, entryThreshold=95)),
        DIAGNOSTIC,
    ):
        print("full %s" % name, flush=True)
        metrics = replay(name, settings, signals, market, clock, full_lo, full_hi)
        metrics["window"] = "full"
        metrics["role"] = "in_sample"
        rows.append(metrics)
        print("  trades %d exp %s pnl %s" % (
            metrics["trades"], metrics["expectancy"], metrics["pnl_pct"]), flush=True)

    for fold in plan:
        train_rows = []
        print("fold %s train" % fold["name"], flush=True)
        for name, settings in ALL_RUNS:
            metrics = replay(name, settings, signals, market, clock, fold["train"][0], fold["train"][1])
            metrics["window"] = fold["name"] + " train"
            metrics["role"] = "train"
            train_rows.append(metrics)
            rows.append(metrics)
            print("  train %-20s trades %3d exp %s pnl %s dd %s" % (
                name, metrics["trades"], _fmt_num(metrics["expectancy"], 3),
                _fmt_num(metrics["pnl_pct"]), _fmt_num(metrics["max_dd_pct"])), flush=True)
        chosen, relaxed = select_preset(train_rows)
        print("  chosen %s relaxed=%s" % (chosen, relaxed), flush=True)
        test_rows = []
        print("fold %s test" % fold["name"], flush=True)
        for name, settings in ALL_RUNS:
            metrics = replay(name, settings, signals, market, clock, fold["test"][0], fold["test"][1])
            metrics["window"] = fold["name"] + " test"
            metrics["role"] = "test"
            metrics["chosen"] = name == chosen
            test_rows.append(metrics)
            rows.append(metrics)
            print("  test  %-20s trades %3d exp %s pnl %s dd %s%s" % (
                name, metrics["trades"], _fmt_num(metrics["expectancy"], 3),
                _fmt_num(metrics["pnl_pct"]), _fmt_num(metrics["max_dd_pct"]),
                "  CHOSEN" if name == chosen else ""), flush=True)
        fold_reports.append({
            "name": fold["name"],
            "train": [backtest.madrid(fold["train"][0]), backtest.madrid(fold["train"][1])],
            "test": [backtest.madrid(fold["test"][0]), backtest.madrid(fold["test"][1])],
            "chosen": chosen,
            "relaxed": relaxed,
            "test_metrics": next(r for r in test_rows if r["name"] == chosen),
        })
    return {
        "start": backtest.madrid(_ms(start)),
        "slices": [
            {"name": n, "start": backtest.madrid(lo), "end": backtest.madrid(hi)}
            for n, lo, hi in slices
        ],
        "folds": fold_reports,
        "rows": rows,
        "selection_rule": (
            "On each train window, choose the selectable preset with the highest "
            "closed-trade expectancy in R among those with at least %d trades. "
            "If none reach that count, choose the highest expectancy anyway. "
            "current_thr95_nofee is not selectable. The test window is not used to choose."
            % MIN_TRAIN_TRADES
        ),
    }


def pooled_oos(report):
    """Trade-weighted OOS expectancy per preset, from test rows only."""
    out = []
    names = [name for name, _ in ALL_RUNS]
    for name in names:
        tests = [r for r in report["rows"] if r["name"] == name and r["role"] == "test"]
        trades = sum(r["trades"] for r in tests)
        if trades:
            exp = sum((r["expectancy"] or 0) * r["trades"] for r in tests) / trades
        else:
            exp = None
        pnl = sum(r["pnl"] for r in tests)
        positive_folds = sum(1 for r in tests if r["expectancy"] is not None and r["expectancy"] > 0)
        enough = all(r["trades"] >= MIN_OOS_TRADES for r in tests) and len(tests) == len(report["folds"])
        robust = enough and all(
            r["expectancy"] is not None and r["expectancy"] > 0 and (r["pf"] or 0) > 1
            for r in tests
        )
        out.append({
            "name": name,
            "trades": trades,
            "expectancy": None if exp is None else round(exp, 4),
            "pnl": round(pnl, 2),
            "pnl_pct_sum": round(sum(r["pnl_pct"] for r in tests), 2),
            "positive_folds": positive_folds,
            "folds": len(tests),
            "robust": robust,
        })
    return out


def main():
    _self_check_select()
    backtest._self_check_exits()
    _self_check_partial()

    now_ms = int(time.time() * 1000)
    end_ms = now_ms - (now_ms % MIN_MS)
    if end_ms == now_ms:
        end_ms -= MIN_MS
    start_ms = _ms(datetime(2026, 4, 23, 0, 0, tzinfo=MADRID))
    print("window %s → %s Madrid" % (backtest.madrid(start_ms), backtest.madrid(end_ms)), flush=True)
    print("source Blofin %s" % backtest.BLOFIN, flush=True)
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
    signals, mismatches = precompute(market, clock)
    report = evaluate(signals, market, clock, end_ms)
    report["source"] = "Blofin openapi.blofin.com candles"
    report["fees"] = "0.06% taker + 0.02% slippage per side, except current_thr95_nofee"
    report["score_price_mismatches"] = mismatches
    report["minutes"] = len(clock)
    report["end"] = backtest.madrid(end_ms)
    report["pooled_oos"] = pooled_oos(report)
    report["presets"] = {name: settings for name, settings in ALL_RUNS}
    out = Path("/tmp/granmago-walkforward.json")
    # Trade paths are large; the table is the summary.
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print()
    print(render_table(report["rows"]))
    print()
    print("POOLED OOS (test folds only, each fold reset to 1000 USDT)")
    for row in report["pooled_oos"]:
        print("  %-22s trades %4d  E[R] %s  sum pnl %s USDT  positive folds %d/%d  robust %s" % (
            row["name"], row["trades"], _fmt_num(row["expectancy"], 3),
            _fmt_num(row["pnl"]), row["positive_folds"], row["folds"], row["robust"]))
    print()
    print("CHOSEN BY TRAIN RULE")
    for fold in report["folds"]:
        test = fold["test_metrics"]
        print("  %s -> %s (relaxed %s) test trades %d E[R] %s pnl %s%% dd %s%%" % (
            fold["name"], fold["chosen"], fold["relaxed"], test["trades"],
            _fmt_num(test["expectancy"], 3), _fmt_num(test["pnl_pct"]),
            _fmt_num(test["max_dd_pct"])))
    print("wrote %s" % out, flush=True)


if __name__ == "__main__":
    main()
