"""Fast portfolio simulator for the frozen 1.6.0 search.

Fills follow the add-on: taker fee and adverse slippage on market orders,
a gap fills at the next print, and inside a bar the path is open, near
extreme, far extreme, close. A stop is tested before a target. Nothing in
here picks a parameter from a result.
"""

import bisect
import math
import random

BAR_MS = 15 * 60 * 1000
INITIAL = 1000.0
TAKER = 0.0006
SLIP = 0.0002
MAKER = 0.0002
# The 1.5.0 paper book risks 1% of the realized balance. The earlier study
# used 2%. Dollar PnL here is the 1% book; E[R] and the profit factor do
# not depend on that choice when the leverage cap is slack.
RISK_PCT = 0.01
LEVERAGE = 3
MAX_POSITIONS = 3
MAX_STOP = 0.08
# A funding print beyond this, in the trade's direction, blocks the entry.
# 0.03% per 8h is about three times a normal crypto perp print. Fixed here.
EXTREME_FUNDING = 0.0003
MIN_TRAIN_TRADES = 40
# Plateau around the best training PnL. Fixed before the run.
PLATEAU_USDT = 50.0
PLATEAU_FRAC = 0.15


def norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def norm_ppf(p):
    """Inverse standard normal. Acklam's approximation, accurate to ~1e-9."""
    if p <= 0.0 or p >= 1.0:
        raise ValueError("ppf domain")
    a = (
        -3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
        1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00,
    )
    b = (
        -5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
        6.680131188771972e+01, -1.328068155288572e+01,
    )
    c = (
        -7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
        -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00,
    )
    d = (
        7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
        3.754408661907416e+00,
    )
    plow = 0.02425
    phigh = 1.0 - plow
    if p < plow:
        q = math.sqrt(-2.0 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
            (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0
        )
    if p > phigh:
        q = math.sqrt(-2.0 * math.log(1.0 - p))
        return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
            (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0
        )
    q = p - 0.5
    r = q * q
    return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / (
        ((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1.0
    )


def _worse(side, price, closing, slip):
    if slip <= 0:
        return price
    worse_up = (closing and side == "short") or ((not closing) and side == "long")
    if worse_up:
        return price * (1.0 + slip)
    return price * (1.0 - slip)


def _path_points(prev, bar):
    """(p0, p1, gap) segments. A gap is the jump into the open."""
    o, h, l, c = bar
    out = []
    if prev is not None and prev != o:
        out.append((prev, o, True))
    seq = (o, l, h, c) if c >= o else (o, h, l, c)
    for left, right in zip(seq, seq[1:]):
        if left != right:
            out.append((left, right, False))
    if not out:
        out.append((o, o, True))
    return out


def _between(p0, p1, level):
    if level is None:
        return False
    if p1 > p0:
        return p0 < level < p1
    if p1 < p0:
        return p1 < level < p0
    return False


def funding_blocks(side, rate, limit):
    if rate is None or limit is None or limit <= 0:
        return False
    if side == "long" and rate > limit:
        return True
    if side == "short" and rate < -limit:
        return True
    return False


def last_funding(series, at_ms):
    """Last rate at or before at_ms. `series` is sorted (time, rate)."""
    if not series:
        return None
    lo, hi = 0, len(series)
    while lo < hi:
        mid = (lo + hi) // 2
        if series[mid][0] <= at_ms:
            lo = mid + 1
        else:
            hi = mid
    if lo == 0:
        return None
    return series[lo - 1][1]


def accept_event(ev, spec):
    """True when this prepared signal is allowed to open under `spec`."""
    if spec["family"] != ev["family"]:
        return False
    if spec["family"] == "trend" and ev.get("config") != spec["name"]:
        return False
    if spec["family"] != "course":
        return True
    if spec.get("setups") == "sweep" and ev.get("setup") != "A":
        return False
    if spec.get("sides") == "long" and ev["side"] != "long":
        return False
    if spec.get("weekly") and ev.get("weekly") != ev["side"]:
        return False
    price = ev["price"]
    stop = ev["sl"]
    if price <= 0 or stop is None:
        return False
    dist = abs(price - stop)
    if dist <= 0 or dist > price * float(spec.get("max_stop_pct") or MAX_STOP):
        return False
    if spec.get("target", "swing") == "swing":
        target = ev.get("tp")
        if target is None:
            return False
        risk = dist
        reward = abs(target - price)
        min_rr = float(spec.get("min_rr") or 0.0)
        if min_rr and reward / risk < min_rr:
            return False
        if ev["side"] == "long" and not (stop < price < target):
            return False
        if ev["side"] == "short" and not (target < price < stop):
            return False
    if funding_blocks(ev["side"], ev.get("funding"), spec.get("funding_max")):
        return False
    return True


def _size(balance, entry, stop, room, vol_target, ann_vol):
    """Notional, risk and entry fee rate's risk amount. None when refused."""
    dist = abs(entry - stop)
    if dist <= 0 or entry <= 0 or dist > entry * MAX_STOP:
        return None
    if vol_target and ann_vol and ann_vol > 0:
        notional = balance * float(vol_target) / ann_vol
    else:
        leg_risk = balance * RISK_PCT
        notional = leg_risk / (dist / entry)
    notional = round(notional, 2)
    if room <= 0 or notional > room:
        notional = math.floor(max(0.0, room) * 100 + 1e-9) / 100.0
    if notional <= 0:
        return None
    leg_risk = notional * (dist / entry)
    return notional, leg_risk


def _open_position(ev, balance, room, spec, decision):
    """Return (pos, balance, entry_fee) or None when the fill is refused."""
    side = ev["side"]
    maker = bool(spec.get("maker")) and ev["family"] == "course"
    slip = 0.0 if maker else SLIP
    fee_rate = MAKER if maker else TAKER
    signal = ev["price"]
    stop = ev["sl"]
    entry = _worse(side, signal, closing=False, slip=slip)
    sized = _size(balance, entry, stop, room, spec.get("vol_target"), ev.get("ann_vol"))
    if sized is None:
        return None
    notional, leg_risk = sized
    if side == "long" and not (stop < entry):
        return None
    if side == "short" and not (entry < stop):
        return None
    dist = abs(entry - stop)
    if spec.get("target") == "r":
        r_mult = float(spec.get("target_r") or 0.0)
        if r_mult <= 0:
            target = None
        elif side == "long":
            target = entry + dist * r_mult
        else:
            target = entry - dist * r_mult
    elif spec.get("target") == "none" or spec["family"] == "trend":
        target = None
    else:
        target = ev.get("tp")
        if side == "long" and not (entry < target):
            return None
        if side == "short" and not (target < entry):
            return None
    entry_fee = notional * fee_rate
    balance = round(balance - entry_fee, 2)
    partial_r = float(spec.get("partial_r") or 0.0)
    partial_level = None
    if partial_r > 0:
        partial_level = entry + dist * partial_r if side == "long" else entry - dist * partial_r
    pos = {
        "pair": ev["pair"],
        "side": side,
        "entry": entry,
        "sl": stop,
        "tp": target,
        "notional": notional,
        "initial_notional": notional,
        "risk": round(leg_risk, 2),
        "initial_risk": round(leg_risk, 2),
        "dist": dist,
        "entry_fee": entry_fee,
        "best": entry,
        "partial_taken": False,
        "partial_level": partial_level,
        "partial_frac": float(spec.get("partial_frac") or 0.5),
        "be_r": float(spec.get("be_r") or 0.0),
        "be_done": False,
        "trail_r": float(spec.get("trail_activate_r") or 0.0),
        "trail_dist_r": float(spec.get("trail_dist_r") or 0.0),
        "trail_on": bool(spec.get("trail_always")),
        "trail_always": bool(spec.get("trail_always")),
        "closed_pnls": [],
        "entry_ms": decision,
        "close_ms": ev.get("close_ms") or decision,
        "confirm_ms": ev.get("confirm_ms") or 0,
        "kind": ev["family"],
        "maker": maker,
    }
    return pos, balance


def _close(pos, price, fraction, balance, taker=True):
    """Close a fraction. Returns (balance, net_rounded, full)."""
    slip = SLIP if taker else 0.0
    fee_rate = TAKER if taker else MAKER
    side = pos["side"]
    fill = _worse(side, price, closing=True, slip=slip)
    entry = pos["entry"]
    notional = pos["notional"]
    if fraction < 1 and notional * (1.0 - fraction) < 0.01:
        fraction = 1.0
    closed = notional * fraction
    if side == "long":
        pnl_pct = (fill - entry) / entry
    else:
        pnl_pct = (entry - fill) / entry
    gross = pnl_pct * closed
    exit_fee = closed * fee_rate
    entry_share = pos["entry_fee"] * fraction
    net = gross - exit_fee - entry_share
    net_rounded = round(net, 2)
    balance = round(balance + gross - exit_fee, 2)
    pos["closed_pnls"].append(net_rounded)
    pos["entry_fee"] *= (1.0 - fraction)
    pos["notional"] = round(notional * (1.0 - fraction), 2)
    full = fraction >= 1 - 1e-12
    return balance, net_rounded, full


def _finish_trade(pos, decision, reason):
    trade_pnl = round(sum(pos["closed_pnls"]), 2)
    risk = pos["initial_risk"]
    trade_r = None if risk <= 0 else round(trade_pnl / risk, 4)
    return {
        "pair": pos["pair"],
        "side": pos["side"],
        "pnl": trade_pnl,
        "r": trade_r,
        "reason": reason,
        "entry_ms": pos["entry_ms"],
        "exit_ms": decision,
    }


def _stop_hit(side, price, sl):
    if side == "long":
        return price <= sl
    return price >= sl


def _tp_hit(side, price, tp):
    if tp is None:
        return False
    if side == "long":
        return price >= tp
    return price <= tp


def _r_now(pos, price):
    dist = pos["dist"]
    if dist <= 0:
        return None
    if pos["side"] == "long":
        return (price - pos["entry"]) / dist
    return (pos["entry"] - price) / dist


def _tighten(pos, price):
    """Move the chandelier. Never loosens."""
    dist = pos["dist"] * (pos["trail_dist_r"] if not pos["trail_always"] else 1.0)
    if dist <= 0:
        return
    if pos["side"] == "long":
        if price > pos["best"]:
            pos["best"] = price
        nxt = pos["best"] - dist
        if nxt > pos["sl"]:
            pos["sl"] = nxt
    else:
        if price < pos["best"]:
            pos["best"] = price
        nxt = pos["best"] + dist
        if nxt < pos["sl"]:
            pos["sl"] = nxt


def _on_price(pos, price, balance, decision, activate_partial=True):
    """Apply one print. Returns (balance, trade_or_none, still_open)."""
    side = pos["side"]
    if _stop_hit(side, price, pos["sl"]):
        reason = "trail" if pos["trail_on"] else "SL"
        if pos["be_done"] and abs(pos["sl"] - pos["entry"]) <= max(1e-8, abs(pos["entry"]) * 1e-8):
            reason = "breakeven"
        balance, _net, _full = _close(pos, price, 1.0, balance, taker=True)
        return balance, _finish_trade(pos, decision, reason), False
    if activate_partial and not pos["partial_taken"] and pos["partial_level"] is not None:
        reached = _r_now(pos, price)
        need = None
        # partial_level encodes the R; hit when price reaches it.
        if side == "long" and price >= pos["partial_level"]:
            need = True
        elif side == "short" and price <= pos["partial_level"]:
            need = True
        if need and reached is not None:
            balance, _net, full = _close(pos, pos["partial_level"], pos["partial_frac"], balance, taker=True)
            if full:
                return balance, _finish_trade(pos, decision, "partial"), False
            pos["partial_taken"] = True
            if side == "long":
                pos["sl"] = max(pos["sl"], pos["entry"])
            else:
                pos["sl"] = min(pos["sl"], pos["entry"])
            pos["be_done"] = True
    if pos["be_r"] > 0 and not pos["be_done"]:
        reached = _r_now(pos, price)
        if reached is not None and reached >= pos["be_r"]:
            if side == "long":
                pos["sl"] = max(pos["sl"], pos["entry"])
            else:
                pos["sl"] = min(pos["sl"], pos["entry"])
            pos["be_done"] = True
    if pos["tp"] is not None and _tp_hit(side, price, pos["tp"]):
        balance, _net, _full = _close(pos, price, 1.0, balance, taker=True)
        return balance, _finish_trade(pos, decision, "TP"), False
    if pos["trail_always"] or pos["trail_r"] > 0:
        reached = _r_now(pos, price)
        if pos["trail_always"] or (reached is not None and reached >= pos["trail_r"]):
            pos["trail_on"] = True
            _tighten(pos, price)
    elif pos["kind"] == "course":
        if side == "long" and price > pos["best"]:
            pos["best"] = price
        elif side == "short" and price < pos["best"]:
            pos["best"] = price
    return balance, None, True


def _walk_segment(pos, p0, p1, gap, balance, decision):
    if gap or p0 == p1:
        return _on_price(pos, p1, balance, decision)
    levels = []
    for level in (pos["sl"], pos["tp"], pos["partial_level"] if not pos["partial_taken"] else None):
        if _between(p0, p1, level):
            levels.append(level)
    if pos["be_r"] > 0 and not pos["be_done"]:
        be_level = pos["entry"] + pos["dist"] * pos["be_r"] if pos["side"] == "long" else pos["entry"] - pos["dist"] * pos["be_r"]
        if _between(p0, p1, be_level):
            levels.append(be_level)
    if pos["trail_r"] > 0 and not pos["trail_on"]:
        act = pos["entry"] + pos["dist"] * pos["trail_r"] if pos["side"] == "long" else pos["entry"] - pos["dist"] * pos["trail_r"]
        if _between(p0, p1, act):
            levels.append(act)
    levels.sort(reverse=p1 < p0)
    unique = []
    for level in levels:
        if not unique or abs(unique[-1] - level) > 1e-12:
            unique.append(level)
    for level in unique + [p1]:
        balance, trade, alive = _on_price(pos, level, balance, decision)
        if not alive:
            return balance, trade, False
    return balance, None, True


def _limit_touch(side, p0, p1, limit, gap):
    """Fill price of a resting limit on this segment, or None.

    A long buys at or below `limit`. A gap that opens through fills at the
    open. A trade through the limit fills at the limit, then the path continues.
    """
    if side == "long":
        if gap:
            return p1 if p1 <= limit else None
        if p0 <= limit:
            return p0
        if p1 <= limit:
            return limit
        return None
    if gap:
        return p1 if p1 >= limit else None
    if p0 >= limit:
        return p0
    if p1 >= limit:
        return limit
    return None


def _mom_exit(side, k_value, hist, prev):
    if k_value is None or hist is None or prev is None:
        return False
    if side == "long":
        return k_value > 80.0 and hist > 0 and hist < prev
    return k_value < 20.0 and hist < 0 and hist > prev


def _mark_unreal(pos, price):
    if pos["side"] == "long":
        return (price - pos["entry"]) / pos["entry"] * pos["notional"]
    return (pos["entry"] - price) / pos["entry"] * pos["notional"]


def _bar_from(series, pair, open_ts, cursors):
    """15m bar at exactly `open_ts`, advancing a forward cursor."""
    book = series.get(pair)
    if book is None:
        return None
    ts = book["ts"]
    i = cursors.get(pair, 0)
    n = len(ts)
    while i < n and ts[i] < open_ts:
        i += 1
    cursors[pair] = i
    if i < n and ts[i] == open_ts:
        return (book["o"][i], book["h"][i], book["l"][i], book["c"][i])
    return None


def _seed_cursors(series, open_ts):
    return {pair: bisect.bisect_left(book["ts"], open_ts) for pair, book in series.items()}


def _current_bars(series, pairs, open_ts, cursors):
    return {pair: _bar_from(series, pair, open_ts, cursors) for pair in pairs}


def simulate(clock, series, events, indicators, spec, slices):
    """Run `spec` on each slice. Each slice resets to 1,000 USDT.

    `clock` is 15-minute open times. `series[pair]` has parallel lists
    `ts, o, h, l, c`. `events` are already filtered for this spec.
    `indicators[pair][close_ms]` is a course snapshot. Trend specs carry
    `_state[pair][close_ms]` equal to 'long', 'short', or None (flat).
    A missing key means this close is not a trend decision.
    """
    by_time = {}
    for ev in events:
        by_time.setdefault(ev["t"], []).append(ev)
    state = spec.get("_state") or {}
    out = []
    pairs = list(series.keys())
    for slice_name, t0, t1 in slices:
        balance = INITIAL
        positions = {}
        pending = {}
        trades = []
        reasons = {}
        peak = INITIAL
        max_dd = 0.0
        prev = {}
        start = bisect.bisect_left(clock, t0 - BAR_MS)
        if start > 0:
            cursors = _seed_cursors(series, clock[start - 1])
            prime = _current_bars(series, pairs, clock[start - 1], cursors)
            for pair, bar in prime.items():
                if bar is not None:
                    prev[pair] = bar[3]
        cursors = _seed_cursors(series, clock[start] if start < len(clock) else t0)
        for open_ts in clock[start:]:
            decision = open_ts + BAR_MS
            if decision >= t1:
                break
            bars = _current_bars(series, pairs, open_ts, cursors)
            if decision < t0:
                for pair, bar in bars.items():
                    if bar is not None:
                        prev[pair] = bar[3]
                continue
            skip_walk = set()
            if pending:
                for pair, order in list(pending.items()):
                    if open_ts < order["live_from"]:
                        continue
                    pending.pop(pair, None)
                    bar = bars.get(pair)
                    if open_ts != order["live_from"] or bar is None or pair in positions:
                        continue
                    filled_at = None
                    remainder = []
                    pts = _path_points(prev.get(pair), bar)
                    for i, (a, b, gap) in enumerate(pts):
                        touch = _limit_touch(order["side"], a, b, order["limit"], gap)
                        if touch is None:
                            continue
                        filled_at = touch
                        remainder = [] if gap else [(touch, b, False)] + list(pts[i + 1:])
                        break
                    if filled_at is None:
                        continue
                    room = balance * LEVERAGE - sum(p["notional"] for p in positions.values())
                    opened = _open_position(order["ev"], balance, room, spec, decision)
                    if opened is None:
                        continue
                    pos, balance = opened
                    if abs(filled_at - order["limit"]) > 1e-9 * max(1.0, order["limit"]):
                        dist = abs(filled_at - pos["sl"])
                        if dist <= 0 or dist > filled_at * MAX_STOP:
                            balance = round(balance + pos["entry_fee"], 2)
                            continue
                        pos["entry"] = filled_at
                        pos["best"] = filled_at
                        pos["dist"] = dist
                        if spec.get("target") == "r":
                            r_mult = float(spec.get("target_r") or 0.0)
                            if r_mult > 0:
                                pos["tp"] = (
                                    filled_at + dist * r_mult if pos["side"] == "long"
                                    else filled_at - dist * r_mult
                                )
                        if pos["partial_level"] is not None:
                            pr = float(spec.get("partial_r") or 0.0)
                            pos["partial_level"] = (
                                filled_at + dist * pr if pos["side"] == "long"
                                else filled_at - dist * pr
                            )
                    positions[pair] = pos
                    skip_walk.add(pair)
                    pos["last"] = bar[3]
                    for a, b, gap in remainder:
                        if pair not in positions:
                            break
                        balance, trade, alive = _walk_segment(positions[pair], a, b, gap, balance, decision)
                        if not alive:
                            trades.append(trade)
                            reasons[trade["reason"]] = reasons.get(trade["reason"], 0) + 1
                            positions.pop(pair, None)
            for pair, pos in list(positions.items()):
                if pair in skip_walk:
                    continue
                bar = bars.get(pair)
                if bar is None:
                    continue
                pts = _path_points(prev.get(pair), bar)
                for a, b, gap in pts:
                    if pair not in positions:
                        break
                    balance, trade, alive = _walk_segment(positions[pair], a, b, gap, balance, decision)
                    if not alive:
                        trades.append(trade)
                        reasons[trade["reason"]] = reasons.get(trade["reason"], 0) + 1
                        positions.pop(pair, None)
                        break
                if pair in positions:
                    positions[pair]["last"] = bar[3]
            for pair, bar in bars.items():
                if bar is not None:
                    prev[pair] = bar[3]
            for pair, pos in list(positions.items()):
                if spec["family"] == "course" and (spec.get("momentum") or spec.get("hold")):
                    snap = indicators.get(pair, {}).get(decision)
                    if snap is None:
                        continue
                    bar_ts = snap["bar_ts"]
                    confirm_ms = snap["confirm_ms"]
                    if confirm_ms <= 0 or bar_ts + confirm_ms <= pos["close_ms"]:
                        continue
                    if int(pos.get("confirm_ms") or 0) not in (0, confirm_ms):
                        continue
                    exit_px = None
                    reason = None
                    if spec.get("momentum") and _mom_exit(pos["side"], snap["k"], snap["hist"], snap["prev"]):
                        exit_px = snap["close"]
                        reason = "momentum"
                    else:
                        hold = int(spec.get("hold") or 0)
                        elapsed = (bar_ts + confirm_ms - pos["close_ms"]) / float(confirm_ms)
                        if hold > 0 and elapsed + 1e-9 >= hold:
                            exit_px = snap["close"]
                            reason = "time"
                    if exit_px is not None:
                        balance, _net, _full = _close(pos, exit_px, 1.0, balance, taker=True)
                        trade = _finish_trade(pos, decision, reason)
                        trades.append(trade)
                        reasons[reason] = reasons.get(reason, 0) + 1
                        positions.pop(pair, None)
                elif spec["family"] == "trend":
                    pair_state = state.get(pair)
                    if pair_state is None or decision not in pair_state:
                        continue
                    if pair_state[decision] == pos["side"]:
                        continue
                    last = pos.get("last")
                    if last is None:
                        continue
                    balance, _net, _full = _close(pos, last, 1.0, balance, taker=True)
                    trade = _finish_trade(pos, decision, "flip")
                    trades.append(trade)
                    reasons["flip"] = reasons.get("flip", 0) + 1
                    positions.pop(pair, None)
            batch = by_time.get(decision)
            if batch and len(positions) + len(pending) < MAX_POSITIONS:
                ordered = batch
                if spec["family"] == "trend":
                    ordered = sorted(
                        batch, key=lambda ev: (-float(ev.get("strength") or 0.0), ev["pair"])
                    )
                for ev in ordered:
                    if len(positions) + len(pending) >= MAX_POSITIONS:
                        break
                    pair = ev["pair"]
                    if pair in positions or pair in pending:
                        continue
                    if spec.get("maker") and spec["family"] == "course":
                        pending[pair] = {
                            "ev": ev,
                            "side": ev["side"],
                            "limit": ev["price"],
                            "live_from": decision,
                        }
                        continue
                    room = balance * LEVERAGE - sum(p["notional"] for p in positions.values())
                    opened = _open_position(ev, balance, room, spec, decision)
                    if opened is None:
                        continue
                    pos, balance = opened
                    pos["last"] = ev["price"]
                    positions[pair] = pos
            unreal = 0.0
            for pos in positions.values():
                unreal += _mark_unreal(pos, pos.get("last", pos["entry"]))
            equity = balance + unreal
            if equity > peak:
                peak = equity
            dd = peak - equity
            if dd > max_dd:
                max_dd = dd
        max_dd_pct = (max_dd / peak * 100.0) if peak else 0.0
        out.append(_metrics(slice_name, trades, balance, max_dd_pct, reasons))
    return out


def _metrics(name, trades, balance, max_dd_pct, reasons):
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
    exp = (sum(rs) / len(rs)) if rs else None
    win_r = [t["r"] for t in trades if t["pnl"] > 0 and t.get("r") is not None]
    loss_r = [t["r"] for t in trades if t["pnl"] < 0 and t.get("r") is not None]
    long_pnl = sum(t["pnl"] for t in trades if t["side"] == "long")
    short_pnl = sum(t["pnl"] for t in trades if t["side"] == "short")
    return {
        "name": name,
        "trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": (len(wins) / len(trades)) if trades else None,
        "pf": None if pf is None else round(pf, 4),
        "expectancy": None if exp is None else round(exp, 4),
        "avg_win_r": None if not win_r else round(sum(win_r) / len(win_r), 4),
        "avg_loss_r": None if not loss_r else round(sum(loss_r) / len(loss_r), 4),
        "pnl": round(balance - INITIAL, 2),
        "pnl_pct": round((balance - INITIAL) / INITIAL * 100.0, 2),
        "max_dd_pct": round(max_dd_pct, 2),
        "reasons": reasons,
        "long_pnl": round(long_pnl, 2),
        "short_pnl": round(short_pnl, 2),
        "long_n": sum(1 for t in trades if t["side"] == "long"),
        "short_n": sum(1 for t in trades if t["side"] == "short"),
        "rs": rs,
        "pnls": [t["pnl"] for t in trades],
    }


def pool(rows):
    rs = [v for row in rows for v in row["rs"]]
    pnls = [v for row in rows for v in row["pnls"]]
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
    win_rows = [row for row in rows if row.get("avg_win_r") is not None]
    # Average win/loss R is recomputed from nothing here; callers that need
    # it pass trades. The fold rows do not keep per-trade R sign lists.
    return {
        "trades": len(pnls),
        "win_rate": (len(wins) / len(pnls)) if pnls else None,
        "pf": None if pf is None else round(pf, 4),
        "expectancy": ci["mean"],
        "expectancy_lo": ci["lo"],
        "expectancy_hi": ci["hi"],
        "pnl": round(sum(row["pnl"] for row in rows), 2),
        "pnl_pct_sum": round(sum(row["pnl_pct"] for row in rows), 2),
        "worst_dd_pct": round(max(row["max_dd_pct"] for row in rows), 2) if rows else None,
        "positive_folds": sum(1 for row in rows if row.get("expectancy") is not None and row["expectancy"] > 0),
        "folds": len(rows),
        "long_pnl": round(sum(row["long_pnl"] for row in rows), 2),
        "short_pnl": round(sum(row["short_pnl"] for row in rows), 2),
        "rs": rs,
        "pnls": pnls,
        "_win_rows": len(win_rows),
    }


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
    return {"n": n, "mean": round(mean, 4), "lo": round(means[lo_i], 4), "hi": round(means[hi_i], 4)}


def trade_sharpe(rs):
    """Per-trade Sharpe, skew, and raw kurtosis. None when undefined."""
    xs = [float(v) for v in rs if v is not None]
    n = len(xs)
    if n < 3:
        return None
    mu = sum(xs) / n
    m2 = sum((x - mu) ** 2 for x in xs) / n
    if m2 <= 0:
        return None
    sd_pop = math.sqrt(m2)
    sample_var = sum((x - mu) ** 2 for x in xs) / (n - 1)
    if sample_var <= 0:
        return None
    skew = (sum((x - mu) ** 3 for x in xs) / n) / (sd_pop ** 3)
    kurt = (sum((x - mu) ** 4 for x in xs) / n) / (m2 ** 2)
    return {
        "n": n,
        "mean": mu,
        "sharpe": mu / math.sqrt(sample_var),
        "skew": skew,
        "kurt": kurt,
    }


def deflated_sharpe(observed, trial_sharpes):
    """Bailey and Lopez de Prado deflated Sharpe, as a probability.

    `observed` is the trade_sharpe() of the one strategy being judged.
    `trial_sharpes` are the per-trade Sharpes of every config in the search,
    including the winner. Returns None when the variance term is not real.
    """
    if not observed or observed["n"] < 3:
        return None
    sharpes = [float(v) for v in trial_sharpes if v is not None]
    n_trials = len(sharpes)
    if n_trials < 2:
        return None
    mean_sr = sum(sharpes) / n_trials
    var = sum((v - mean_sr) ** 2 for v in sharpes) / (n_trials - 1)
    euler = 0.5772156649015329
    z1 = norm_ppf(1.0 - 1.0 / n_trials)
    z2 = norm_ppf(1.0 - 1.0 / (n_trials * math.e))
    sr0 = math.sqrt(var) * ((1.0 - euler) * z1 + euler * z2)
    sr = observed["sharpe"]
    inside = 1.0 - observed["skew"] * sr + ((observed["kurt"] - 1.0) / 4.0) * sr * sr
    if inside <= 0:
        return {"sr": sr, "sr0": sr0, "dsr": None, "n_trials": n_trials, "inside": inside}
    z = (sr - sr0) * math.sqrt(observed["n"] - 1) / math.sqrt(inside)
    return {
        "sr": round(sr, 4),
        "sr0": round(sr0, 4),
        "dsr": round(norm_cdf(z), 4),
        "z": round(z, 4),
        "n_trials": n_trials,
        "trials_sr_mean": round(mean_sr, 4),
        "trials_sr_var": round(var, 6),
    }


def select_config(train_rows, configs, min_trades=MIN_TRAIN_TRADES):
    """Pick one config from summed training folds.

    The score is the sum of reset-fold PnL in USDT. A config under
    `min_trades` is ignored while any config clears it. Among configs
    inside the plateau band of the best score, the lowest rank wins.
    Returns (name, relaxed, plateau_names). When nothing traded, the
    baseline `sweep_ab` is the choice and relaxed is True.
    """
    by_name = {row["name"]: row for row in train_rows}
    ranked = []
    for spec in configs:
        row = by_name.get(spec["name"])
        if row is None:
            continue
        ranked.append(row)
    if not ranked:
        return "sweep_ab", True, ["sweep_ab"]
    enough = [row for row in ranked if row.get("trades", 0) >= min_trades and row.get("pnl") is not None]
    if not enough:
        # Too few trades to rank anything. Keep the monitor book.
        return "sweep_ab", True, ["sweep_ab"]
    best = max(enough, key=lambda row: row["pnl"])
    band = max(PLATEAU_USDT, PLATEAU_FRAC * abs(best["pnl"]))
    plateau = [row for row in enough if row["pnl"] >= best["pnl"] - band - 1e-9]
    rank = {spec["name"]: spec["rank"] for spec in configs}

    def key(row):
        return (rank.get(row["name"], 99), -row["pnl"], row["name"])

    plateau.sort(key=key)
    names = [row["name"] for row in plateau]
    return plateau[0]["name"], False, names


def sum_folds(rows):
    """Collapse fold rows of one config into one training score."""
    return {
        "name": rows[0]["name"] if rows else None,
        "trades": sum(row["trades"] for row in rows),
        "pnl": round(sum(row["pnl"] for row in rows), 2),
        "folds": len(rows),
    }
