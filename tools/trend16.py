"""Time-series trend signals for the frozen 1.6.0 search.

Decisions use only candles that have closed. A Donchian channel, an EMA and
a return lookback at bar i never read bar i+1. The entry is that bar's close.
"""

import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "granmago"))

import granmago_strategy as g  # noqa: E402

DAY_MS = g.DAY_MS
H4_MS = g.H4_MS
MAX_STOP = 0.08


def donchian_sides(candles, entry_n, exit_n):
    """'long', 'short', or None per bar. Flat until both windows exist."""
    n = len(candles)
    sides = [None] * n
    state = None
    highs = [float(c["h"]) for c in candles]
    lows = [float(c["l"]) for c in candles]
    closes = [float(c["c"]) for c in candles]
    need = max(int(entry_n), int(exit_n))
    for i in range(n):
        if i < need:
            continue
        hh = max(highs[i - entry_n:i])
        ll = min(lows[i - entry_n:i])
        eh = max(highs[i - exit_n:i])
        el = min(lows[i - exit_n:i])
        close = closes[i]
        if state == "long" and close < el:
            state = None
        elif state == "short" and close > eh:
            state = None
        if state is None:
            if close > hh:
                state = "long"
            elif close < ll:
                state = "short"
        sides[i] = state
    return sides


def _annual_vol(closes, i, per_year):
    if i < 30:
        return None
    rets = []
    for j in range(i - 29, i + 1):
        prev = closes[j - 1]
        if prev <= 0:
            return None
        rets.append(closes[j] / prev - 1.0)
    mu = sum(rets) / len(rets)
    var = sum((r - mu) ** 2 for r in rets) / (len(rets) - 1)
    if var <= 0:
        return None
    return math.sqrt(var) * math.sqrt(per_year)


def build_trend(pair, candles, bar_ms, spec):
    """Entry events and a close-time state map for one pair.

    The state map's value is the desired side after filters ('long',
    'short', or None for flat). Missing times are not decision bars.
    An entry is omitted when the ATR stop would be wider than 8%, but the
    state stays on so an open trade is not forced out by that width.
    """
    candles = list(candles or [])
    n = len(candles)
    closes = [float(c["c"]) for c in candles]
    atr = g.atr_series(candles, 14)
    mode = spec["mode"]
    lookback = int(spec["lookback"])
    if mode == "ema":
        ema = g.ema_series(closes, lookback)
        raw = [None] * n
        for i, value in enumerate(ema):
            if value is None:
                continue
            if closes[i] > value:
                raw[i] = "long"
            elif closes[i] < value:
                raw[i] = "short"
    elif mode == "tsmom":
        raw = [None] * n
        for i in range(lookback, n):
            base = closes[i - lookback]
            if base <= 0:
                continue
            ret = closes[i] / base - 1.0
            if ret > 0:
                raw[i] = "long"
            elif ret < 0:
                raw[i] = "short"
    elif mode == "donchian":
        raw = donchian_sides(candles, lookback, int(spec["exit_lookback"]))
    else:
        raise ValueError(mode)
    adx = g.adx_series(candles, 14) if spec.get("adx_min") else None
    per_year = 365.0 if bar_ms >= DAY_MS else 365.0 * 6.0
    events = []
    state = {}
    for i, candle in enumerate(candles):
        side = raw[i]
        if spec.get("sides") == "long" and side == "short":
            side = None
        if adx is not None:
            value = adx[i]
            if value is None or value < float(spec["adx_min"]):
                side = None
        close_ms = int(candle["ts"]) + bar_ms
        state[close_ms] = side
        if side not in ("long", "short"):
            continue
        atr_i = atr[i]
        price = closes[i]
        if atr_i is None or atr_i <= 0 or price <= 0:
            continue
        dist = atr_i * float(spec["atr_mult"])
        if dist <= 0 or dist > price * MAX_STOP:
            continue
        stop = price - dist if side == "long" else price + dist
        if mode == "ema":
            strength = abs(price / ema[i] - 1.0) if ema[i] else 0.0
        elif mode == "tsmom":
            base = closes[i - lookback]
            strength = abs(price / base - 1.0) if base else 0.0
        else:
            window = candles[i - lookback:i]
            if side == "long":
                level = max(float(c["h"]) for c in window)
                strength = (price - level) / atr_i
            else:
                level = min(float(c["l"]) for c in window)
                strength = (level - price) / atr_i
        events.append({
            "t": close_ms,
            "pair": pair,
            "side": side,
            "price": price,
            "sl": stop,
            "tp": None,
            "family": "trend",
            "config": spec["name"],
            "strength": strength,
            "ann_vol": _annual_vol(closes, i, per_year) if spec.get("vol_target") else None,
            "close_ms": close_ms,
            "confirm_ms": bar_ms,
            "setup": "T",
        })
    return events, state
