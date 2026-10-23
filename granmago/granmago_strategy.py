"""Gran Mago course strategy, as a selectable mode next to the score scanner.

The course (14 classes) plus the later X posts disagree on a few numbers.
This module picks one concrete reading and keeps the free choices in the
add-on options (variant, adds, momentum exit, hold, minimum R, breakeven,
stop pad). Nothing here is tuned on the walk-forward sample.

Chosen readings, where the sources disagree:

- Trend is EMA50 on the weekly and on the daily. The daily wins when they
  conflict (the user asked for that; the course text skips the conflict).
  A flip needs two consecutive closes on the other side of the EMA. One
  close through it is the "deviation" he says does not count.
- Variant A follows the course: longs and shorts, confirmed by two closed
  4H candles. Variant B follows his later posts: longs only, confirmed by
  one closed 12H candle. 12H bars are three Blofin 4H bars, 00:00–12:00 and
  12:00–00:00 UTC. There is no second knob for the close count.
- Setup A is the main entry: a wick through the most extreme unswept daily
  close-pivot (left = right = 5) of the last 30 days, then the confirming
  closes back through the 4H/12H level with the most touches (±0.2%, at
  least 3). Otherwise the level is the pivot itself. The signal is the bar
  that completes the count. A later bar is not chased.
- Setup B is the with-trend pullback: the pullback extreme tags the daily
  EMA50, a broken swing high/low, an open 3-candle imbalance, or the 50%
  of a big wick, and the same close count follows. It is skipped while a
  Setup A sweep is waiting for its confirmation.
- Stochastic is 14, 3, 3. It is a veto, not a trigger: no long if %K > 80,
  no short if %K < 20. The middle of the range is allowed. Blocking it, as
  one class suggests, would reject the reclaim bars the rest of the course
  waits for. The check uses the confirmation timeframe only.
- Squeeze is LazyBear's Squeeze Momentum, not the unnamed "SQZ ADX" script.
  Bollinger 20, 2 on the close. Keltner 20, 1.5 times the SMA of true range.
  The histogram is the linear regression (offset 0) of
  close − average(average(highest high 20, lowest low 20), SMA close 20).
  Dark red, the downside valley exhausting, is hist < 0 and hist > hist[1].
  Dark green is hist > 0 and hist < hist[1]. A cross through zero counts too.
  The ADX half is Wilder ADX(14) turning up: adx > adx[1]. That is the
  stand-in for "ADX made a local low and turned" from the area-style script.
- The stop sits beyond the swept wick by max(0.5 × ATR14 of 4H, pad% of
  price). An unfilled imbalance or a big wick that reaches past that pad
  pushes the stop further. A stop wider than 8% of price is refused, same
  ceiling as the scanner. The pad defaults to 0.3%. He quoted other people
  using 2–3% and did not give his own number.
- The target is the nearest prior daily swing cluster in profit, 0.1% inside
  its extreme. There is no R-multiple target. Minimum reward/risk defaults
  to 0 because his published card has an average win near 0.4× the average
  loss (the stop wider than the target). The course note of 1.5 is the
  `granmago_min_rr` option and is not the default.
- The hard stop is a price stop. His X posts often invalidate on a 12H
  close through the wick. A close through the swept extreme blocks further
  adds; the position itself waits for the price stop, because the bot
  watches ticks and a close-only stop would ignore the wick.
- Momentum exit (on by default): a long exits when %K > 80 and the squeeze
  histogram is above zero and falling. The short is the mirror. Time exit
  is 15 confirmation bars. Breakeven is off; he mentioned it and never
  taught it.
- Adds, when enabled, are at most one or two. The first fill risks
  1/(1+adds) of the trade risk so that a full stop of every leg is still
  one `risk_pct`. An add fills only when price tags the swept extreme and
  has not touched the stop, and never after a confirmation close through
  that extreme.
- No news calendar. The course avoids data days; on X he says the CPI print
  does not change the idea. There is no calendar in this repo to do it
  faithfully, so it is not implemented.
- Countertrend is off. The repeated scalping re-entry is not implemented.
"""

import math

H4_MS = 4 * 60 * 60 * 1000
H12_MS = 12 * 60 * 60 * 1000
DAY_MS = 24 * 60 * 60 * 1000
WEEK_MS = 7 * DAY_MS

PIVOT_LEFT = 5
PIVOT_RIGHT = 5
# The extreme of the range you can see, not the origin of a trend that was
# never revisited. 90 days left that origin unswept and blocked every nearer
# sweep, so setup A never armed. 30 daily bars is the range used here.
PIVOT_LOOKBACK_MS = 30 * DAY_MS
TOUCH_TOL = 0.002
TOUCH_PAD = 0.003
MIN_TOUCHES = 3
CLUSTER_BARS = 60
PULLBACK_BARS = 8
EMA_PERIOD = 50
HYSTERESIS = 2
SQUEEZE_LEN = 20
ADX_PERIOD = 14
ATR_PERIOD = 14
STOP_ATR_MULT = 0.5
DEFAULT_STOP_PAD_PCT = 0.3
MAX_STOP_PCT = 0.08
TP_INSIDE = 0.001
WICK_LOOKBACK = 50
WICK_MULT = 2.0


def variant_spec(variant):
    """Fixed confirmation rules. A is the course, B is the later X practice."""
    text = str(variant or "a").strip().lower()
    if text == "b":
        return {
            "variant": "b",
            "sides": ("long",),
            "confirm_bars": 1,
            "confirm_ms": H12_MS,
        }
    return {
        "variant": "a",
        "sides": ("long", "short"),
        "confirm_bars": 2,
        "confirm_ms": H4_MS,
    }


def aggregate_12h(candles_4h):
    """Closed 12H candles from 4H bars. Buckets open at 00:00 and 12:00 UTC.

    A bucket is kept only when the three opens (bucket, +4H, +8H) all exist.
    Unix epoch is 00:00 UTC, so flooring the open time to 12 hours lands on
    those two clocks. A partial bucket is dropped.
    """
    groups = {}
    order = []
    for candle in candles_4h:
        try:
            ts = int(candle["ts"])
        except (TypeError, ValueError, KeyError):
            continue
        bucket = ts - (ts % H12_MS)
        if bucket not in groups:
            groups[bucket] = []
            order.append(bucket)
        groups[bucket].append(candle)
    out = []
    for bucket in order:
        needed = (bucket, bucket + H4_MS, bucket + 2 * H4_MS)
        chosen = []
        seen = set()
        for candle in sorted(groups[bucket], key=lambda row: int(row["ts"])):
            ts = int(candle["ts"])
            if ts in seen or ts not in needed:
                continue
            seen.add(ts)
            chosen.append(candle)
        if len(chosen) != 3:
            continue
        out.append({
            "ts": bucket,
            "o": float(chosen[0]["o"]),
            "h": max(float(c["h"]) for c in chosen),
            "l": min(float(c["l"]) for c in chosen),
            "c": float(chosen[-1]["c"]),
        })
    return out


def drop_forming(candles, bar_ms, now_ms):
    """Remove the bar that has not closed yet. `now_ms` None keeps the series."""
    if not candles or now_ms is None:
        return list(candles or [])
    out = []
    for candle in candles:
        try:
            if int(candle["ts"]) + bar_ms <= int(now_ms):
                out.append(candle)
        except (TypeError, ValueError, KeyError):
            continue
    return out


def ema_series(values, period=EMA_PERIOD):
    """EMA aligned to `values`. Leading entries are None until the seed."""
    period = int(period)
    out = [None] * len(values)
    if period < 1 or len(values) < period:
        return out
    k = 2.0 / (period + 1.0)
    ema = sum(values[:period]) / period
    out[period - 1] = ema
    for i in range(period, len(values)):
        ema = values[i] * k + ema * (1.0 - k)
        out[i] = ema
    return out


def trend_states(candles, period=EMA_PERIOD, confirm=HYSTERESIS):
    """'long' / 'short' / None per bar. Two closes on the other side flip it."""
    closes = [float(c["c"]) for c in candles]
    averages = ema_series(closes, period)
    state = None
    up = 0
    down = 0
    states = []
    for close, average in zip(closes, averages):
        if average is None:
            states.append(None)
            continue
        if close > average:
            up += 1
            down = 0
        elif close < average:
            down += 1
            up = 0
        else:
            up = 0
            down = 0
        if up >= confirm:
            state = "long"
        elif down >= confirm:
            state = "short"
        states.append(state)
    return states


def _state_asof(candles, states, bar_ms, at_ms):
    found = None
    for candle, state in zip(candles, states):
        try:
            closed = int(candle["ts"]) + bar_ms
        except (TypeError, ValueError, KeyError):
            continue
        if closed <= at_ms:
            found = state
        else:
            break
    return found


def trend_at(daily, weekly, at_ms, daily_states=None, weekly_states=None):
    """Daily EMA50 regime at `at_ms`. Weekly is used only while the daily is unset."""
    if daily_states is None:
        daily_states = trend_states(daily)
    if weekly_states is None:
        weekly_states = trend_states(weekly)
    daily_side = _state_asof(daily, daily_states, DAY_MS, at_ms)
    if daily_side in ("long", "short"):
        return daily_side
    return _state_asof(weekly, weekly_states, WEEK_MS, at_ms)


def _sma(values, length):
    out = [None] * len(values)
    if length <= 0 or len(values) < length:
        return out
    total = sum(values[:length])
    out[length - 1] = total / length
    for i in range(length, len(values)):
        total += values[i] - values[i - length]
        out[i] = total / length
    return out


def _linreg_at_end(values):
    """Regression value on the last index. Pine `linreg(..., length, 0)`."""
    n = len(values)
    if n == 0:
        return None
    x_mean = (n - 1) / 2.0
    y_mean = sum(values) / float(n)
    num = 0.0
    den = 0.0
    for i, y in enumerate(values):
        dx = i - x_mean
        num += dx * (y - y_mean)
        den += dx * dx
    slope = 0.0 if den == 0 else num / den
    intercept = y_mean - slope * x_mean
    return intercept + slope * (n - 1)


def squeeze_hist(candles, length=SQUEEZE_LEN):
    """LazyBear squeeze-momentum histogram. None until the window is full.

    True range of the first bar is high − low. Later bars use the standard
    true range. The Keltner width is an SMA of that range, matching the
    script's `sma(tr, lengthKC)`, not Wilder ATR.
    """
    n = len(candles)
    out = [None] * n
    if n < length or length < 2:
        return out
    highs = [float(c["h"]) for c in candles]
    lows = [float(c["l"]) for c in candles]
    closes = [float(c["c"]) for c in candles]
    trs = [highs[0] - lows[0]]
    for i in range(1, n):
        trs.append(max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1])))
    close_avg = _sma(closes, length)
    delta = [None] * n
    for i in range(length - 1, n):
        if close_avg[i] is None:
            continue
        highest = max(highs[i - length + 1:i + 1])
        lowest = min(lows[i - length + 1:i + 1])
        mid = ((highest + lowest) / 2.0 + close_avg[i]) / 2.0
        delta[i] = closes[i] - mid
    for i in range(n):
        if i < 2 * (length - 1):
            continue
        window = delta[i - length + 1:i + 1]
        if any(value is None for value in window):
            continue
        out[i] = _linreg_at_end(window)
    return out


def squeeze_allows(hist, index, side):
    """Long: red valley turning up, or the histogram crossing above zero."""
    if index < 1 or index >= len(hist):
        return False
    cur = hist[index]
    prev = hist[index - 1]
    if cur is None or prev is None:
        return False
    if side == "long":
        return (cur < 0 and cur > prev) or (cur > 0 and prev <= 0)
    return (cur > 0 and cur < prev) or (cur < 0 and prev >= 0)


def stoch_k_series(candles, period=14, smooth=3):
    """Slow %K of a 14, 3, 3 stochastic. %D is not used; the veto is on %K."""
    n = len(candles)
    raw = [None] * n
    for i in range(period - 1, n):
        window = candles[i - period + 1:i + 1]
        high = max(float(c["h"]) for c in window)
        low = min(float(c["l"]) for c in window)
        close = float(candles[i]["c"])
        raw[i] = 50.0 if high == low else (close - low) / (high - low) * 100.0
    out = [None] * n
    for i in range(n):
        if i < period - 1 + smooth - 1:
            continue
        chunk = raw[i - smooth + 1:i + 1]
        if any(value is None for value in chunk):
            continue
        out[i] = sum(chunk) / float(smooth)
    return out


def stoch_allows(k_value, side):
    """Veto only the exhausted side. A missing value fails closed."""
    if k_value is None:
        return False
    if side == "long":
        return k_value <= 80.0
    return k_value >= 20.0


def _wilder(values, period):
    if len(values) < period:
        return []
    smoothed = [sum(values[:period]) / float(period)]
    for value in values[period:]:
        smoothed.append((smoothed[-1] * (period - 1) + value) / float(period))
    return smoothed


def _true_ranges(candles):
    ranges = []
    for i in range(1, len(candles)):
        prev = float(candles[i - 1]["c"])
        high = float(candles[i]["h"])
        low = float(candles[i]["l"])
        ranges.append(max(high - low, abs(high - prev), abs(low - prev)))
    return ranges


def adx_series(candles, period=ADX_PERIOD):
    """Wilder ADX aligned to candles. None until two smoothings are done."""
    n = len(candles)
    out = [None] * n
    if period < 2 or n < (2 * period) + 1:
        return out
    plus_dm = []
    minus_dm = []
    for i in range(1, n):
        up = float(candles[i]["h"]) - float(candles[i - 1]["h"])
        down = float(candles[i - 1]["l"]) - float(candles[i]["l"])
        plus_dm.append(up if up > down and up > 0 else 0.0)
        minus_dm.append(down if down > up and down > 0 else 0.0)
    atr_s = _wilder(_true_ranges(candles), period)
    plus_s = _wilder(plus_dm, period)
    minus_s = _wilder(minus_dm, period)
    dx = []
    for atr_value, plus, minus in zip(atr_s, plus_s, minus_s):
        if atr_value <= 0:
            dx.append(0.0)
            continue
        plus_di = 100.0 * plus / atr_value
        minus_di = 100.0 * minus / atr_value
        denom = plus_di + minus_di
        dx.append(0.0 if denom <= 0 else 100.0 * abs(plus_di - minus_di) / denom)
    adx_s = _wilder(dx, period)
    # atr_s[0] sits on candle `period`. adx_s[0] averages the first `period`
    # DX values, so it sits on candle `period + period - 1`.
    for k, value in enumerate(adx_s):
        idx = (2 * period - 1) + k
        if idx < n:
            out[idx] = value
    return out


def adx_turning_up(series, index):
    if index < 1 or index >= len(series):
        return False
    cur = series[index]
    prev = series[index - 1]
    if cur is None or prev is None:
        return False
    return cur > prev


def atr_series(candles, period=ATR_PERIOD):
    n = len(candles)
    out = [None] * n
    if n < period + 1:
        return out
    smoothed = _wilder(_true_ranges(candles), period)
    for k, value in enumerate(smoothed):
        idx = period + k
        if idx < n:
            out[idx] = value
    return out


def _atr_asof(candles_4h, series, at_ms):
    found = None
    for candle, value in zip(candles_4h, series):
        try:
            closed = int(candle["ts"]) + H4_MS
        except (TypeError, ValueError, KeyError):
            continue
        if closed <= at_ms:
            found = value
        else:
            break
    return found


def close_pivots(candles, kind, left=PIVOT_LEFT, right=PIVOT_RIGHT):
    """Close pivots. Known only after `right` newer bars have printed.

    The price is the close, which is his line-chart extreme. On a tie the
    leftmost bar keeps the pivot so a flat stretch does not spawn several.
    """
    out = []
    n = len(candles)
    for i in range(left, n - right):
        price = float(candles[i]["c"])
        window = [float(candles[j]["c"]) for j in range(i - left, i + right + 1)]
        if kind == "low":
            if price != min(window) or window.index(price) != left:
                continue
        else:
            if price != max(window) or window.index(price) != left:
                continue
        out.append({
            "index": i,
            "ts": int(candles[i]["ts"]),
            "price": price,
            "confirm_ts": int(candles[i + right]["ts"]),
            "known_ms": int(candles[i + right]["ts"]) + DAY_MS,
        })
    return out


def _densest_level(prices, tol=TOUCH_TOL, minimum=MIN_TOUCHES):
    """Median of the band that contains the most prices. None if under `minimum`."""
    if not prices:
        return None
    best_count = 0
    best_level = None
    for anchor in prices:
        band = [price for price in prices if abs(price - anchor) <= abs(anchor) * tol]
        if len(band) > best_count:
            ordered = sorted(band)
            best_count = len(band)
            best_level = ordered[len(ordered) // 2]
    if best_count < minimum:
        return None
    return best_level


def _confirmation_level(confirm, sweep_i, side, fallback):
    start = max(0, sweep_i - CLUSTER_BARS)
    if side == "long":
        prices = [float(confirm[j]["l"]) for j in range(start, sweep_i)]
    else:
        prices = [float(confirm[j]["h"]) for j in range(start, sweep_i)]
    level = _densest_level(prices)
    return fallback if level is None else level


def _lower_wick(candle):
    body = min(float(candle["o"]), float(candle["c"]))
    return max(0.0, body - float(candle["l"]))


def _upper_wick(candle):
    body = max(float(candle["o"]), float(candle["c"]))
    return max(0.0, float(candle["h"]) - body)


def _wick_median(confirm, index, side):
    start = max(0, index - WICK_LOOKBACK + 1)
    if side == "long":
        wicks = [_lower_wick(confirm[j]) for j in range(start, index + 1)]
    else:
        wicks = [_upper_wick(confirm[j]) for j in range(start, index + 1)]
    if not wicks:
        return 0.0
    ordered = sorted(wicks)
    return ordered[len(ordered) // 2]


def _fvgs(confirm, side):
    """3-candle imbalances. Long uses a gap up (bullish), short a gap down."""
    out = []
    for i in range(2, len(confirm)):
        if side == "long" and float(confirm[i]["l"]) > float(confirm[i - 2]["h"]):
            out.append({
                "index": i,
                "far": float(confirm[i - 2]["h"]),
                "near": float(confirm[i]["l"]),
            })
        elif side == "short" and float(confirm[i]["h"]) < float(confirm[i - 2]["l"]):
            out.append({
                "index": i,
                "far": float(confirm[i - 2]["l"]),
                "near": float(confirm[i]["h"]),
            })
    return out


def _gap_filled(gap, confirm, index, side):
    for j in range(gap["index"] + 1, index + 1):
        if side == "long" and float(confirm[j]["l"]) <= gap["far"]:
            return True
        if side == "short" and float(confirm[j]["h"]) >= gap["far"]:
            return True
    return False


def _stop_price(extreme, price, atr, pad_pct, confirm, index, side):
    """Price stop beyond the swept extreme. None when it would exceed 8%.

    The pad is max(0.5 × 4H ATR, pad% of price). It only moves further out:
    an unfilled imbalance or a big wick that still straddles the pad pushes
    the stop to the far side of that magnet. A magnet already inside the pad
    is left alone, because the stop is already beyond it.
    """
    pad = max(0.0, float(pad_pct)) / 100.0
    atr_pad = 0.0 if atr is None else STOP_ATR_MULT * float(atr)
    margin = max(atr_pad, pad * float(price))
    extreme = float(extreme)
    price = float(price)
    stop = extreme - margin if side == "long" else extreme + margin
    for gap in _fvgs(confirm, side):
        if gap["index"] > index or _gap_filled(gap, confirm, index, side):
            continue
        if side == "long" and gap["far"] < stop < gap["near"]:
            stop = gap["far"]
        elif side == "short" and gap["near"] < stop < gap["far"]:
            stop = gap["far"]
    median = _wick_median(confirm, index, side)
    start = max(0, index - WICK_LOOKBACK + 1)
    for j in range(start, index + 1):
        candle = confirm[j]
        if side == "long":
            wick = _lower_wick(candle)
            if median <= 0 or wick <= WICK_MULT * median:
                continue
            far = float(candle["l"])
            near = far + wick
            if far < stop < near:
                stop = far
        else:
            wick = _upper_wick(candle)
            if median <= 0 or wick <= WICK_MULT * median:
                continue
            far = float(candle["h"])
            near = far - wick
            if near < stop < far:
                stop = far
    dist = (price - stop) if side == "long" else (stop - price)
    if dist <= 0 or price <= 0 or dist > price * MAX_STOP_PCT:
        return None
    return stop


def _target_price(daily, side, entry):
    """Nearest prior daily swing cluster, 0.1% inside the extreme. None if none."""
    kind = "high" if side == "long" else "low"
    pivots = close_pivots(daily, kind)
    if side == "long":
        prices = [p["price"] for p in pivots if p["price"] > entry]
        if not prices:
            return None
        nearest = min(prices)
        cluster = [price for price in prices if abs(price - nearest) <= abs(nearest) * TOUCH_TOL]
        return max(cluster) * (1.0 - TP_INSIDE)
    prices = [p["price"] for p in pivots if p["price"] < entry]
    if not prices:
        return None
    nearest = max(prices)
    cluster = [price for price in prices if abs(price - nearest) <= abs(nearest) * TOUCH_TOL]
    return min(cluster) * (1.0 + TP_INSIDE)


def _reward_ok(entry, stop, target, side, min_rr):
    if target is None or stop is None:
        return False
    if side == "long" and not (stop < entry < target):
        return False
    if side == "short" and not (target < entry < stop):
        return False
    risk = abs(entry - stop)
    if risk <= 0:
        return False
    if min_rr and abs(target - entry) / risk < float(min_rr):
        return False
    return True


def _ema_asof(daily, averages, at_ms):
    found = None
    for candle, value in zip(daily, averages):
        if int(candle["ts"]) + DAY_MS <= at_ms:
            found = value
        else:
            break
    return found


def _tags_level(candle, level, side):
    if level is None or level <= 0:
        return False
    if side == "long":
        return float(candle["l"]) <= level * (1.0 + TOUCH_PAD) and float(candle["c"]) >= level * (1.0 - TOUCH_PAD)
    return float(candle["h"]) >= level * (1.0 - TOUCH_PAD) and float(candle["c"]) <= level * (1.0 + TOUCH_PAD)


def _pullback_levels(confirm, index, daily, daily_ema, high_pivots, low_pivots, side, at_ms, bar_ts):
    levels = []
    ema = _ema_asof(daily, daily_ema, at_ms)
    if ema:
        levels.append(ema)
    pivots = high_pivots if side == "long" else low_pivots
    window_start = bar_ts - PIVOT_LOOKBACK_MS
    close = float(confirm[index]["c"])
    for pivot in pivots:
        if pivot["known_ms"] > bar_ts or pivot["ts"] < window_start:
            continue
        if side == "long" and pivot["price"] < close:
            levels.append(pivot["price"])
        elif side == "short" and pivot["price"] > close:
            levels.append(pivot["price"])
    median = _wick_median(confirm, index, side)
    start = max(0, index - WICK_LOOKBACK + 1)
    for j in range(start, index + 1):
        candle = confirm[j]
        if side == "long":
            wick = _lower_wick(candle)
            if median > 0 and wick > WICK_MULT * median:
                levels.append(float(candle["l"]) + 0.5 * wick)
        else:
            wick = _upper_wick(candle)
            if median > 0 and wick > WICK_MULT * median:
                levels.append(float(candle["h"]) - 0.5 * wick)
    for gap in _fvgs(confirm, side):
        if gap["index"] > index:
            continue
        filled = False
        for j in range(gap["index"] + 1, index + 1):
            if side == "long" and float(confirm[j]["l"]) <= gap["far"]:
                filled = True
                break
            if side == "short" and float(confirm[j]["h"]) >= gap["far"]:
                filled = True
                break
        if not filled:
            levels.append((gap["far"] + gap["near"]) / 2.0)
    return levels


def _indicators_ok(index, side, k_series, hist, adx):
    if index < 1:
        return False
    k_value = k_series[index] if index < len(k_series) else None
    if not stoch_allows(k_value, side):
        return False
    if not squeeze_allows(hist, index, side):
        return False
    if not adx_turning_up(adx, index):
        return False
    return True


def _pack_signal(spec, side, setup, confirm, index, extreme, level, c4, atr_s, k_series, hist, adx, daily, min_rr, pad_pct):
    bar = confirm[index]
    price = float(bar["c"])
    close_ms = int(bar["ts"]) + spec["confirm_ms"]
    atr = _atr_asof(c4, atr_s, close_ms)
    stop = _stop_price(extreme, price, atr, pad_pct, confirm, index, side)
    target = _target_price(daily, side, price)
    if not _reward_ok(price, stop, target, side, min_rr):
        return None
    return {
        "side": side,
        "setup": setup,
        "variant": spec["variant"],
        "price": price,
        "sl": stop,
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
    }


def _bar_snapshot(confirm, index, spec, k_series, hist):
    bar = confirm[index]
    prev = hist[index - 1] if index > 0 else None
    return {
        "close": float(bar["c"]),
        "k": None if index >= len(k_series) else k_series[index],
        "hist": None if index >= len(hist) else hist[index],
        "prevHist": prev,
        "confirmMs": spec["confirm_ms"],
        "barTs": int(bar["ts"]),
    }


def find_signals(c4, daily, weekly, variant="a", min_rr=0.0, stop_pad_pct=DEFAULT_STOP_PAD_PCT, filters=True):
    """Every confirmation-bar signal in the series, oldest first.

    Candles must already be closed. A pivot, an EMA state and an indicator
    value at bar i use only bars up to i, and a daily pivot is invisible
    until its right-hand bar has closed. Passing the whole history does not
    leak the future into an earlier signal.

    Returns (signals, bars_by_close_ms). The bar map feeds the momentum and
    time exits without a second indicator pass.
    """
    spec = variant_spec(variant)
    c4 = list(c4 or [])
    daily = list(daily or [])
    weekly = list(weekly or [])
    if spec["confirm_ms"] == H12_MS:
        confirm = aggregate_12h(c4)
    else:
        confirm = c4
    bars = {}
    if len(confirm) < 30 or len(daily) < EMA_PERIOD + PIVOT_RIGHT + 2:
        return [], bars

    daily_states = trend_states(daily)
    weekly_states = trend_states(weekly)
    daily_ema = ema_series([float(c["c"]) for c in daily])
    k_series = stoch_k_series(confirm)
    hist = squeeze_hist(confirm)
    adx = adx_series(confirm)
    atr_s = atr_series(c4)
    low_pivots = close_pivots(daily, "low")
    high_pivots = close_pivots(daily, "high")

    signals = []
    for side in spec["sides"]:
        pivots = low_pivots if side == "long" else high_pivots
        sweep_of = {}
        for pivot in pivots:
            sweep_of[id(pivot)] = None
            for i, bar in enumerate(confirm):
                if int(bar["ts"]) < pivot["known_ms"]:
                    continue
                if side == "long" and float(bar["l"]) < pivot["price"]:
                    sweep_of[id(pivot)] = i
                    break
                if side == "short" and float(bar["h"]) > pivot["price"]:
                    sweep_of[id(pivot)] = i
                    break

        armed = None
        sweep_i = None
        extreme = None
        streak = 0
        for i, bar in enumerate(confirm):
            close_ms = int(bar["ts"]) + spec["confirm_ms"]
            if side == spec["sides"][0]:
                bars[close_ms] = _bar_snapshot(confirm, i, spec, k_series, hist)
            trend = trend_at(daily, weekly, close_ms, daily_states, weekly_states)
            window_start = int(bar["ts"]) - PIVOT_LOOKBACK_MS
            known = [
                pivot for pivot in pivots
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
                armed = important
                sweep_i = i
                extreme = float(bar["l"] if side == "long" else bar["h"])
                streak = 0
            elif armed is not None and extreme is not None:
                wick = float(bar["l"] if side == "long" else bar["h"])
                extreme = min(extreme, wick) if side == "long" else max(extreme, wick)

            level = None
            if armed is not None and sweep_i is not None:
                level = _confirmation_level(confirm, sweep_i, side, armed["price"])
                reclaimed = float(bar["c"]) > level if side == "long" else float(bar["c"]) < level
                streak = streak + 1 if reclaimed else 0
            else:
                streak = 0

            took_a = False
            if streak == spec["confirm_bars"] and armed is not None:
                took_a = True
                filters_ok = (not filters) or _indicators_ok(i, side, k_series, hist, adx)
                if trend == side and filters_ok:
                    signal = _pack_signal(
                        spec, side, "A", confirm, i, extreme, level, c4, atr_s,
                        k_series, hist, adx, daily, min_rr, stop_pad_pct,
                    )
                    if signal is not None:
                        signals.append(signal)
                armed = None
                sweep_i = None
                extreme = None
                streak = 0

            if took_a or armed is not None or trend != side:
                continue
            if filters and not _indicators_ok(i, side, k_series, hist, adx):
                continue
            pullback = _setup_b_extreme(
                confirm, i, spec["confirm_bars"], side, daily, daily_ema,
                high_pivots, low_pivots, close_ms, int(bar["ts"]),
            )
            if pullback is None:
                continue
            b_extreme, b_level = pullback
            signal = _pack_signal(
                spec, side, "B", confirm, i, b_extreme, b_level, c4, atr_s,
                k_series, hist, adx, daily, min_rr, stop_pad_pct,
            )
            if signal is not None:
                signals.append(signal)

    signals.sort(key=lambda row: (row["closeMs"], row["side"], row["setup"]))
    # One signal per bar and side. Setup A is sorted first only if we emit
    # both; drop B when A already took that close.
    kept = []
    seen = set()
    for signal in signals:
        key = (signal["closeMs"], signal["side"])
        if key in seen:
            continue
        seen.add(key)
        kept.append(signal)
    return kept, bars


def _setup_b_extreme(confirm, index, need, side, daily, daily_ema, high_pivots, low_pivots, close_ms, bar_ts):
    """Pullback extreme and the level it tagged, or None.

    The touch is the most extreme bar of the last few, on or before the
    first confirmation close. The close count must finish on `index`.
    """
    if index < need:
        return None
    start = max(0, index - PULLBACK_BARS)
    first_confirm = index - need + 1
    if first_confirm < start:
        return None
    window = range(start, first_confirm + 1)
    if side == "long":
        touch_i = min(window, key=lambda j: (float(confirm[j]["l"]), j))
    else:
        touch_i = max(window, key=lambda j: (float(confirm[j]["h"]), -j))
    levels = _pullback_levels(
        confirm, touch_i, daily, daily_ema, high_pivots, low_pivots, side, close_ms, bar_ts,
    )
    tagged = [level for level in levels if _tags_level(confirm[touch_i], level, side)]
    if not tagged:
        return None
    touch_price = float(confirm[touch_i]["l"] if side == "long" else confirm[touch_i]["h"])
    level = min(tagged, key=lambda value: abs(value - touch_price))
    for k in range(first_confirm, index + 1):
        close = float(confirm[k]["c"])
        if side == "long" and close <= level:
            return None
        if side == "short" and close >= level:
            return None
    prev = index - need
    if prev >= 0:
        close = float(confirm[prev]["c"])
        if side == "long" and close > level:
            return None
        if side == "short" and close < level:
            return None
    if side == "long":
        extreme = min(float(confirm[j]["l"]) for j in range(touch_i, index + 1))
    else:
        extreme = max(float(confirm[j]["h"]) for j in range(touch_i, index + 1))
    return extreme, level


def momentum_exhausted(side, k_value, hist, prev_hist):
    """Long: overbought stochastic and a falling green squeeze valley."""
    if k_value is None or hist is None or prev_hist is None:
        return False
    if side == "long":
        return k_value > 80.0 and hist > 0 and hist < prev_hist
    return k_value < 20.0 and hist < 0 and hist > prev_hist


def exit_decision(pos, bar):
    """('momentum'|'time', price) or None. Sets addBlocked on an invalidating close.

    `bar` is the snapshot from find_signals for one closed confirmation bar.
    The entry bar itself is not an exit. Later bars are counted from closeMs.
    """
    if not isinstance(pos, dict) or not isinstance(bar, dict):
        return None
    try:
        close_ms = int(pos.get("closeMs") or 0)
        bar_ts = int(bar.get("barTs") or 0)
        confirm_ms = int(bar.get("confirmMs") or pos.get("confirmMs") or 0)
    except (TypeError, ValueError):
        return None
    if confirm_ms <= 0 or bar_ts + confirm_ms <= close_ms:
        return None
    side = pos.get("side")
    extreme = pos.get("sweepExtreme")
    try:
        extreme = float(extreme)
        close = float(bar.get("close"))
    except (TypeError, ValueError):
        return None
    if side == "long" and close < extreme:
        pos["addBlocked"] = True
    elif side == "short" and close > extreme:
        pos["addBlocked"] = True
    if pos.get("momentumExit", True) and momentum_exhausted(side, bar.get("k"), bar.get("hist"), bar.get("prevHist")):
        return ("momentum", close)
    try:
        hold = int(pos.get("maxHoldBars") or 0)
    except (TypeError, ValueError):
        hold = 0
    elapsed = (bar_ts + confirm_ms - close_ms) / float(confirm_ms)
    if hold > 0 and elapsed + 1e-9 >= hold:
        return ("time", close)
    return None


def add_distance_ok(side, mark, add_price, stop):
    """True when `mark` tags the swept extreme and has not traded through the stop."""
    try:
        mark = float(mark)
        add_price = float(add_price)
        stop = float(stop)
    except (TypeError, ValueError):
        return False
    if side == "long":
        return mark <= add_price and mark > stop
    if side == "short":
        return mark >= add_price and mark < stop
    return False
