#!/usr/bin/env python3
"""GranMago Scanner — paper trading loop for the Home Assistant add-on.

The loop lives in this process. Closing the browser does not stop it.
A full scan keeps running on the scan interval. Several positions can be
open at once, each on a different coin, up to max_positions.
"""

import json
import logging
import math
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from flask import Flask, jsonify, request

PAIRS = [
    "BTC-USDT", "ETH-USDT", "SOL-USDT", "XRP-USDT", "BNB-USDT", "ADA-USDT",
    "DOGE-USDT", "AVAX-USDT", "LINK-USDT", "DOT-USDT", "LTC-USDT", "BCH-USDT",
]

VERSION = "1.3.0"
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
STATE_FILE = DATA_DIR / "state.json"
PORT = int(os.environ.get("PORT", "8787"))
MONITOR_SECONDS = 20

_LEVELS = {
    "TRACE": "DEBUG",
    "DEBUG": "DEBUG",
    "INFO": "INFO",
    "NOTICE": "INFO",
    "WARNING": "WARNING",
    "ERROR": "ERROR",
    "FATAL": "CRITICAL",
}
_log_name = os.environ.get("LOG_LEVEL", "info").upper()
logging.basicConfig(
    level=getattr(logging, _LEVELS.get(_log_name, "INFO")),
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("granmago")

app = Flask(__name__)
SESSION = requests.Session()
SESSION.headers["User-Agent"] = "granmago-scanner/%s" % VERSION

LOCK = threading.Lock()
SCAN_LOCK = threading.Lock()
_closing = threading.Lock()
_wake = threading.Condition()
_kick_gen = 0
STOP = threading.Event()


def _now():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _env_float(name, default):
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return float(default)


def _env_int(name, default):
    try:
        return int(float(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return int(default)


def _env_set(name):
    raw = os.environ.get(name)
    return raw is not None and str(raw).strip() != ""


def _as_bool(value, default):
    if isinstance(value, bool):
        return value
    if value is None:
        return bool(default)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value != 0
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off"):
        return False
    return bool(default)


def _sane_float(value, fallback):
    """Non-negative float. Invalid or negative values use the fallback."""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return float(fallback)
    if num < 0:
        return float(fallback)
    return num


def _apply_runtime_options(settings):
    """Apply add-on options. A leftover max-hold field is ignored.

    Positions close only by the stop, the target, the trailing stop, or a
    manual close. There is no time limit.
    """
    settings.pop("maxHoldHours", None)
    if _env_set("TRAILING_ENABLED"):
        settings["trailingEnabled"] = _as_bool(os.environ.get("TRAILING_ENABLED"), True)
    else:
        settings["trailingEnabled"] = _as_bool(settings.get("trailingEnabled"), True)
    if _env_set("TRAILING_ACTIVATION_PCT"):
        settings["trailingActivationPct"] = _sane_float(os.environ.get("TRAILING_ACTIVATION_PCT"), 1.0)
    else:
        settings["trailingActivationPct"] = _sane_float(settings.get("trailingActivationPct", 1.0), 1.0)
    if _env_set("TRAILING_DISTANCE"):
        settings["trailingDistance"] = _sane_float(os.environ.get("TRAILING_DISTANCE"), 0.5)
    else:
        settings["trailingDistance"] = _sane_float(settings.get("trailingDistance", 0.5), 0.5)
    if _env_set("ENTRY_THRESHOLD"):
        settings["entryThreshold"] = _sane_threshold(os.environ.get("ENTRY_THRESHOLD"))
    else:
        settings["entryThreshold"] = _sane_threshold(settings.get("entryThreshold", 100))
    if _env_set("MAX_POSITIONS"):
        settings["maxPositions"] = _sane_max_positions(os.environ.get("MAX_POSITIONS"))
    else:
        settings["maxPositions"] = _sane_max_positions(settings.get("maxPositions", 5))


def _settings():
    settings = STATE.get("settings") if isinstance(STATE.get("settings"), dict) else {}
    return settings


def trailing_enabled():
    return _as_bool(_settings().get("trailingEnabled"), True)


def trailing_activation_pct():
    return _sane_float(_settings().get("trailingActivationPct", 1.0), 1.0)


def trailing_distance():
    return _sane_float(_settings().get("trailingDistance", 0.5), 0.5)


def _sane_threshold(value, fallback=100):
    """Integer entry score from 0 to 100. Invalid values use the fallback."""
    try:
        num = int(float(value))
    except (TypeError, ValueError):
        return int(fallback)
    if num < 0:
        return 0
    if num > 100:
        return 100
    return num


def entry_threshold():
    return _sane_threshold(_settings().get("entryThreshold", 100), 100)


def _sane_max_positions(value, fallback=5):
    """Integer slot count from 1 to 12. Invalid values use the fallback."""
    try:
        num = int(float(value))
    except (TypeError, ValueError):
        return int(fallback)
    if num < 1:
        return 1
    if num > 12:
        return 12
    return num


def max_positions():
    return _sane_max_positions(_settings().get("maxPositions", 5), 5)


def _as_price(value):
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if num <= 0:
        return None
    return num


def ensure_position_fields(pos):
    """Fill trailing fields missing from a 1.0.0 position. Does not close it."""
    if not isinstance(pos, dict):
        return pos
    if _as_price(pos.get("initialSl")) is None and _as_price(pos.get("sl")) is not None:
        pos["initialSl"] = pos["sl"]
    if pos.get("initialTp") is None and pos.get("tp") is not None:
        pos["initialTp"] = pos["tp"]
    if pos.get("trailingActive") is None:
        pos["trailingActive"] = False
    if _as_price(pos.get("bestPrice")) is None:
        # Seed with the entry. The next live check keeps whichever of the
        # entry and the current price is more favourable. A stale last mark
        # is not treated as the best price since entry.
        pos["bestPrice"] = _as_price(pos.get("entry"))
    return pos


def position_pnl(pos, price):
    entry = _as_price(pos.get("entry"))
    notional = _as_price(pos.get("notional"))
    mark = _as_price(price)
    if entry is None or notional is None or mark is None:
        return None
    if pos.get("side") == "long":
        pct = (mark - entry) / entry
    else:
        pct = (entry - mark) / entry
    return pct * notional


def trail_distance_price(pos):
    entry = _as_price(pos.get("entry"))
    initial_sl = _as_price(pos.get("initialSl"))
    if initial_sl is None:
        initial_sl = _as_price(pos.get("sl"))
    if entry is None or initial_sl is None:
        return None
    return abs(entry - initial_sl) * trailing_distance()


def update_best_price(pos, price):
    mark = _as_price(price)
    if mark is None:
        return _as_price(pos.get("bestPrice"))
    current = _as_price(pos.get("bestPrice"))
    if current is None:
        current = _as_price(pos.get("entry"))
    if current is None:
        pos["bestPrice"] = mark
        return mark
    if pos.get("side") == "short":
        pos["bestPrice"] = min(current, mark)
    else:
        pos["bestPrice"] = max(current, mark)
    return pos["bestPrice"]


def trailed_stop(pos):
    """Stop once trailing is on: breakeven floor, then only tighter from the best price."""
    entry = _as_price(pos.get("entry"))
    best = _as_price(pos.get("bestPrice"))
    dist = trail_distance_price(pos)
    if entry is None or best is None or dist is None:
        return None
    if pos.get("side") == "long":
        return max(entry, best - dist)
    return min(entry, best + dist)


def tighten_stop(pos):
    """Move the live stop to the trail if that tightens it. Never loosens."""
    candidate = trailed_stop(pos)
    current = _as_price(pos.get("sl"))
    if candidate is None or current is None:
        return False
    if pos.get("side") == "long":
        updated = max(current, candidate)
    else:
        updated = min(current, candidate)
    if abs(updated - current) <= 1e-12:
        return False
    pos["sl"] = updated
    return True


def activate_trailing(pos):
    """Drop the fixed target and replace the protective stop with the trail.

    The original stop stays stored as initialSl so the distance is stable.
    The live stop is always a price; the position is never left unprotected.
    """
    if pos.get("initialTp") is None and pos.get("tp") is not None:
        pos["initialTp"] = pos["tp"]
    pos["tp"] = None
    pos["trailingActive"] = True
    tighten_stop(pos)
    if _as_price(pos.get("sl")) is None:
        restored = trailed_stop(pos)
        if restored is None:
            restored = _as_price(pos.get("initialSl"))
        if restored is not None:
            pos["sl"] = restored


def trailing_should_activate(pos, price):
    if not trailing_enabled() or pos.get("trailingActive"):
        return False
    pnl = position_pnl(pos, price)
    notional = _as_price(pos.get("notional"))
    if pnl is None or notional is None:
        return False
    return pnl >= notional * (trailing_activation_pct() / 100.0)


def _stop_hit(side, price, sl):
    if _as_price(sl) is None or _as_price(price) is None:
        return False
    if side == "long":
        return price <= sl
    return price >= sl


def _target_hit(side, price, tp):
    if _as_price(tp) is None or _as_price(price) is None:
        return False
    if side == "long":
        return price >= tp
    return price <= tp


def kick():
    """Wake the scan and monitor loops early (start, stop, new position)."""
    global _kick_gen
    with _wake:
        _kick_gen += 1
        _wake.notify_all()


def _wait(seconds):
    """Sleep up to `seconds`. Return False when the process is stopping.

    A kick() returns early. Spurious condition wakeups wait out the remainder.
    """
    deadline = time.monotonic() + max(0.0, float(seconds))
    with _wake:
        seen = _kick_gen
        while not STOP.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return True
            _wake.wait(timeout=remaining)
            if _kick_gen != seen:
                return not STOP.is_set()
        return False


def default_state():
    balance = _env_float("INITIAL_BALANCE", 10000)
    state = {
        "running": False,
        "balance": balance,
        "initialBalance": balance,
        "openPositions": [],
        "history": [],
        "lastScan": [],
        "bestLong": None,
        "bestShort": None,
        "log": [],
        "lastTickAt": None,
        "lastScanAt": None,
        "busy": False,
        "settings": {
            "riskPct": _env_float("RISK_PCT", 2),
            "leverage": _env_int("LEVERAGE", 3),
            "scanSeconds": max(30, _env_int("SCAN_SECONDS", 90)),
            "trailingEnabled": True,
            "trailingActivationPct": 1.0,
            "trailingDistance": 0.5,
            "entryThreshold": 100,
            "maxPositions": 5,
        },
    }
    _apply_runtime_options(state["settings"])
    return state


def _positions():
    positions = STATE.get("openPositions")
    if not isinstance(positions, list):
        positions = []
        STATE["openPositions"] = positions
    return positions


def _find_position(pair):
    for pos in _positions():
        if isinstance(pos, dict) and pos.get("pair") == pair:
            return pos
    return None


def _migrate_positions(state, raw):
    """Move a saved single openPosition into the list. Does not close it.

    Entry, stop, target, size and the rest of the book stay as they were.
    Missing trailing fields are filled the same way as in 1.1.0.
    """
    positions = state.get("openPositions")
    if not isinstance(positions, list):
        positions = []
    legacy = raw.get("openPosition") if isinstance(raw, dict) else None
    if not positions and isinstance(legacy, dict) and legacy.get("pair"):
        positions = [legacy]
    cleaned = []
    seen = set()
    for pos in positions:
        if not isinstance(pos, dict) or not pos.get("pair"):
            continue
        if pos["pair"] in seen:
            continue
        seen.add(pos["pair"])
        ensure_position_fields(pos)
        cleaned.append(pos)
    state["openPositions"] = cleaned
    state.pop("openPosition", None)


def _score_num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _qualifying_side(row, threshold):
    """At most one side of a pair. The higher score wins; a tie stays long.

    A missing bias is allowed so a row already chosen as a setup still
    qualifies. A real scan always carries the weekly bias, and a score of
    100 already includes that bias.
    """
    bias = row.get("bias") if isinstance(row, dict) else None
    long_score = _score_num(row.get("scoreLong") if isinstance(row, dict) else None)
    short_score = _score_num(row.get("scoreShort") if isinstance(row, dict) else None)
    long_ok = long_score >= threshold and bias in (None, "ALCISTA")
    short_ok = short_score >= threshold and bias in (None, "BAJISTA")
    if long_ok and short_ok:
        if short_score > long_score:
            return "short", short_score
        return "long", long_score
    if long_ok:
        return "long", long_score
    if short_ok:
        return "short", short_score
    return None, None


def _leverage():
    try:
        lev = int(float(STATE["settings"].get("leverage", 1)))
    except (TypeError, ValueError):
        lev = 1
    return max(1, lev)


def _notional_room():
    """Notional still allowed under balance × leverage.

    Open positions keep the size they were given. Only a new entry is
    reduced or skipped.
    """
    used = 0.0
    for pos in _positions():
        if not isinstance(pos, dict):
            continue
        try:
            used += max(0.0, float(pos.get("notional") or 0))
        except (TypeError, ValueError):
            continue
    return float(STATE["balance"]) * _leverage() - used


def load_state():
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            base = default_state()
            if isinstance(data, dict):
                base.update({k: data[k] for k in base if k in data and k != "settings"})
                if isinstance(data.get("settings"), dict):
                    base["settings"].update(data["settings"])
            _apply_runtime_options(base["settings"])
            _migrate_positions(base, data if isinstance(data, dict) else {})
            base["busy"] = False
            return base
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("state unreadable, starting fresh: %s", exc)
    return default_state()


STATE = load_state()


def save_state():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".json.tmp")
    payload = {k: v for k, v in STATE.items() if k != "busy"}
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(STATE_FILE)


def add_log(msg):
    line = "[%s] %s" % (datetime.now().strftime("%H:%M:%S"), msg)
    STATE["log"].insert(0, line)
    del STATE["log"][80:]
    log.info(msg)


def public_state():
    with LOCK:
        snap = json.loads(json.dumps({k: v for k, v in STATE.items()}))
    snap["busy"] = SCAN_LOCK.locked()
    return snap


def ema(values, period):
    if not values or len(values) < period:
        return None
    k = 2 / (period + 1)
    e = sum(values[:period]) / period
    for price in values[period:]:
        e = price * k + e * (1 - k)
    return e


def stochastic(candles, period=14, k_smooth=3, d_smooth=3):
    if len(candles) < period + 5:
        return {"k": 50.0, "d": 50.0}
    raw = []
    for i in range(period - 1, len(candles)):
        window = candles[i - period + 1 : i + 1]
        hh = max(c["h"] for c in window)
        ll = min(c["l"] for c in window)
        close = candles[i]["c"]
        raw.append(50.0 if hh == ll else ((close - ll) / (hh - ll)) * 100)
    k_vals = []
    for i in range(k_smooth - 1, len(raw)):
        chunk = raw[i - k_smooth + 1 : i + 1]
        k_vals.append(sum(chunk) / k_smooth)
    k = k_vals[-1] if k_vals else 50.0
    tail = k_vals[-d_smooth:] or [50.0]
    d = sum(tail) / len(tail)
    return {"k": round(k, 1), "d": round(d, 1)}


def score_setup(bias, near_ema, stoch_ok, daily_align, stoch_value, side):
    score = 0
    if side == "long" and bias == "ALCISTA":
        score += 35
    if side == "short" and bias == "BAJISTA":
        score += 35
    if near_ema:
        score += 25
    if daily_align:
        score += 20
    if stoch_ok:
        score += 20
    if side == "long" and stoch_value < 60:
        score += 5
    if side == "short" and stoch_value > 40:
        score += 5
    return min(100, score)


def fetch_json(url):
    try:
        resp = SESSION.get(url, timeout=10)
        if resp.status_code != 200:
            return None
        return resp.json()
    except requests.RequestException as exc:
        log.debug("fetch failed %s: %s", url, exc)
        return None


def fetch_candles(inst_id, bar, limit=120):
    blofin = fetch_json(
        "https://openapi.blofin.com/api/v1/market/candles"
        "?instId=%s&bar=%s&limit=%s" % (inst_id, bar, limit)
    )
    if isinstance(blofin, dict) and str(blofin.get("code")) == "0" and isinstance(blofin.get("data"), list):
        rows = []
        for c in reversed(blofin["data"]):
            try:
                close = float(c[4])
            except (TypeError, ValueError, IndexError):
                continue
            if close <= 0:
                continue
            rows.append({"ts": int(float(c[0])), "o": float(c[1]), "h": float(c[2]), "l": float(c[3]), "c": close})
        if rows:
            return rows
    interval = {"4H": "4h", "1D": "1d", "1W": "1w"}.get(bar, "4h")
    symbol = inst_id.replace("-", "")
    binance = fetch_json(
        "https://api.binance.com/api/v3/klines?symbol=%s&interval=%s&limit=%s" % (symbol, interval, limit)
    )
    if isinstance(binance, list):
        rows = []
        for c in binance:
            try:
                rows.append({"ts": int(c[0]), "o": float(c[1]), "h": float(c[2]), "l": float(c[3]), "c": float(c[4])})
            except (TypeError, ValueError, IndexError):
                continue
        return rows
    return []


def fetch_price(inst_id):
    symbol = inst_id.replace("-", "")
    data = fetch_json("https://api.binance.com/api/v3/ticker/price?symbol=%s" % symbol)
    if isinstance(data, dict) and data.get("price"):
        try:
            price = float(data["price"])
            if price > 0:
                return price
        except (TypeError, ValueError):
            pass
    candles = fetch_candles(inst_id, "4H", 5)
    if candles:
        return candles[-1]["c"]
    return None


def score_market(pair, c4, c_d, c_w, threshold):
    """Score one pair from candles already fetched. None when 4H history is short."""
    if len(c4) < 50:
        return None
    last = c4[-1]
    ema50_4 = ema([c["c"] for c in c4], 50)
    ema50_d = ema([c["c"] for c in c_d], 50)
    ema50_w = ema([c["c"] for c in c_w], 50)
    bias = "NEUTRAL"
    if ema50_w:
        if last["c"] > ema50_w * 1.01:
            bias = "ALCISTA"
        elif last["c"] < ema50_w * 0.99:
            bias = "BAJISTA"
    stoch = stochastic(c4)
    near = bool(ema50_4) and abs(last["c"] - ema50_4) / ema50_4 < 0.018
    above = bool(ema50_4) and last["c"] > ema50_4
    daily_long = bool(ema50_d) and last["c"] > ema50_d
    daily_short = bool(ema50_d) and last["c"] < ema50_d
    stoch_long = 10 < stoch["k"] < 80
    stoch_short = 20 < stoch["k"] < 90
    score_long = score_setup(bias, near and above, stoch_long, daily_long, stoch["k"], "long")
    score_short = score_setup(bias, near and not above, stoch_short, daily_short, stoch["k"], "short")
    recent_low = min(c["l"] for c in c4[-10:])
    recent_high = max(c["h"] for c in c4[-10:])
    return {
        "pair": pair,
        "price": last["c"],
        "bias": bias,
        "scoreLong": score_long,
        "scoreShort": score_short,
        "nearEMA": near,
        "stoch": stoch["k"],
        "dailyLong": daily_long,
        "dailyShort": daily_short,
        "ema50_4": ema50_4,
        "recentLow": recent_low,
        "recentHigh": recent_high,
        "validLong": score_long >= threshold and bias == "ALCISTA",
        "validShort": score_short >= threshold and bias == "BAJISTA",
    }


def analyze_pair(pair, threshold=None):
    if threshold is None:
        threshold = entry_threshold()
    c4 = fetch_candles(pair, "4H", 150)
    c_d = fetch_candles(pair, "1D", 80)
    c_w = fetch_candles(pair, "1W", 60)
    return score_market(pair, c4, c_d, c_w, threshold)


def scan_all():
    if not SCAN_LOCK.acquire(blocking=False):
        return None
    try:
        with LOCK:
            add_log("Escaneo de %d pares..." % len(PAIRS))
            threshold = entry_threshold()
            save_state()
        results = []
        for pair in PAIRS:
            if STOP.is_set():
                break
            try:
                row = analyze_pair(pair, threshold)
            except Exception as exc:
                log.warning("analyze %s: %s", pair, exc)
                row = None
            if row:
                results.append(row)
            time.sleep(0.3)
        results.sort(key=lambda r: max(r["scoreLong"], r["scoreShort"]), reverse=True)
        best_l = sorted((r for r in results if r["validLong"]), key=lambda r: r["scoreLong"], reverse=True)
        best_s = sorted((r for r in results if r["validShort"]), key=lambda r: r["scoreShort"], reverse=True)
        with LOCK:
            STATE["lastScan"] = results
            STATE["bestLong"] = best_l[0] if best_l else None
            STATE["bestShort"] = best_s[0] if best_s else None
            STATE["lastScanAt"] = _now()
            STATE["lastTickAt"] = _now()
            bl = STATE["bestLong"]
            bs = STATE["bestShort"]
            add_log(
                "Escaneo terminado. Long: %s | Short: %s"
                % (
                    ("%s (%s)" % (bl["pair"], bl["scoreLong"])) if bl else "ninguno",
                    ("%s (%s)" % (bs["pair"], bs["scoreShort"])) if bs else "ninguno",
                )
            )
            save_state()
        return results
    finally:
        SCAN_LOCK.release()


def open_from_result(res, side):
    """Append one paper trade. False when the pair is taken or the size is refused.

    risk_pct is applied to the current realized balance, same as a single
    trade. If this notional would push the book past balance × leverage,
    the new trade is shrunk to the room that is left, or skipped at zero.
    """
    pair = res.get("pair")
    if not pair or _find_position(pair):
        if pair:
            add_log("Ya hay posición en %s, no se duplica." % pair)
        return False
    entry = res["price"]
    if side == "long":
        sl_dist = entry - res["recentLow"] * 0.996
    else:
        sl_dist = res["recentHigh"] * 1.004 - entry
    if sl_dist <= entry * 0.004:
        sl_dist = entry * 0.012
    if sl_dist > entry * 0.08:
        add_log("SL demasiado amplio en %s" % pair)
        return False
    risk_pct = float(STATE["settings"]["riskPct"]) / 100
    lev = _leverage()
    risk_amt = STATE["balance"] * risk_pct
    notional = round(risk_amt / (sl_dist / entry), 2)
    room = _notional_room()
    capped = False
    if room <= 0 or notional > room:
        capped = True
        notional = math.floor(max(0.0, room) * 100 + 1e-9) / 100.0
        if notional <= 0:
            add_log(
                "Sin hueco de nocional para %s (tope balance × apalancamiento)." % pair
            )
            return False
        risk_amt = notional * (sl_dist / entry)
        add_log(
            "Tamaño de %s limitado a %.2f USDT de nocional (tope balance × apalancamiento)."
            % (pair, notional)
        )
    sl = entry - sl_dist if side == "long" else entry + sl_dist
    tp = entry + sl_dist * 1.7 if side == "long" else entry - sl_dist * 1.7
    score = res["scoreLong"] if side == "long" else res["scoreShort"]
    _positions().append({
        "pair": pair,
        "side": side,
        "entry": entry,
        "sl": sl,
        "tp": tp,
        "notional": notional,
        "notionalCapped": capped,
        "leverage": lev,
        "riskAmt": round(risk_amt, 2),
        "time": _now(),
        "unrealized": 0,
        "price": entry,
        "initialSl": sl,
        "initialTp": tp,
        "bestPrice": entry,
        "trailingActive": False,
    })
    add_log(
        "ENTRADA AUTO %s %s @ %s (score %s) [%d/%d]"
        % (side.upper(), pair, _fmt(entry), score, len(_positions()), max_positions())
    )
    return True


def close_position(reason, price, pair=None):
    """Close one open pair. With a single position, the pair can be omitted."""
    positions = _positions()
    if pair is None:
        if len(positions) != 1:
            return None
        pair = positions[0].get("pair")
    idx = None
    for i, item in enumerate(positions):
        if isinstance(item, dict) and item.get("pair") == pair:
            idx = i
            break
    if idx is None:
        return None
    pos = positions[idx]
    if pos["side"] == "long":
        pnl_pct = (price - pos["entry"]) / pos["entry"]
    else:
        pnl_pct = (pos["entry"] - price) / pos["entry"]
    pnl = pnl_pct * pos["notional"]
    STATE["balance"] = round(STATE["balance"] + pnl, 2)
    STATE["history"].insert(0, {
        "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "pair": pos["pair"],
        "side": pos["side"],
        "entry": pos["entry"],
        "exit": price,
        "pnl": round(pnl, 2),
        "reason": reason,
    })
    del STATE["history"][200:]
    add_log("CERRADA %s %s → PnL %.2f USDT" % (pos["pair"], reason, pnl))
    del positions[idx]
    return pos


def _apply_mark(pos, price):
    """SL, TP or trailing for one position. The caller holds LOCK."""
    ensure_position_fields(pos)
    pair = pos["pair"]
    side = pos["side"]
    pnl = position_pnl(pos, price)
    pos["price"] = price
    if pnl is not None:
        pos["unrealized"] = round(pnl, 2)
    update_best_price(pos, price)
    if pos.get("trailingActive"):
        moved = tighten_stop(pos)
        if moved:
            add_log("Trailing %s SL → %s (mejor %s)" % (pair, _fmt(pos["sl"]), _fmt(pos["bestPrice"])))
        if _stop_hit(side, price, pos.get("sl")):
            close_position("trailing", price, pair)
    else:
        # Fixed stop and target, unchanged, until the trail takes over.
        if _stop_hit(side, price, pos.get("sl")):
            close_position("SL", price, pair)
        elif _target_hit(side, price, pos.get("tp")):
            close_position("TP", price, pair)
        elif trailing_should_activate(pos, price):
            activate_trailing(pos)
            add_log(
                "Trailing activo en %s. SL %s (mejor %s)"
                % (pair, _fmt(pos["sl"]), _fmt(pos["bestPrice"]))
            )
            if _stop_hit(side, price, pos.get("sl")):
                close_position("trailing", price, pair)


def _monitor_one(pair):
    price = fetch_price(pair)
    with LOCK:
        pos = _find_position(pair)
        if not pos:
            return
        if price is None:
            add_log("Sin precio de %s, se reintenta" % pair)
            return
        _apply_mark(pos, price)


def monitor_position():
    with LOCK:
        pairs = [p.get("pair") for p in _positions() if isinstance(p, dict) and p.get("pair")]
    if not pairs:
        return
    for pair in pairs:
        _monitor_one(pair)
    with LOCK:
        STATE["lastTickAt"] = _now()
        save_state()


def maybe_open():
    """Fill free slots from the latest scan, best score first.

    One position per coin, long or short, never both. A manual scan does
    not call this. Nothing is opened while the bot is stopped.
    """
    opened = False
    with LOCK:
        if not STATE["running"]:
            return
        limit = max_positions()
        if len(_positions()) >= limit:
            return
        threshold = entry_threshold()
        rows = STATE.get("lastScan") or []
        occupied = {p.get("pair") for p in _positions() if isinstance(p, dict)}
        candidates = []
        seen = set()
        for row in rows:
            if not isinstance(row, dict):
                continue
            pair = row.get("pair")
            if not pair or pair in occupied or pair in seen:
                continue
            side, score = _qualifying_side(row, threshold)
            if side is None:
                continue
            seen.add(pair)
            candidates.append((score, row, side))
        candidates.sort(key=lambda item: item[0], reverse=True)
        if not candidates:
            add_log("Ningún setup alcanza el umbral de entrada (%d). Esperando..." % threshold)
            save_state()
            return
        for _score, row, side in candidates:
            if len(_positions()) >= limit:
                break
            if open_from_result(row, side):
                opened = True
        save_state()
    if opened:
        kick()


def next_actions(running, has_position):
    """What the background loops should do.

    Scans stay on the scan interval whenever the bot is running, including
    while a position is open, so the ranking in the UI keeps updating.
    The 20s SL/TP check runs only when a position is open. Entering a new
    trade is decided later by maybe_open(), which fills free slots up to
    max_positions and never repeats a coin.
    """
    running = bool(running)
    has_position = bool(has_position)
    return {
        "scan": running,
        "monitor": running and has_position,
    }


def _fmt(price):
    if price >= 1000:
        return "%.0f" % price
    if price >= 1:
        return "%.2f" % price
    return "%.4f" % price


def _trail_label():
    if not trailing_enabled():
        return "desactivado, SL y TP fijos"
    return "se activa al %s%% del nocional, distancia %s del riesgo inicial" % (
        _fmt_num(trailing_activation_pct()),
        _fmt_num(trailing_distance()),
    )


def _fmt_num(value):
    text = ("%.4f" % float(value)).rstrip("0").rstrip(".")
    return text or "0"


def scan_loop():
    """Full market scan on the scan interval, whether or not a position is open."""
    while not STOP.is_set():
        with LOCK:
            running = STATE["running"]
            has_pos = bool(STATE.get("openPositions"))
            interval = max(30, int(STATE["settings"].get("scanSeconds") or 90))
        if not next_actions(running, has_pos)["scan"]:
            if not _wait(2):
                return
            continue
        try:
            scan_all()
            # maybe_open re-checks the book under the lock. A pair that
            # already has a position is skipped, in either direction.
            maybe_open()
        except Exception as exc:
            log.exception("scan")
            with LOCK:
                add_log("Fallo en el ciclo, el bot sigue: %s" % exc)
                STATE["lastTickAt"] = _now()
                save_state()
        if not _wait(interval):
            return


def monitor_loop():
    """SL/TP, then the trailing stop once it is active. Independent of the scan."""
    while not STOP.is_set():
        with LOCK:
            running = STATE["running"]
            has_pos = bool(STATE.get("openPositions"))
        if not next_actions(running, has_pos)["monitor"]:
            if not _wait(1):
                return
            continue
        try:
            monitor_position()
        except Exception as exc:
            log.exception("monitor")
            with LOCK:
                add_log("Fallo vigilando la posición, el bot sigue: %s" % exc)
                save_state()
        with LOCK:
            still_open = bool(STATE.get("openPositions"))
        if not _wait(MONITOR_SECONDS if still_open else 1):
            return


HTML_PATH = Path(__file__).resolve().parent / "static" / "index.html"


@app.route("/")
def index():
    html = HTML_PATH.read_text(encoding="utf-8")
    ingress = request.headers.get("X-Ingress-Path", "").rstrip("/")
    return html.replace("__INGRESS__", ingress)


@app.route("/api/state")
def api_state():
    return jsonify(public_state())


@app.route("/api/start", methods=["POST"])
def api_start():
    body = request.get_json(silent=True) or {}
    with LOCK:
        _apply_settings_locked(body)
        if not STATE["history"] and not _positions() and body.get("initialBalance"):
            try:
                bal = float(body["initialBalance"])
            except (TypeError, ValueError):
                bal = STATE["balance"]
            if bal > 0:
                STATE["balance"] = bal
                STATE["initialBalance"] = bal
        STATE["running"] = True
        add_log("Bot iniciado en el servidor. Sigue aunque cierres esta página.")
        save_state()
    kick()
    return jsonify(public_state())


@app.route("/api/stop", methods=["POST"])
def api_stop():
    with LOCK:
        STATE["running"] = False
        add_log("Bot detenido.")
        save_state()
    kick()
    return jsonify(public_state())


@app.route("/api/reset", methods=["POST"])
def api_reset():
    body = request.get_json(silent=True) or {}
    with LOCK:
        try:
            bal = float(body.get("initialBalance") or STATE["settings"].get("initialHint") or os.environ.get("INITIAL_BALANCE") or 10000)
        except (TypeError, ValueError):
            bal = 10000
        if bal <= 0:
            bal = 10000
        running = STATE["running"]
        settings = STATE["settings"]
        STATE.clear()
        STATE.update(default_state())
        STATE["balance"] = bal
        STATE["initialBalance"] = bal
        STATE["running"] = running
        STATE["settings"] = settings
        add_log("Reset. Balance %.2f" % bal)
        save_state()
    return jsonify(public_state())


def _apply_settings_locked(body):
    settings = STATE["settings"]
    if "riskPct" in body:
        try:
            settings["riskPct"] = min(5.0, max(0.5, float(body["riskPct"])))
        except (TypeError, ValueError):
            pass
    if "leverage" in body:
        try:
            settings["leverage"] = min(10, max(1, int(body["leverage"])))
        except (TypeError, ValueError):
            pass
    if "scanSeconds" in body:
        try:
            settings["scanSeconds"] = min(3600, max(30, int(body["scanSeconds"])))
        except (TypeError, ValueError):
            pass


@app.route("/api/settings", methods=["POST"])
def api_settings():
    body = request.get_json(silent=True) or {}
    with LOCK:
        _apply_settings_locked(body)
        save_state()
    return jsonify(public_state())


@app.route("/api/scan", methods=["POST"])
def api_scan():
    if SCAN_LOCK.locked():
        return jsonify({"ok": True, "busy": True})
    threading.Thread(target=scan_all, name="manual-scan", daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/close_position", methods=["POST"])
def api_close_position():
    """Close one paper position at the current market price.

    Body: {"pair": "ETH-USDT"}. Same balance and history path as an SL/TP
    close. Reason is 'manual'. Other open pairs stay open.
    """
    if not _closing.acquire(blocking=False):
        return jsonify({"ok": False, "error": "ocupado"}), 409
    try:
        body = request.get_json(silent=True) or {}
        pair = body.get("pair")
        if not isinstance(pair, str) or not pair.strip():
            return jsonify({"ok": False, "error": "falta_par"}), 400
        pair = pair.strip()
        with LOCK:
            if not _find_position(pair):
                return jsonify({"ok": False, "error": "sin_posicion"}), 409
        price = fetch_price(pair)
        with LOCK:
            if not _find_position(pair):
                return jsonify({"ok": False, "error": "sin_posicion"}), 409
            if price is None or price <= 0:
                add_log("No se pudo cerrar %s: sin precio de mercado." % pair)
                save_state()
                return jsonify({"ok": False, "error": "sin_precio"}), 503
            close_position("manual", price, pair)
            save_state()
        return jsonify(public_state())
    finally:
        _closing.release()


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with LOCK:
        add_log("Umbral de entrada: %d." % entry_threshold())
        add_log("Máximo de posiciones: %d (una por moneda)." % max_positions())
        add_log("Trailing: %s." % _trail_label())
        if STATE["running"]:
            add_log("Bot reanudado al arrancar el complemento.")
        save_state()
    threading.Thread(target=monitor_loop, name="granmago-monitor", daemon=True).start()
    threading.Thread(target=scan_loop, name="granmago-scan", daemon=True).start()
    log.info(
        "GranMago Scanner %s on 0.0.0.0:%s data=%s entry_threshold=%s max_positions=%s trailing=%s activation_pct=%s distance=%s",
        VERSION,
        PORT,
        DATA_DIR,
        entry_threshold(),
        max_positions(),
        trailing_enabled(),
        trailing_activation_pct(),
        trailing_distance(),
    )
    from waitress import serve
    serve(app, host="0.0.0.0", port=PORT, threads=4)


if __name__ == "__main__":
    main()
