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

VERSION = "1.4.0"
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
# Backtests set this to a callable returning epoch seconds. Live trading leaves it unset.
_clock = None


def now_epoch():
    if _clock is not None:
        return float(_clock())
    return time.time()


def _now():
    if _clock is not None:
        return datetime.fromtimestamp(now_epoch(), timezone.utc).astimezone().isoformat(timespec="seconds")
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _history_time():
    if _clock is not None:
        return datetime.fromtimestamp(now_epoch(), timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")
    return datetime.now().strftime("%Y-%m-%d %H:%M")


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

    Positions close only by the stop, the target, the trailing stop, a
    partial target, or a manual close. There is no time limit.

    When the add-on exports an option (run.sh), that value replaces the copy
    saved in state.json. The page can change risk and leverage until the next
    restart; the next start applies the add-on values again.
    """
    settings.pop("maxHoldHours", None)
    _option_bool(settings, "TRAILING_ENABLED", "trailingEnabled", True)
    _option_float(settings, "TRAILING_ACTIVATION_PCT", "trailingActivationPct", 1.0)
    _option_float(settings, "TRAILING_DISTANCE", "trailingDistance", 0.5)
    _option_float(settings, "TRAILING_ACTIVATION_R", "trailingActivationR", 0.0)
    _option_bool(settings, "TRAILING_KEEP_TP", "trailingKeepTp", False)
    if _env_set("ENTRY_THRESHOLD"):
        settings["entryThreshold"] = _sane_threshold(os.environ.get("ENTRY_THRESHOLD"))
    else:
        settings["entryThreshold"] = _sane_threshold(settings.get("entryThreshold", 100))
    if _env_set("MAX_POSITIONS"):
        settings["maxPositions"] = _sane_max_positions(os.environ.get("MAX_POSITIONS"))
    else:
        settings["maxPositions"] = _sane_max_positions(settings.get("maxPositions", 5))
    if _env_set("RISK_PCT"):
        settings["riskPct"] = _sane_risk_pct(os.environ.get("RISK_PCT"))
    else:
        settings["riskPct"] = _sane_risk_pct(settings.get("riskPct", 2))
    if _env_set("LEVERAGE"):
        settings["leverage"] = _sane_leverage(os.environ.get("LEVERAGE"))
    else:
        settings["leverage"] = _sane_leverage(settings.get("leverage", 3))
    _option_float(settings, "FEE_PCT_PER_SIDE", "feePctPerSide", 0.0)
    _option_float(settings, "SLIPPAGE_PCT_PER_SIDE", "slippagePctPerSide", 0.0)
    _option_float(settings, "TAKE_PROFIT_R", "takeProfitR", 1.7)
    _option_float(settings, "PARTIAL_TP_R", "partialTpR", 0.0)
    _option_float(settings, "PARTIAL_TP_FRACTION", "partialTpFraction", 0.5)
    _option_bool(settings, "PARTIAL_MOVE_BREAKEVEN", "partialMoveBreakeven", True)
    if _env_set("STOP_MODE"):
        settings["stopMode"] = _sane_stop_mode(os.environ.get("STOP_MODE"))
    else:
        settings["stopMode"] = _sane_stop_mode(settings.get("stopMode", "swing"))
    _option_int(settings, "STOP_ATR_PERIOD", "stopAtrPeriod", 14, 2, 100)
    _option_float(settings, "STOP_ATR_MULT", "stopAtrMult", 2.0)
    _option_int(settings, "PAIR_COOLDOWN_MINUTES", "pairCooldownMinutes", 0, 0, 10080)
    _option_float(settings, "MAX_SAME_DIRECTION_RISK_R", "maxSameDirectionRiskR", 0.0)
    if _env_set("REGIME_FILTER"):
        settings["regimeFilter"] = _sane_regime(os.environ.get("REGIME_FILTER"))
    else:
        settings["regimeFilter"] = _sane_regime(settings.get("regimeFilter", "off"))
    _option_float(settings, "REGIME_ADX_MIN", "regimeAdxMin", 20.0)
    _option_float(settings, "REGIME_ER_MIN", "regimeErMin", 0.2)


def _option_bool(settings, env_name, key, default):
    if _env_set(env_name):
        settings[key] = _as_bool(os.environ.get(env_name), default)
    else:
        settings[key] = _as_bool(settings.get(key), default)


def _option_float(settings, env_name, key, default):
    if _env_set(env_name):
        settings[key] = _sane_float(os.environ.get(env_name), default)
    else:
        settings[key] = _sane_float(settings.get(key, default), default)


def _option_int(settings, env_name, key, default, low, high):
    raw = os.environ.get(env_name) if _env_set(env_name) else settings.get(key, default)
    try:
        num = int(float(raw))
    except (TypeError, ValueError):
        num = int(default)
    if num < low:
        num = low
    if num > high:
        num = high
    settings[key] = num


def _settings():
    settings = STATE.get("settings") if isinstance(STATE.get("settings"), dict) else {}
    return settings


def trailing_enabled():
    return _as_bool(_settings().get("trailingEnabled"), True)


def trailing_activation_pct():
    return _sane_float(_settings().get("trailingActivationPct", 1.0), 1.0)


def trailing_distance():
    return _sane_float(_settings().get("trailingDistance", 0.5), 0.5)


def trailing_activation_r():
    """R multiple that arms the trail. 0 keeps the percent-of-notional rule."""
    return _sane_float(_settings().get("trailingActivationR", 0.0), 0.0)


def trailing_keep_tp():
    return _as_bool(_settings().get("trailingKeepTp"), False)


def fee_pct_per_side():
    return _sane_float(_settings().get("feePctPerSide", 0.0), 0.0)


def slippage_pct_per_side():
    return _sane_float(_settings().get("slippagePctPerSide", 0.0), 0.0)


def take_profit_r():
    return _sane_float(_settings().get("takeProfitR", 1.7), 1.7)


def partial_tp_r():
    return _sane_float(_settings().get("partialTpR", 0.0), 0.0)


def partial_tp_fraction():
    raw = _sane_float(_settings().get("partialTpFraction", 0.5), 0.5)
    if raw > 1:
        return 1.0
    return raw


def partial_move_breakeven():
    return _as_bool(_settings().get("partialMoveBreakeven"), True)


def stop_mode():
    return _sane_stop_mode(_settings().get("stopMode", "swing"))


def stop_atr_period():
    try:
        period = int(float(_settings().get("stopAtrPeriod", 14)))
    except (TypeError, ValueError):
        period = 14
    return min(100, max(2, period))


def stop_atr_mult():
    return _sane_float(_settings().get("stopAtrMult", 2.0), 2.0)


def pair_cooldown_minutes():
    try:
        minutes = int(float(_settings().get("pairCooldownMinutes", 0)))
    except (TypeError, ValueError):
        return 0
    return min(10080, max(0, minutes))


def max_same_direction_risk_r():
    return _sane_float(_settings().get("maxSameDirectionRiskR", 0.0), 0.0)


def regime_filter():
    return _sane_regime(_settings().get("regimeFilter", "off"))


def regime_adx_min():
    return _sane_float(_settings().get("regimeAdxMin", 20.0), 20.0)


def regime_er_min():
    return _sane_float(_settings().get("regimeErMin", 0.2), 0.2)


def _sane_risk_pct(value, fallback=2.0):
    try:
        num = float(value)
    except (TypeError, ValueError):
        return float(fallback)
    if num <= 0 or num > 100:
        return float(fallback)
    return num


def _sane_leverage(value, fallback=3):
    try:
        num = int(float(value))
    except (TypeError, ValueError):
        return int(fallback)
    if num < 1:
        return 1
    if num > 50:
        return 50
    return num


def _sane_stop_mode(value):
    text = str(value or "swing").strip().lower()
    if text in ("atr", "swing"):
        return text
    return "swing"


def _sane_regime(value):
    text = str(value or "off").strip().lower()
    if text in ("off", "slope", "adx", "er", "slope_adx", "slope_er"):
        return text
    return "off"


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
    entry = _as_price(pos.get("entry"))
    initial_sl = _as_price(pos.get("initialSl"))
    if _as_price(pos.get("initialRiskDist")) is None and entry is not None and initial_sl is not None:
        pos["initialRiskDist"] = abs(entry - initial_sl)
    if pos.get("initialRiskAmt") is None:
        risk = pos.get("riskAmt")
        notional = _as_price(pos.get("notional"))
        if risk is None and entry and initial_sl and notional:
            risk = notional * abs(entry - initial_sl) / entry
        if risk is not None:
            try:
                pos["initialRiskAmt"] = float(risk)
            except (TypeError, ValueError):
                pass
    if pos.get("openRiskAmt") is None and pos.get("initialRiskAmt") is not None:
        try:
            pos["openRiskAmt"] = float(pos["initialRiskAmt"])
        except (TypeError, ValueError):
            pass
    if pos.get("entryFee") is None:
        pos["entryFee"] = 0.0
    if pos.get("partialTaken") is None:
        pos["partialTaken"] = False
    if pos.get("tradeId") is None:
        pos["tradeId"] = _next_trade_id()
    return pos


def _next_trade_id():
    try:
        n = int(STATE.get("tradeSeq") or 0) + 1
    except (TypeError, ValueError):
        n = 1
    STATE["tradeSeq"] = n
    return n


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
    """Replace the protective stop with the trail.

    The original stop stays stored as initialSl so the distance is stable.
    The live stop is always a price; the position is never left unprotected.
    The fixed target is removed unless trailing_keep_tp is on.
    """
    if pos.get("initialTp") is None and pos.get("tp") is not None:
        pos["initialTp"] = pos["tp"]
    if not trailing_keep_tp():
        pos["tp"] = None
    pos["trailingActive"] = True
    tighten_stop(pos)
    if _as_price(pos.get("sl")) is None:
        restored = trailed_stop(pos)
        if restored is None:
            restored = _as_price(pos.get("initialSl"))
        if restored is not None:
            pos["sl"] = restored


def price_r(pos, price):
    """Open profit in R from the initial stop distance. Independent of size."""
    entry = _as_price(pos.get("entry"))
    dist = _as_price(pos.get("initialRiskDist"))
    mark = _as_price(price)
    if entry is None or dist is None or mark is None or dist <= 0:
        return None
    if pos.get("side") == "long":
        return (mark - entry) / dist
    return (entry - mark) / dist


def trailing_should_activate(pos, price):
    if not trailing_enabled() or pos.get("trailingActive"):
        return False
    r_needed = trailing_activation_r()
    if r_needed > 0:
        reached = price_r(pos, price)
        return reached is not None and reached >= r_needed
    pnl = position_pnl(pos, price)
    notional = _as_price(pos.get("notional"))
    if pnl is None or notional is None:
        return False
    return pnl >= notional * (trailing_activation_pct() / 100.0)


def trailing_activation_price(pos):
    """Price at which the trail arms. None when it is off or already armed."""
    if not trailing_enabled() or pos.get("trailingActive"):
        return None
    entry = _as_price(pos.get("entry"))
    if entry is None:
        return None
    r_needed = trailing_activation_r()
    if r_needed > 0:
        dist = _as_price(pos.get("initialRiskDist"))
        if dist is None or dist <= 0:
            return None
        if pos.get("side") == "long":
            return entry + dist * r_needed
        return entry - dist * r_needed
    pct = trailing_activation_pct()
    if pct <= 0:
        return None
    move = pct / 100.0
    if pos.get("side") == "long":
        return entry * (1.0 + move)
    return entry * (1.0 - move)


def partial_target_price(pos):
    """Limit price of the partial target. None when the partial is off or already filled."""
    r_needed = partial_tp_r()
    if r_needed <= 0 or pos.get("partialTaken") or partial_tp_fraction() <= 0:
        return None
    entry = _as_price(pos.get("entry"))
    dist = _as_price(pos.get("initialRiskDist"))
    if entry is None or dist is None or dist <= 0:
        return None
    if pos.get("side") == "long":
        return entry + dist * r_needed
    return entry - dist * r_needed


def _partial_hit(pos, price):
    level = partial_target_price(pos)
    if level is None:
        return False
    reached = price_r(pos, price)
    return reached is not None and reached + 1e-9 >= partial_tp_r()


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
        "cooldowns": {},
        "tradeSeq": 0,
        "settings": {
            "riskPct": _env_float("RISK_PCT", 2),
            "leverage": _env_int("LEVERAGE", 3),
            "scanSeconds": max(30, _env_int("SCAN_SECONDS", 90)),
            "trailingEnabled": True,
            "trailingActivationPct": 1.0,
            "trailingDistance": 0.5,
            "trailingActivationR": 0.0,
            "trailingKeepTp": False,
            "entryThreshold": 100,
            "maxPositions": 5,
            "feePctPerSide": 0.0,
            "slippagePctPerSide": 0.0,
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


def _true_ranges(candles):
    ranges = []
    for i in range(1, len(candles)):
        prev = candles[i - 1]["c"]
        high = candles[i]["h"]
        low = candles[i]["l"]
        ranges.append(max(high - low, abs(high - prev), abs(low - prev)))
    return ranges


def _wilder(values, period):
    if len(values) < period:
        return []
    smoothed = [sum(values[:period]) / period]
    for value in values[period:]:
        smoothed.append((smoothed[-1] * (period - 1) + value) / period)
    return smoothed


def average_true_range(candles, period=14):
    """Wilder ATR. None when the candle history is shorter than the period."""
    try:
        period = int(period)
    except (TypeError, ValueError):
        period = 14
    if period < 2 or len(candles) < period + 1:
        return None
    smoothed = _wilder(_true_ranges(candles), period)
    if not smoothed:
        return None
    return smoothed[-1]


def average_directional_index(candles, period=14):
    """Wilder ADX. None when history is too short. Range is 0 to 100."""
    try:
        period = int(period)
    except (TypeError, ValueError):
        period = 14
    if period < 2 or len(candles) < (2 * period) + 1:
        return None
    plus_dm = []
    minus_dm = []
    for i in range(1, len(candles)):
        up = candles[i]["h"] - candles[i - 1]["h"]
        down = candles[i - 1]["l"] - candles[i]["l"]
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
    if not adx_s:
        return None
    return adx_s[-1]


def efficiency_ratio(candles, period=20):
    """Kaufman efficiency ratio over `period` closes. 1 is a straight line, 0 is chop."""
    try:
        period = int(period)
    except (TypeError, ValueError):
        period = 20
    if period < 2 or len(candles) < period + 1:
        return None
    closes = [c["c"] for c in candles[-(period + 1):]]
    change = abs(closes[-1] - closes[0])
    path = 0.0
    for i in range(1, len(closes)):
        path += abs(closes[i] - closes[i - 1])
    if path <= 0:
        return 0.0
    return change / path


def ema_slope(values, period, lookback):
    """Fractional change of an EMA versus the same EMA `lookback` bars earlier.

    Positive means the average is rising. None when the series is too short.
    """
    try:
        period = int(period)
        lookback = int(lookback)
    except (TypeError, ValueError):
        return None
    if period < 2 or lookback < 1 or not values or len(values) < period + lookback:
        return None
    now = ema(values, period)
    prev = ema(values[:-lookback], period)
    if now is None or prev is None or prev == 0:
        return None
    return (now - prev) / prev


def regime_allows(row, side):
    """True when the configured regime filter lets this side open.

    off: always. slope: the daily EMA50 must not point against the trade
    (flat is allowed). adx / er: the 4H trend strength must clear the minimum.
    Combinations require both. Missing inputs fail closed when that filter is on.
    """
    mode = regime_filter()
    if mode == "off":
        return True
    if not isinstance(row, dict):
        return False
    if mode in ("slope", "slope_adx", "slope_er"):
        slope = row.get("emaSlope")
        try:
            slope = float(slope)
        except (TypeError, ValueError):
            return False
        if side == "long" and slope < 0:
            return False
        if side == "short" and slope > 0:
            return False
    if mode in ("adx", "slope_adx"):
        try:
            adx_value = float(row.get("adx"))
        except (TypeError, ValueError):
            return False
        if adx_value < regime_adx_min():
            return False
    if mode in ("er", "slope_er"):
        try:
            er_value = float(row.get("er"))
        except (TypeError, ValueError):
            return False
        if er_value < regime_er_min():
            return False
    return True


def score_setup(bias, near_ema, stoch_ok, daily_align, stoch_value, side):
    """Score from 0 to 100. 100 requires every component, including the stoch bonus.

    The previous weights summed to 105 and were then capped at 100, so a full
    setup with the bonus and one without it both became 100. Thresholds 90,
    95 and 100 therefore selected the same trades. Bias is 30 points (it was
    35) so the existing +5 bonus fits inside 100:

    - 100: bias, near EMA, daily alignment, stoch band and the stoch bonus
    - 95: the same setup without the bonus
    - 80: one of daily alignment or the stoch band is missing, bonus present
    - 75: the near-EMA component is missing, or a 20-point component and the bonus

    Nothing scores 90–94 or 81–94, so a threshold of 90 still matches 95.
    100 and 95 do not match. The comparison stays >= threshold.
    """
    score = 0
    if side == "long" and bias == "ALCISTA":
        score += 30
    if side == "short" and bias == "BAJISTA":
        score += 30
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
    return score


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
    # Daily EMA50 over five daily bars is the higher-timeframe slope. 4H ADX,
    # efficiency ratio and ATR describe the entry timeframe. They are attached
    # even when the regime filter and the ATR stop are off, so a scan can show
    # them and a backtest can reuse one pass.
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
        "atr": average_true_range(c4, stop_atr_period()),
        "adx": average_directional_index(c4, 14),
        "er": efficiency_ratio(c4, 20),
        "emaSlope": ema_slope([c["c"] for c in c_d], 50, 5),
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


def _swing_distance(entry, side, res):
    if side == "long":
        return entry - float(res["recentLow"]) * 0.996
    return float(res["recentHigh"]) * 1.004 - entry


def _stop_distance(entry, side, res):
    """Price distance from the fill to the stop. None when it is wider than 8%."""
    if stop_mode() == "atr":
        atr_value = _as_price(res.get("atr"))
        mult = stop_atr_mult()
        if atr_value is None or mult <= 0:
            add_log("ATR no disponible en %s, se usa el swing." % res.get("pair"))
            sl_dist = _swing_distance(entry, side, res)
        else:
            sl_dist = atr_value * mult
    else:
        sl_dist = _swing_distance(entry, side, res)
    if sl_dist <= entry * 0.004:
        sl_dist = entry * 0.012
    if sl_dist > entry * 0.08:
        return None
    return sl_dist


def _slip_price(side, price, closing):
    """Worse fill. A long buys higher and sells lower; a short does the opposite."""
    px = _as_price(price)
    if px is None:
        return None
    slip = slippage_pct_per_side() / 100.0
    if slip <= 0:
        return px
    worse_up = (closing and side == "short") or ((not closing) and side == "long")
    if worse_up:
        return px * (1.0 + slip)
    return px * (1.0 - slip)


def _fee_amount(notional):
    pct = fee_pct_per_side() / 100.0
    if pct <= 0:
        return 0.0
    try:
        return max(0.0, float(notional)) * pct
    except (TypeError, ValueError):
        return 0.0


def _unit_risk():
    try:
        return float(STATE["balance"]) * float(STATE["settings"]["riskPct"]) / 100.0
    except (TypeError, ValueError, KeyError):
        return 0.0


def directional_risk_ok(side, new_risk):
    """False when this entry would push same-direction open risk over the cap.

    The cap is a multiple of one full trade (balance × risk_pct). 0 disables
    it. Risk already open is the original risk of each position, so a trailed
    or partial winner still counts until it is fully closed.
    """
    cap = max_same_direction_risk_r()
    if cap <= 0:
        return True
    unit = _unit_risk()
    if unit <= 0:
        return False
    used = 0.0
    for pos in _positions():
        if not isinstance(pos, dict) or pos.get("side") != side:
            continue
        try:
            used += max(0.0, float(pos.get("initialRiskAmt") or 0.0))
        except (TypeError, ValueError):
            continue
    try:
        extra = max(0.0, float(new_risk))
    except (TypeError, ValueError):
        return False
    return used + extra <= cap * unit + 1e-6


def cooldown_active(pair, at=None):
    minutes = pair_cooldown_minutes()
    if minutes <= 0 or not pair:
        return False
    cool = STATE.get("cooldowns")
    if not isinstance(cool, dict):
        return False
    until = cool.get(pair)
    if until is None:
        return False
    try:
        until = float(until)
    except (TypeError, ValueError):
        return False
    now = now_epoch() if at is None else float(at)
    return now < until


def _remember_cooldown(pair):
    minutes = pair_cooldown_minutes()
    if minutes <= 0 or not pair:
        return
    cool = STATE.get("cooldowns")
    if not isinstance(cool, dict):
        cool = {}
        STATE["cooldowns"] = cool
    cool[pair] = now_epoch() + minutes * 60.0


def open_from_result(res, side):
    """Append one paper trade. False when the pair is taken or the size is refused.

    risk_pct is applied to the current realized balance, same as a single
    trade. If this notional would push the book past balance × leverage,
    the new trade is shrunk to the room that is left, or skipped at zero.
    Entry price is worsened by slippage. The taker fee is taken from the
    balance immediately. Both are zero unless the add-on options set them.
    """
    pair = res.get("pair")
    if not pair or _find_position(pair):
        if pair:
            add_log("Ya hay posición en %s, no se duplica." % pair)
        return False
    if cooldown_active(pair):
        return False
    if not regime_allows(res, side):
        return False
    signal = _as_price(res.get("price"))
    if signal is None:
        return False
    entry = _slip_price(side, signal, closing=False)
    sl_dist = _stop_distance(entry, side, res)
    if sl_dist is None:
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
    if not directional_risk_ok(side, risk_amt):
        add_log(
            "Riesgo %s en la misma dirección ya está en el tope (%.2f R)."
            % (side, max_same_direction_risk_r())
        )
        return False
    sl = entry - sl_dist if side == "long" else entry + sl_dist
    tp_r = take_profit_r()
    if tp_r > 0:
        tp = entry + sl_dist * tp_r if side == "long" else entry - sl_dist * tp_r
    else:
        tp = None
    entry_fee = _fee_amount(notional)
    if entry_fee:
        STATE["balance"] = round(float(STATE["balance"]) - entry_fee, 2)
    score = res["scoreLong"] if side == "long" else res["scoreShort"]
    risk_amt = round(risk_amt, 2)
    _positions().append({
        "pair": pair,
        "side": side,
        "entry": entry,
        "signalPrice": signal,
        "sl": sl,
        "tp": tp,
        "notional": notional,
        "initialNotional": notional,
        "notionalCapped": capped,
        "leverage": lev,
        "riskAmt": risk_amt,
        "initialRiskAmt": risk_amt,
        "openRiskAmt": risk_amt,
        "initialRiskDist": sl_dist,
        "entryFee": entry_fee,
        "time": _now(),
        "unrealized": 0,
        "price": entry,
        "initialSl": sl,
        "initialTp": tp,
        "bestPrice": entry,
        "trailingActive": False,
        "partialTaken": False,
        "tradeId": _next_trade_id(),
        "stopMode": stop_mode(),
    })
    add_log(
        "ENTRADA AUTO %s %s @ %s (score %s) [%d/%d]"
        % (side.upper(), pair, _fmt(entry), score, len(_positions()), max_positions())
    )
    return True


def _open_risk_amt(pos):
    if pos.get("openRiskAmt") is not None:
        try:
            return max(0.0, float(pos["openRiskAmt"]))
        except (TypeError, ValueError):
            pass
    for key in ("initialRiskAmt", "riskAmt"):
        if pos.get(key) is None:
            continue
        try:
            return max(0.0, float(pos[key]))
        except (TypeError, ValueError):
            continue
    return 0.0


def _move_stop_to_breakeven(pos):
    """Tighten the live stop to the entry. Never loosens it."""
    entry = _as_price(pos.get("entry"))
    current = _as_price(pos.get("sl"))
    if entry is None or current is None:
        return False
    if pos.get("side") == "long":
        updated = max(current, entry)
    else:
        updated = min(current, entry)
    if abs(updated - current) <= 1e-12:
        return False
    pos["sl"] = updated
    return True


def close_position(reason, price, pair=None, fraction=1.0):
    """Close one open pair, or a fraction of it. With a single position, the pair can be omitted.

    `price` is the market print. Slippage worsens the fill and the taker fee
    is charged on the closed notional. The entry fee was already taken from
    the balance; the history row subtracts this leg's share so its PnL matches
    the balance change of the round trip. A fraction below 1 leaves the rest
    open and does not start the pair cooldown.
    """
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
    try:
        fraction = float(fraction)
    except (TypeError, ValueError):
        fraction = 1.0
    if fraction <= 0:
        return None
    if fraction > 1:
        fraction = 1.0
    entry = _as_price(pos.get("entry"))
    notional = _as_price(pos.get("notional"))
    fill = _slip_price(pos.get("side"), price, closing=True)
    if entry is None or notional is None or fill is None or entry <= 0:
        return None
    if fraction < 1 and notional * (1.0 - fraction) < 0.01:
        fraction = 1.0
    closed_notional = notional * fraction
    if pos["side"] == "long":
        pnl_pct = (fill - entry) / entry
    else:
        pnl_pct = (entry - fill) / entry
    gross = pnl_pct * closed_notional
    exit_fee = _fee_amount(closed_notional)
    try:
        entry_fee_total = float(pos.get("entryFee") or 0.0)
    except (TypeError, ValueError):
        entry_fee_total = 0.0
    entry_fee_share = entry_fee_total * fraction
    net = gross - exit_fee - entry_fee_share
    net_rounded = round(net, 2)
    STATE["balance"] = round(float(STATE["balance"]) + gross - exit_fee, 2)
    try:
        initial_risk = float(pos.get("initialRiskAmt") or pos.get("riskAmt") or 0.0)
    except (TypeError, ValueError):
        initial_risk = 0.0
    open_risk = _open_risk_amt(pos)
    risk_share = open_risk * fraction
    r_mult = None if risk_share <= 0 else round(net_rounded / risk_share, 4)
    closed_pnls = pos.get("closedPnls")
    if not isinstance(closed_pnls, list):
        closed_pnls = []
    closed_pnls.append(net_rounded)
    pos["closedPnls"] = closed_pnls
    trade_pnl = round(sum(closed_pnls), 2)
    full_close = fraction >= 1 - 1e-12
    trade_r = None
    if full_close and initial_risk > 0:
        trade_r = round(trade_pnl / initial_risk, 4)
    STATE["history"].insert(0, {
        "time": _history_time(),
        "pair": pos["pair"],
        "side": pos["side"],
        "entry": pos["entry"],
        "exit": fill,
        "pnl": net_rounded,
        "reason": reason,
        "initialSl": pos.get("initialSl"),
        "riskAmt": round(risk_share, 4),
        "initialRiskAmt": round(initial_risk, 4) if initial_risk else None,
        "rMultiple": r_mult,
        "tradePnl": trade_pnl if full_close else None,
        "tradeR": trade_r,
        "fees": round(exit_fee + entry_fee_share, 4),
        "notional": round(closed_notional, 2),
        "tradeId": pos.get("tradeId"),
    })
    del STATE["history"][200:]
    add_log("CERRADA %s %s → PnL %.2f USDT" % (pos["pair"], reason, net_rounded))
    if fraction >= 1 - 1e-12:
        _remember_cooldown(pos.get("pair"))
        del positions[idx]
    else:
        pos["notional"] = round(notional * (1.0 - fraction), 2)
        pos["entryFee"] = entry_fee_total * (1.0 - fraction)
        pos["openRiskAmt"] = open_risk * (1.0 - fraction)
        if pos.get("riskAmt") is not None:
            try:
                pos["riskAmt"] = round(float(pos["riskAmt"]) * (1.0 - fraction), 2)
            except (TypeError, ValueError):
                pass
        pos["partialTaken"] = True
        if partial_move_breakeven():
            _move_stop_to_breakeven(pos)
    return pos


def _stop_reason(pos):
    if pos.get("trailingActive"):
        return "trailing"
    if pos.get("partialTaken"):
        entry = _as_price(pos.get("entry"))
        sl = _as_price(pos.get("sl"))
        if entry is not None and sl is not None and abs(sl - entry) <= max(1e-8, abs(entry) * 1e-8):
            return "breakeven"
    return "SL"


def _apply_mark(pos, price):
    """SL, partial, TP or trailing for one position. The caller holds LOCK.

    Returns the exit reason, or 'partial' when a slice was closed and the
    rest is still open, or None. A stop is always checked first. A print that
    reaches the full target also fills a pending partial at the partial price
    before the remainder is closed at this print.
    """
    ensure_position_fields(pos)
    pair = pos["pair"]
    side = pos["side"]
    mark = _as_price(price)
    if mark is None:
        return None
    pnl = position_pnl(pos, mark)
    pos["price"] = mark
    if pnl is not None:
        pos["unrealized"] = round(pnl, 2)
    update_best_price(pos, mark)
    if _stop_hit(side, mark, pos.get("sl")):
        reason = _stop_reason(pos)
        close_position(reason, mark, pair)
        return reason
    if _partial_hit(pos, mark):
        level = partial_target_price(pos)
        close_position("partial", level if level is not None else mark, pair, fraction=partial_tp_fraction())
        if _find_position(pair) is None:
            return "partial"
        pos = _find_position(pair)
    if _target_hit(side, mark, pos.get("tp")):
        close_position("TP", mark, pair)
        return "TP"
    if pos.get("trailingActive"):
        moved = tighten_stop(pos)
        if moved:
            add_log("Trailing %s SL → %s (mejor %s)" % (pair, _fmt(pos["sl"]), _fmt(pos["bestPrice"])))
        if _stop_hit(side, mark, pos.get("sl")):
            close_position("trailing", mark, pair)
            return "trailing"
        return None
    if trailing_should_activate(pos, mark):
        activate_trailing(pos)
        add_log(
            "Trailing activo en %s. SL %s (mejor %s)"
            % (pair, _fmt(pos["sl"]), _fmt(pos["bestPrice"]))
        )
        if _stop_hit(side, mark, pos.get("sl")):
            close_position("trailing", mark, pair)
            return "trailing"
    return None


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
        blocked = []
        for _score, row, side in candidates:
            if len(_positions()) >= limit:
                break
            pair = row.get("pair")
            if pair and cooldown_active(pair):
                blocked.append("%s cooldown" % str(pair).replace("-USDT", ""))
                continue
            if not regime_allows(row, side):
                blocked.append("%s régimen" % str(pair).replace("-USDT", ""))
                continue
            if open_from_result(row, side):
                opened = True
        if blocked:
            add_log("Filtro: %s." % ", ".join(blocked[:8]))
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
    if trailing_activation_r() > 0:
        armed = "se activa a %sR" % _fmt_num(trailing_activation_r())
    else:
        armed = "se activa al %s%% del nocional" % _fmt_num(trailing_activation_pct())
    kept = "el TP se mantiene" if trailing_keep_tp() else "al activarse se quita el TP"
    return "%s, distancia %s del riesgo inicial, %s" % (
        armed,
        _fmt_num(trailing_distance()),
        kept,
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
        add_log(
            "Riesgo %.2f%%, apalancamiento %d (opciones del complemento; pisan el estado al arrancar)."
            % (float(STATE["settings"].get("riskPct") or 0), _leverage())
        )
        add_log(
            "Costes paper: comisión %.4f%% + slippage %.4f%% por lado."
            % (fee_pct_per_side(), slippage_pct_per_side())
        )
        add_log("Stop %s, TP %sR, parcial %sR." % (
            stop_mode(),
            _fmt_num(take_profit_r()),
            _fmt_num(partial_tp_r()) if partial_tp_r() > 0 else "off",
        ))
        add_log("Régimen: %s. Cooldown %d min. Tope misma dirección %sR." % (
            regime_filter(),
            pair_cooldown_minutes(),
            _fmt_num(max_same_direction_risk_r()) if max_same_direction_risk_r() > 0 else "off",
        ))
        add_log("Trailing: %s." % _trail_label())
        if STATE["running"]:
            add_log("Bot reanudado al arrancar el complemento.")
        save_state()
    threading.Thread(target=monitor_loop, name="granmago-monitor", daemon=True).start()
    threading.Thread(target=scan_loop, name="granmago-scan", daemon=True).start()
    log.info(
        "GranMago Scanner %s on 0.0.0.0:%s data=%s entry_threshold=%s max_positions=%s risk_pct=%s leverage=%s trailing=%s activation_pct=%s activation_r=%s distance=%s fees=%s slip=%s regime=%s",
        VERSION,
        PORT,
        DATA_DIR,
        entry_threshold(),
        max_positions(),
        _settings().get("riskPct"),
        _leverage(),
        trailing_enabled(),
        trailing_activation_pct(),
        trailing_activation_r(),
        trailing_distance(),
        fee_pct_per_side(),
        slippage_pct_per_side(),
        regime_filter(),
    )
    from waitress import serve
    serve(app, host="0.0.0.0", port=PORT, threads=4)


if __name__ == "__main__":
    main()
