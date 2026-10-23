#!/usr/bin/env python3
"""Count why Setup A never becomes a trade on the cached Blofin window.

A sweep episode is one arming of the important daily pivot (the same arm
the strategy uses). The episode ends when two (variant A) or one (variant B)
confirmation closes reclaim the level, when a different pivot is armed, or
when the series ends. Gates on a completed reclaim are applied in the same
order as find_signals: trend, stochastic, squeeze, ADX, then stop/target.

This script does not choose parameters from profit. It only counts.
"""

import json
import sys
from collections import Counter
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(TOOLS.parent / "granmago"))

import granmago_strategy as course  # noqa: E402
import server  # noqa: E402

CACHE = Path("/tmp/granmago-bt-cache")


def load(pair, bar):
    payload = json.loads((CACHE / ("%s-%s.json" % (pair, bar))).read_text(encoding="utf-8"))
    return payload["rows"]


def _geometry(spec, side, confirm, index, extreme, level, c4, atr_s, daily):
    price = float(confirm[index]["c"])
    stop = course._stop_price(extreme, price, course._atr_asof(c4, atr_s, int(confirm[index]["ts"]) + spec["confirm_ms"]), 0.3, confirm, index, side)
    if stop is None:
        dist_raw = (price - extreme) if side == "long" else (extreme - price)
        if dist_raw <= 0:
            return "stop_wrong_side"
        if price > 0 and dist_raw > price * course.MAX_STOP_PCT:
            return "stop_wider_than_8pct"
        return "stop_invalid"
    target = course._target_price(daily, side, price)
    if target is None:
        return "no_target"
    if not course._reward_ok(price, stop, target, side, 0.0):
        return "target_wrong_side"
    return None


def _stoch_cross(k_series, sweep_i, index, side):
    """True when %K leaves 20 (long) or 80 (short) between the sweep and the signal."""
    start = max(1, sweep_i)
    for j in range(start, index + 1):
        prev = k_series[j - 1] if j - 1 < len(k_series) else None
        cur = k_series[j] if j < len(k_series) else None
        if prev is None or cur is None:
            continue
        if side == "long" and prev <= 20.0 and cur > 20.0:
            return True
        if side == "short" and prev >= 80.0 and cur < 80.0:
            return True
    return False


def diagnose_pair(pair, variant):
    spec = course.variant_spec(variant)
    c4 = load(pair, "4H")
    daily = load(pair, "1D")
    weekly = load(pair, "1W")
    # Drop the in-progress 4H bar. Daily and weekly caches already stop on a closed bar.
    if c4:
        c4 = c4[:-1]
    confirm = course.aggregate_12h(c4) if spec["confirm_ms"] == course.H12_MS else c4
    counts = Counter()
    if len(confirm) < 30 or len(daily) < course.EMA_PERIOD + course.PIVOT_RIGHT + 2:
        counts["too_short"] += 1
        return counts

    daily_states = course.trend_states(daily)
    weekly_states = course.trend_states(weekly)
    daily_ema = course.ema_series([float(c["c"]) for c in daily])
    k_series = course.stoch_k_series(confirm)
    hist = course.squeeze_hist(confirm)
    adx = course.adx_series(confirm)
    atr_s = course.atr_series(c4)
    for side in spec["sides"]:
        side_pivots = course.close_pivots(daily, "low" if side == "long" else "high")
        sweep_of = {}
        for pivot in side_pivots:
            sweep_of[id(pivot)] = None
            for i, bar in enumerate(confirm):
                if int(bar["ts"]) < pivot["known_ms"]:
                    continue
                pierced = (side == "long" and float(bar["l"]) < pivot["price"]) or (
                    side == "short" and float(bar["h"]) > pivot["price"]
                )
                if pierced:
                    sweep_of[id(pivot)] = i
                    break

        # Every in-window pivot pierce, whether or not it was the extreme.
        for pivot in side_pivots:
            i = sweep_of[id(pivot)]
            if i is None:
                continue
            bar = confirm[i]
            if pivot["ts"] < int(bar["ts"]) - course.PIVOT_LOOKBACK_MS:
                counts["pierce_outside_30d"] += 1
                continue
            counts["pierce_inside_30d"] += 1
            window_start = int(bar["ts"]) - course.PIVOT_LOOKBACK_MS
            known = [
                item for item in side_pivots
                if item["known_ms"] <= int(bar["ts"]) and item["ts"] >= window_start
                and (sweep_of[id(item)] is None or sweep_of[id(item)] >= i)
            ]
            if not known:
                continue
            if side == "long":
                important = min(known, key=lambda item: (item["price"], item["ts"]))
            else:
                important = max(known, key=lambda item: (item["price"], -item["ts"]))
            if abs(important["price"] - pivot["price"]) > 1e-9:
                counts["pierce_not_the_extreme"] += 1
            else:
                counts["pierce_is_the_extreme"] += 1

        armed = None
        sweep_i = None
        extreme = None
        streak = 0
        for i, bar in enumerate(confirm):
            close_ms = int(bar["ts"]) + spec["confirm_ms"]
            trend = course.trend_at(daily, weekly, close_ms, daily_states, weekly_states)
            window_start = int(bar["ts"]) - course.PIVOT_LOOKBACK_MS
            known = [
                pivot for pivot in side_pivots
                if pivot["known_ms"] <= int(bar["ts"]) and pivot["ts"] >= window_start
            ]
            unswept = [
                pivot for pivot in known
                if sweep_of[id(pivot)] is None or sweep_of[id(pivot)] >= i
            ]
            important = None
            if unswept:
                if side == "long":
                    important = min(unswept, key=lambda pivot: (pivot["price"], pivot["ts"]))
                else:
                    important = max(unswept, key=lambda pivot: (pivot["price"], -pivot["ts"]))
            pierced = False
            if important is not None:
                if side == "long" and float(bar["l"]) < important["price"]:
                    pierced = True
                elif side == "short" and float(bar["h"]) > important["price"]:
                    pierced = True
            if pierced and (armed is None or abs(armed["price"] - important["price"]) > 1e-9):
                if armed is not None:
                    counts["ended_replaced_before_confirm"] += 1
                    counts["bars_waited_replaced"] += i - sweep_i
                counts["episodes"] += 1
                armed = important
                sweep_i = i
                extreme = float(bar["l"] if side == "long" else bar["h"])
                streak = 0
            elif armed is not None and extreme is not None:
                wick = float(bar["l"] if side == "long" else bar["h"])
                extreme = min(extreme, wick) if side == "long" else max(extreme, wick)

            level = None
            if armed is not None and sweep_i is not None:
                level = course._confirmation_level(confirm, sweep_i, side, armed["price"])
                reclaimed = float(bar["c"]) > level if side == "long" else float(bar["c"]) < level
                streak = streak + 1 if reclaimed else 0
            else:
                streak = 0

            if streak != spec["confirm_bars"] or armed is None:
                continue

            waited = i - sweep_i
            counts["confirmed_episodes"] += 1
            counts["bars_waited_confirmed"] += waited
            if waited <= spec["confirm_bars"]:
                counts["confirmed_within_window"] += 1
            else:
                counts["confirmed_late"] += 1
            k_value = k_series[i] if i < len(k_series) else None
            if _stoch_cross(k_series, sweep_i, i, side):
                counts["confirmed_with_stoch_cross"] += 1
            daily_only = _daily_close_side(daily, daily_ema, close_ms)
            if daily_only == side:
                counts["confirmed_daily_ema_agrees"] += 1
            if trend == side:
                counts["confirmed_trend_agrees"] += 1
            else:
                counts["reject_trend"] += 1
            if course.stoch_allows(k_value, side):
                counts["confirmed_stoch_ok"] += 1
            else:
                counts["reject_stoch"] += 1
            if course.squeeze_allows(hist, i, side):
                counts["confirmed_squeeze_ok"] += 1
            else:
                counts["reject_squeeze"] += 1
            if course.adx_turning_up(adx, i):
                counts["confirmed_adx_ok"] += 1
            else:
                counts["reject_adx"] += 1
            geo = _geometry(spec, side, confirm, i, extreme, level, c4, atr_s, daily)
            if geo is None:
                counts["confirmed_geometry_ok"] += 1
            else:
                counts["reject_" + geo] += 1

            reason = None
            if trend != side:
                reason = "trend"
            elif not course.stoch_allows(k_value, side):
                reason = "stoch"
            elif not course.squeeze_allows(hist, i, side):
                reason = "squeeze"
            elif not course.adx_turning_up(adx, i):
                reason = "adx"
            else:
                reason = geo or "pass"
            counts["seq_" + reason] += 1
            if waited <= spec["confirm_bars"]:
                counts["prompt_seq_" + reason] += 1
                if daily_only == side:
                    counts["prompt_daily"] += 1
                if _stoch_cross(k_series, sweep_i, i, side):
                    counts["prompt_cross"] += 1
                if course.stoch_allows(k_value, side):
                    counts["prompt_stoch_ok"] += 1
                if geo is None:
                    counts["prompt_geometry"] += 1
                if daily_only == side and geo is None:
                    counts["prompt_daily_geometry"] += 1
                if daily_only == side and geo is None and _stoch_cross(k_series, sweep_i, i, side):
                    counts["prompt_daily_geometry_cross"] += 1
                if daily_only == side and geo is None and course.stoch_allows(k_value, side):
                    counts["prompt_daily_geometry_veto_ok"] += 1
            armed = None
            sweep_i = None
            extreme = None
            streak = 0
        if armed is not None:
            counts["ended_still_open"] += 1
    return counts


def _daily_close_side(daily, averages, at_ms):
    found = None
    for candle, value in zip(daily, averages):
        if int(candle["ts"]) + course.DAY_MS > at_ms:
            break
        if value is None:
            continue
        close = float(candle["c"])
        if close > value:
            found = "long"
        elif close < value:
            found = "short"
    return found


def _pivots(candles, kind, left, right, bar_ms):
    out = []
    n = len(candles)
    for i in range(left, n - right):
        price = float(candles[i]["c"])
        window = [float(candles[j]["c"]) for j in range(i - left, i + right + 1)]
        if kind == "low":
            if price != min(window) or window.index(price) != left:
                continue
        elif price != max(window) or window.index(price) != left:
            continue
        out.append({
            "ts": int(candles[i]["ts"]),
            "price": price,
            "known_ms": int(candles[i + right]["ts"]) + bar_ms,
            "source": "pivot",
        })
    return out


def looser_pair(pair, variant):
    """Count prompt sweeps of the looser level set. Not a backtest."""
    spec = course.variant_spec(variant)
    c4 = load(pair, "4H")[:-1]
    daily = load(pair, "1D")
    confirm = course.aggregate_12h(c4) if spec["confirm_ms"] == course.H12_MS else list(c4)
    counts = Counter()
    if len(confirm) < 30 or len(daily) < course.EMA_PERIOD + 5:
        return counts
    daily_ema = course.ema_series([float(c["c"]) for c in daily])
    k_series = course.stoch_k_series(confirm)
    atr_s = course.atr_series(c4)
    c4_index = {int(bar["ts"]): i for i, bar in enumerate(c4)}
    levels = {"long": [], "short": []}
    for side, kind in (("long", "low"), ("short", "high")):
        if side not in spec["sides"]:
            continue
        for pivot in _pivots(daily, kind, 3, 3, course.DAY_MS):
            levels[side].append(pivot)
        for pivot in _pivots(c4, kind, 5, 5, course.H4_MS):
            levels[side].append(pivot)
    for side in spec["sides"]:
        armed = None
        sweep_i = None
        extreme = None
        streak = 0
        for i, bar in enumerate(confirm):
            close_ms = int(bar["ts"]) + spec["confirm_ms"]
            day_open = int(bar["ts"]) - (int(bar["ts"]) % course.DAY_MS)
            prev_day = None
            for candle in daily:
                if int(candle["ts"]) + course.DAY_MS <= day_open:
                    prev_day = candle
                else:
                    break
            candidates = []
            for level in levels[side]:
                if level["known_ms"] > int(bar["ts"]):
                    continue
                if level["ts"] < int(bar["ts"]) - 20 * course.DAY_MS:
                    continue
                price = level["price"]
                hit = (side == "long" and float(bar["l"]) < price) or (side == "short" and float(bar["h"]) > price)
                if hit:
                    candidates.append(price)
            if prev_day is not None:
                price = float(prev_day["l"] if side == "long" else prev_day["h"])
                hit = (side == "long" and float(bar["l"]) < price) or (side == "short" and float(bar["h"]) > price)
                if hit:
                    candidates.append(price)
            if candidates:
                chosen = min(candidates) if side == "long" else max(candidates)
                if armed is None or abs(armed - chosen) > 1e-9:
                    if armed is not None:
                        counts["cancelled_or_replaced"] += 1
                    counts["episodes"] += 1
                    armed = chosen
                    sweep_i = i
                    extreme = float(bar["l"] if side == "long" else bar["h"])
                    streak = 0
            elif armed is not None and extreme is not None:
                wick = float(bar["l"] if side == "long" else bar["h"])
                extreme = min(extreme, wick) if side == "long" else max(extreme, wick)
            if armed is None:
                continue
            reclaimed = float(bar["c"]) > armed if side == "long" else float(bar["c"]) < armed
            if not reclaimed:
                counts["no_reclaim"] += 1
                armed = None
                continue
            streak += 1
            if streak < spec["confirm_bars"]:
                continue
            counts["prompt_confirmed"] += 1
            daily_only = _daily_close_side(daily, daily_ema, close_ms)
            cross = _stoch_cross(k_series, sweep_i, i, side)
            c4_i = c4_index.get(int(bar["ts"]))
            geo = None
            if c4_i is None and spec["confirm_ms"] == course.H12_MS:
                # 12H open equals the first 4H open of the bucket.
                c4_i = c4_index.get(int(bar["ts"]) + 2 * course.H4_MS)
            if c4_i is None:
                geo = "no_bar"
            else:
                geo = _geometry(spec, side, confirm, i, extreme, armed, c4, atr_s, daily)
            if daily_only == side:
                counts["daily"] += 1
            if cross:
                counts["cross"] += 1
            if geo is None:
                counts["geometry"] += 1
            if daily_only == side and geo is None:
                counts["pass_no_cross"] += 1
            if daily_only == side and geo is None and cross:
                counts["pass_cross"] += 1
            armed = None
            streak = 0
    return counts


def main():
    for variant in ("a", "b"):
        total = Counter()
        print("=== variant %s ===" % variant)
        for pair in server.PAIRS:
            counts = diagnose_pair(pair, variant)
            total.update(counts)
            print("  %-12s episodes %3d confirmed %3d prompt %3d seq %s" % (
                pair,
                counts["episodes"],
                counts["confirmed_episodes"],
                counts["confirmed_within_window"],
                {k: counts[k] for k in sorted(counts) if k.startswith("seq_")},
            ))
        print("TOTAL")
        for key in sorted(total):
            print("  %-32s %d" % (key, total[key]))
        confirmed = total["confirmed_episodes"] or 1
        print("independent reject share of confirmed episodes")
        for key in (
            "reject_trend", "reject_stoch", "reject_squeeze", "reject_adx",
            "reject_stop_wider_than_8pct", "reject_stop_wrong_side", "reject_stop_invalid",
            "reject_no_target", "reject_target_wrong_side",
        ):
            print("  %-32s %5.1f%%" % (key, 100.0 * total[key] / confirmed))
        print()
    for variant in ("a", "b"):
        total = Counter()
        print("=== looser levels variant %s ===" % variant)
        for pair in server.PAIRS:
            counts = looser_pair(pair, variant)
            total.update(counts)
            print("  %-12s episodes %4d confirmed %4d pass_no_cross %3d pass_cross %3d" % (
                pair, counts["episodes"], counts["prompt_confirmed"],
                counts["pass_no_cross"], counts["pass_cross"],
            ))
        print("TOTAL")
        for key in sorted(total):
            print("  %-28s %d" % (key, total[key]))
        print()


if __name__ == "__main__":
    main()
