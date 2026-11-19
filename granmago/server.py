#!/usr/bin/env python3
"""GranMago Scanner — paper trading loop for the Home Assistant add-on.

The loop lives in this process. Closing the browser does not stop it.
A full scan keeps running on the scan interval. Several positions can be
open at once, each on a different coin, up to max_positions.
"""

import hmac
import json
import logging
import math
import os
import random
import re
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from email.utils import parsedate_to_datetime

import granmago_strategy
import blofin_trading

import requests
from flask import Flask, jsonify, request

# The 12-pair book shipped from 1.0.0 through 1.5.0. Research tools still
# walk this list (PAIRS below). It is not the add-on default anymore.
DEFAULT_PAIRS = [
    "BTC-USDT", "ETH-USDT", "SOL-USDT", "XRP-USDT", "BNB-USDT", "ADA-USDT",
    "DOGE-USDT", "AVAX-USDT", "LINK-USDT", "DOT-USDT", "LTC-USDT", "BCH-USDT",
]
# Liquid USDT swaps used in the 1.5.0 walk-forward, frozen from 24h quote
# volume on 23 Oct 2026 and listed on or before 23 Oct 2024. This is the
# add-on default. An empty or invalid pairs option falls back to it.
LIQUID_PAIRS = DEFAULT_PAIRS + [
    "ZEC-USDT", "PEPE-USDT", "1000BONK-USDT", "NEAR-USDT", "XMR-USDT",
    "INJ-USDT", "SUI-USDT", "TAO-USDT", "IOST-USDT", "XLM-USDT",
    "TRX-USDT", "HBAR-USDT", "ENA-USDT", "WLD-USDT", "AAVE-USDT", "QNT-USDT",
]
# Research universe. The live loop uses active_pairs(), not this list.
PAIRS = list(DEFAULT_PAIRS)
_PAIR_RE = re.compile(r"^[A-Z0-9]+-USDT$")
MAX_CONFIGURED_PAIRS = 40

VERSION = "1.7.6"

# Walk-forward monitoring setup. The add-on pins these on every start.
# Leftover options and a saved copy in state.json do not change them.
# Research tools can still write the settings dict after startup.
FIXED_BEHAVIOUR = {
    "scanSeconds": 90,
    "trailingEnabled": False,
    "trailingActivationPct": 1.0,
    "trailingDistance": 0.5,
    "trailingActivationR": 0.0,
    "trailingKeepTp": False,
    "entryThreshold": 100,
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
    "strategy": "granmago",
    "granmagoVariant": "a",
    "granmagoMaxAdds": 0,
    "granmagoMomentumExit": True,
    "granmagoMaxHoldBars": 15,
    "granmagoBreakevenR": 0.0,
    "granmagoMinRr": 0.0,
    "granmagoStopPadPct": 0.3,
    "granmagoStyle": "sweep",
    "granmagoSetups": "both",
}
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
STATE_FILE = DATA_DIR / "state.json"
PORT = int(os.environ.get("PORT", "8787"))
# 1.7.2: 5 s (was 20 s). The book freshness limit is 10 s, so a 20 s cycle left
# every open position "stale" for about half of each cycle in the panel.
MONITOR_SECONDS = 5

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
# One shared requests.Session (pooled keep-alive) for all public GETs; no price cache.
_SESSION = None
_SESSION_LOCK = threading.Lock()
BLOFIN_MARKET = "https://openapi.blofin.com/api/v1/market/"
# Freshness is the order book generation time (books?size=1 "ts"), which Blofin
# refreshes continuously. The ticker ts is the last trade and is not used.
QUOTE_MAX_AGE_SECONDS = 10
# Display only: how old the last executed/monitored quote may be before the
# panel calls it stale. Execution still needs a fresh quote (<= 10 s) fetched
# right before acting; this only stops the PnL/equity flicker between cycles.
DISPLAY_MAX_AGE_SECONDS = 30
INSTRUMENTS_TTL_SECONDS = 600
# Panel: Blofin account / mark data older than this is not shown (reconcile
# refreshes it every RECONCILE_SECONDS = 60 s).
ACCOUNT_DISPLAY_MAX_AGE_SECONDS = 180
# Local clock vs Blofin Date header. Freshness checks compare Blofin ts with
# the local clock, so a skew close to QUOTE_MAX_AGE_SECONDS rejects every quote.
CLOCK_SKEW_ALERT_SECONDS = 2
CLOSE_RETRY_SECONDS = 15
DEMO_MARKET = "https://demo-trading-openapi.blofin.com/api/v1/market/"
# Blofin publishes a new 4H/1D candle ~70 s after the boundary (measured
# 14/11/2026). The just-closed candle stays acceptable for 180 s.
CANDLE_CLOSE_GRACE_SECONDS = 180
HTTP_CONNECT_TIMEOUT = 3.0
HTTP_READ_TIMEOUT = 3.0
SCAN_RETRY_DELAY_SECONDS = 5
PAIR_DEGRADED_FAILURES = 3
PAIR_DEGRADED_SECONDS = 60
MAX_SPREAD_RATIO = 1.02
QUOTE_FUTURE_SECONDS = 2
SCAN_MAX_AGE_SECONDS = 180
ENTRY_MAX_DIVERGENCE = 0.05
HTTP_MAX_ATTEMPTS = 3
HTTP_BUDGET_SECONDS = 8
HTTP_MIN_INTERVAL = 0.25  # <=240 GETs/min and 1200/5min; Blofin limits 500/1500
_RATE_LOCK = threading.Lock()
_RATE_LIMIT_UNTIL = 0.0
_NEXT_REQUEST_AT = 0.0
BAR_SECONDS = {"4H": 14400, "1D": 86400, "1W": 604800}

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


def _pair_items(value):
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    text = str(value).strip()
    if not text or text.lower() in ("null", "none"):
        return []
    if text.startswith("["):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, list):
            return parsed
    return [part for part in re.split(r"[\s,]+", text) if part]


def parse_pairs(value, fallback=None):
    """USDT-swap symbols, uppercased, de-duplicated, capped.

    An empty or fully invalid value uses the 28-pair monitoring book.
    """
    if fallback is None:
        fallback = list(LIQUID_PAIRS)
    out = []
    seen = set()
    for item in _pair_items(value):
        text = str(item or "").strip().upper()
        if not text or text in seen or _PAIR_RE.match(text) is None:
            continue
        seen.add(text)
        out.append(text)
        if len(out) >= MAX_CONFIGURED_PAIRS:
            break
    return out or list(fallback)


def active_pairs():
    """Pairs the scanner walks. Open positions outside this list stay monitored.

    In demo, pairs that do not exist on the demo endpoint (e.g. IOST-USDT) are
    left out, using the cached demo instrument list (no network here)."""
    settings = _settings()
    pairs = parse_pairs(settings.get("pairs"), LIQUID_PAIRS)
    demo = _DEMO_UNIVERSE.get("ids")
    if TRADER is not None and demo:
        pairs = [p for p in pairs if p in demo]
    return pairs


_DEMO_UNIVERSE = {"ids": None, "at": None, "missing": []}


def refresh_demo_universe(force=False):
    """Public demo instrument list, cached INSTRUMENTS_TTL_SECONDS. Call
    outside LOCK. On failure the previous list (or no filter) is kept."""
    if TRADER is None:
        return _DEMO_UNIVERSE.get("ids")
    now = time.monotonic()
    if not force and _DEMO_UNIVERSE["at"] is not None and now - _DEMO_UNIVERSE["at"] < INSTRUMENTS_TTL_SECONDS:
        return _DEMO_UNIVERSE["ids"]
    body = fetch_json(DEMO_MARKET + "instruments")
    try:
        items = body["data"] if isinstance(body, dict) and str(body.get("code")) == "0" else None
        ids = {i["instId"] for i in items if isinstance(i, dict) and i.get("state") == "live"} if items else None
    except (KeyError, TypeError):
        ids = None
    if not ids:
        _record_market("demo:universe", False, last_fail_reason() or "sin lista")
        return _DEMO_UNIVERSE.get("ids")
    configured = parse_pairs(_settings().get("pairs"), LIQUID_PAIRS)
    missing = sorted(p for p in configured if p not in ids)
    if missing != _DEMO_UNIVERSE.get("missing"):
        with LOCK:
            add_log("Pares sin instrumento en Blofin demo (no se escanean en demo): %s" % (", ".join(missing) or "ninguno"))
    _DEMO_UNIVERSE.update(ids=ids, at=now, missing=missing)
    return ids


def _apply_runtime_options(settings):
    """Apply the exposed options, then pin the monitoring behaviour.

    risk, leverage, fees, slippage, max positions and pairs come from the
    environment when the add-on exported them, otherwise from the saved
    state, otherwise from the 1.5.2 defaults. Supervisor fills a default
    only when that key is missing, so an upgrade keeps a value already saved.

    pairs is the exception for an empty list. bashio::config prints nothing
    for `[]`, and run.sh still exports that empty string. An empty option
    means the 28-pair book and replaces a list left in state.json. A missing
    PAIRS variable is not an empty option: a saved list is kept.

    Strategy, variant, style, exits, trailing, regime, cooldown and the scan
    interval are fixed. Old environment variables and a saved copy of those
    keys are ignored. The page can still change risk and leverage until the
    next restart; the next start applies the add-on values again.
    """
    settings.pop("maxHoldHours", None)
    if _env_set("MAX_POSITIONS"):
        settings["maxPositions"] = _sane_max_positions(os.environ.get("MAX_POSITIONS"), 3)
    else:
        settings["maxPositions"] = _sane_max_positions(settings.get("maxPositions", 3), 3)
    if _env_set("RISK_PCT"):
        settings["riskPct"] = _sane_risk_pct(os.environ.get("RISK_PCT"), 1.0)
    else:
        settings["riskPct"] = _sane_risk_pct(settings.get("riskPct", 1), 1.0)
    if _env_set("LEVERAGE"):
        settings["leverage"] = _sane_leverage(os.environ.get("LEVERAGE"))
    else:
        settings["leverage"] = _sane_leverage(settings.get("leverage", 3))
    _option_float(settings, "FEE_PCT_PER_SIDE", "feePctPerSide", 0.06)
    _option_float(settings, "SLIPPAGE_PCT_PER_SIDE", "slippagePctPerSide", 0.02)
    if "PAIRS" in os.environ:
        settings["pairs"] = parse_pairs(os.environ.get("PAIRS"), LIQUID_PAIRS)
    else:
        settings["pairs"] = parse_pairs(settings.get("pairs"), LIQUID_PAIRS)
    for key, value in FIXED_BEHAVIOUR.items():
        settings[key] = value


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


def _sane_strategy(value):
    text = str(value or "scanner").strip().lower()
    if text in ("scanner", "granmago"):
        return text
    return "scanner"


def _sane_variant(value):
    text = str(value or "a").strip().lower()
    if text in ("a", "b"):
        return text
    return "a"


def strategy_mode():
    return _sane_strategy(_settings().get("strategy", "scanner"))


def granmago_variant():
    return _sane_variant(_settings().get("granmagoVariant", "a"))


def granmago_style():
    return granmago_strategy._style_name(_settings().get("granmagoStyle", "strict"))


def granmago_setups():
    return granmago_strategy._setups_name(_settings().get("granmagoSetups", "both"))


def granmago_max_adds():
    try:
        adds = int(float(_settings().get("granmagoMaxAdds", 0)))
    except (TypeError, ValueError):
        return 0
    return min(2, max(0, adds))


def granmago_momentum_exit():
    return _as_bool(_settings().get("granmagoMomentumExit"), True)


def granmago_max_hold_bars():
    try:
        bars = int(float(_settings().get("granmagoMaxHoldBars", 15)))
    except (TypeError, ValueError):
        return 15
    return min(40, max(0, bars))


def granmago_breakeven_r():
    return _sane_float(_settings().get("granmagoBreakevenR", 0.0), 0.0)


def granmago_min_rr():
    return _sane_float(_settings().get("granmagoMinRr", 0.0), 0.0)


def granmago_stop_pad_pct():
    return _sane_float(_settings().get("granmagoStopPadPct", 0.3), 0.3)


def _sane_risk_pct(value, fallback=1.0):
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
    except (TypeError, ValueError, OverflowError):
        return None
    if isinstance(value, bool) or not math.isfinite(num) or num <= 0:
        return None
    return num


def ensure_position_fields(pos, state=None):
    """Fill trailing fields missing from a 1.0.0 position. Does not close it.

    `state` is the book being loaded. The module-level STATE does not exist
    yet on the first import, so a position saved before trade ids cannot
    borrow the counter from it.
    """
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
        pos["tradeId"] = _next_trade_id(state)
    return pos


def _next_trade_id(state=None):
    book = STATE if state is None else state
    try:
        n = int(book.get("tradeSeq") or 0) + 1
    except (TypeError, ValueError):
        n = 1
    book["tradeSeq"] = n
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
        "usedSignals": {},
        # 1.7.1: persisted across restarts (load_state only keeps known keys).
        "ledgerMode": "paper",        # whose book is active: "paper" or "demo"
        "balanceSource": "paper",     # "paper" or "blofin-demo"
        "paperSnapshot": None,        # paper book while demo is active
        "demoLedger": None,           # demo book while paper is active
        "killSwitch": False,
        "lastReconcileAt": None,
        "exchangeAccount": None,      # last Blofin demo account/balance snapshot
        "migrations": [],
        "lastPaperRebuild": None,
        "demoBaseMigration": None,    # 1.7.6: demo initialBalance USD -> USDT     # 1.7.5: what the paper rebuild changed, if anything
        "settings": {
            "riskPct": 1.0,
            "leverage": 3,
            "maxPositions": 3,
            "feePctPerSide": 0.06,
            "slippagePctPerSide": 0.02,
            "pairs": list(LIQUID_PAIRS),
            **FIXED_BEHAVIOUR,
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


def _signal_key(pair, side, confirm_ms, close_ms):
    """Identity of one course signal: pair, direction, and candle close.

    confirm_ms separates a 4H close from a 12H close that lands on the same
    timestamp. A missing or non-positive close is not an identity, so research
    callers that omit it are not blocked from opening.
    """
    if side not in ("long", "short") or not pair:
        return None
    try:
        close = int(close_ms)
    except (TypeError, ValueError):
        return None
    if close <= 0:
        return None
    try:
        confirm = int(confirm_ms or 0)
    except (TypeError, ValueError):
        confirm = 0
    return "%s|%s|%d|%d" % (pair, side, confirm, close)


def _cap_used_signals(book):
    if len(book) <= 400:
        return book

    def _stamp(item):
        try:
            return int(item[1])
        except (TypeError, ValueError):
            return 0

    return dict(sorted(book.items(), key=_stamp)[-400:])


def _seed_used_signals(state):
    """Keep a signal consumed across a restart.

    An open position and a history row written by this version carry the
    candle close. A book saved before that field only has what usedSignals
    already recorded.
    """
    book = state.get("usedSignals")
    if not isinstance(book, dict):
        book = {}
    else:
        book = dict(book)
    rows = []
    for name in ("openPositions", "history"):
        found = state.get(name)
        if isinstance(found, list):
            rows.extend(found)
    for row in rows:
        if not isinstance(row, dict):
            continue
        key = _signal_key(row.get("pair"), row.get("side"), row.get("confirmMs"), row.get("closeMs"))
        if key and key not in book:
            book[key] = row.get("closeMs")
    state["usedSignals"] = _cap_used_signals(book)


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
        ensure_position_fields(pos, state)
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
                if "ledgerMode" not in data:
                    # Pre-1.7.1 file: the active book is demo if the balance came from Blofin.
                    base["ledgerMode"] = "demo" if data.get("balanceSource") == "blofin-demo" else "paper"
                if not isinstance(base.get("migrations"), list):
                    base["migrations"] = []
                if isinstance(data.get("settings"), dict):
                    base["settings"].update(data["settings"])
            _apply_runtime_options(base["settings"])
            _migrate_positions(base, data if isinstance(data, dict) else {})
            _seed_used_signals(base)
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
    # Never secrets: the trading block only carries mode/health, no key material.
    snap["tradingMode"] = trading_mode()
    snap["tradingError"] = TRADING_ERROR
    snap["trading"] = TRADER.status() if TRADER is not None else {"mode": trading_mode()}
    snap["market"] = market_stats()
    snap["alerts"] = list(ALERTS)[-10:]
    snap["killSwitch"] = bool(snap.get("killSwitch"))
    for name in ("paperSnapshot", "demoLedger"):
        book = snap.get(name)
        if isinstance(book, dict):  # summary only: the panel shows the active book
            snap[name] = {"balance": book.get("balance"), "initialBalance": book.get("initialBalance"),
                          "trades": len(book.get("history") or []), "openPositions": len(book.get("openPositions") or []),
                          "at": book.get("at")}
    now_ms = now_epoch() * 1000
    for pos in snap.get("openPositions", []):
        mark = pos.get("lastMarket") or {}
        # Display staleness = age of the last good Blofin quote. A single failed
        # refresh (pos["priceStale"] set by the monitor) does not blank a price
        # that is still within DISPLAY_MAX_AGE_SECONDS.
        stale = (mark.get("source") != "Blofin" or not _fresh_timestamp(mark.get("ts"), DISPLAY_MAX_AGE_SECONDS))
        pos["priceStale"] = stale
        if stale:
            pos["price"] = None
            pos["unrealized"] = None
        ex = pos.get("exchangeMark")
        if isinstance(ex, dict):
            age = _age_seconds(ex.get("atMs"), now_ms)
            if age is None or age > ACCOUNT_DISPLAY_MAX_AGE_SECONDS:
                pos["exchangeMark"] = None
            else:
                ex["ageSeconds"] = age
    acc = snap.get("exchangeAccount")
    if isinstance(acc, dict):
        acc["ageSeconds"] = _age_seconds(acc.get("fetchedAtMs"), now_ms)
    snap["display"] = _display_figures(snap)
    return snap


def _age_seconds(ms, now_ms):
    try:
        age = (now_ms - float(ms)) / 1000.0
    except (TypeError, ValueError):
        return None
    return round(age, 1) if math.isfinite(age) and age >= -QUOTE_FUTURE_SECONDS else None


def _num_or_none(value):
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _demo_equity_usdt(acc):
    """Demo account equity in USDT: details[USDT].equity; totalEquity (USD) only
    as a fallback when Blofin does not send the currency row."""
    if not isinstance(acc, dict):
        return None
    eq = _num_or_none(acc.get("equity"))
    return eq if eq is not None else _num_or_none(acc.get("totalEquity"))


DEMO_USDT_BASE_MIGRATION = "1.7.6-demo-usdt-base"


def _migrate_demo_base_usdt(acc):
    """Up to 1.7.5 the demo initialBalance was taken from totalEquity (USD).
    Convert it once to USDT with the account's own USDT/USD ratio, so PnL total
    = equity (USDT) - initial (USDT). Caller holds LOCK; demo book active."""
    done = STATE.setdefault("migrations", [])
    if DEMO_USDT_BASE_MIGRATION in done:
        return
    initial = _num_or_none(STATE.get("initialBalance"))
    eq, usd = _num_or_none(acc.get("equity")), _num_or_none(acc.get("totalEquity"))
    if initial is None:
        done.append(DEMO_USDT_BASE_MIGRATION)  # a new base is taken in USDT
        return
    if eq is None or usd is None or eq <= 0 or usd <= 0 or not 0.9 < eq / usd < 1.1:
        return  # retry on the next readable balance
    new = round(initial * eq / usd, 4)
    log.warning("Migración 1.7.6: saldo inicial DEMO de USD a USDT %.4f -> %.4f (USDT/USD %.6f).",
                initial, new, eq / usd)
    add_log("Migración 1.7.6: saldo inicial DEMO en USDT %.2f (antes %.2f en USD de Blofin)." % (new, initial))
    STATE["initialBalance"] = new
    STATE["demoBaseMigration"] = {"old": initial, "new": new, "ratio": eq / usd, "at": _now()}
    done.append(DEMO_USDT_BASE_MIGRATION)


def _display_figures(snap):
    """Balance, equity and PnL the panel shows: one source of truth, computed
    on the server clock. Demo equity comes from Blofin (it already contains the
    unrealized PnL) and is never rebuilt as balance + unrealized."""
    positions = [p for p in snap.get("openPositions") or [] if isinstance(p, dict)]
    # 1.7.4: the active BOOK decides, not only the mode. With trading blocked
    # (e.g. demo without keys, or live) the demo ledger stays active: its
    # balance is Blofin's totalEquity, so "balance + unrealized" would count
    # the open PnL twice and show a demo book as if it were paper.
    demo = snap.get("tradingMode") == "demo" or snap.get("ledgerMode") == "demo"
    initial = _num_or_none(snap.get("initialBalance"))
    out = {"mode": snap.get("tradingMode"), "balance": None, "equity": None, "equitySource": None,
           "unrealized": None, "unrealizedSource": None, "pnlTotal": None, "unavailable": None,
           "balanceSource": None, "marginUsed": None, "marginSource": None}
    local_ok = all(not p.get("priceStale") for p in positions)
    if local_ok:
        out["unrealized"] = round(sum(_num_or_none(p.get("unrealized")) or 0.0 for p in positions), 2)
        out["unrealizedSource"] = "local"
    elif demo and all((not p.get("priceStale")) or p.get("exchangeMark") for p in positions):
        out["unrealized"] = round(sum((_num_or_none(p.get("unrealized")) or 0.0) if not p.get("priceStale")
                                      else (_num_or_none(p["exchangeMark"].get("unrealizedPnl")) or 0.0)
                                      for p in positions), 2)
        out["unrealizedSource"] = "blofin"
    if demo:
        acc = snap.get("exchangeAccount") if isinstance(snap.get("exchangeAccount"), dict) else {}
        age = acc.get("ageSeconds")
        fresh = age is not None and age <= ACCOUNT_DISPLAY_MAX_AGE_SECONDS
        # 1.7.6: equity in USDT (details[USDT].equity). totalEquity is "in USD"
        # per the docs: ~0.1 % off and it did not add up with the USDT cards.
        total = _demo_equity_usdt(acc) if fresh else None
        if total is None:
            out["unavailable"] = "sin saldo Blofin reciente" if acc else "esperando el primer saldo de Blofin"
        else:
            out["equity"], out["equitySource"] = round(total, 2), "blofin"
            # 1.7.3: the card is the FREE balance (docs: details.available =
            # "Available balance"), not details.balance ("Cash balance"),
            # which next to Equity looked like money had disappeared.
            free, src = _num_or_none(acc.get("available")), "available"
            if free is None:
                free, src = _num_or_none(acc.get("balance")), "balance"
            out["balance"] = None if free is None else round(free, 2)
            out["balanceSource"] = None if free is None else src
            # Margin in use: sum of the positions' `margin` (Blofin), else
            # details.isolatedEquity - isolatedUnrealizedPnl (margin of isolated
            # positions without their PnL), else 0 when nothing is open.
            margin, msrc = _num_or_none(acc.get("positionsMargin")), "positions"
            if margin is None:
                iso, iupnl = _num_or_none(acc.get("currencyIsolatedEquity")), _num_or_none(acc.get("isolatedUnrealizedPnl"))
                if iso is not None:
                    margin, msrc = iso - (iupnl or 0.0), "isolatedEquity"
            if margin is None and not positions:
                margin, msrc = 0.0, "positions"
            out["marginUsed"] = None if margin is None else round(max(0.0, margin), 2)
            out["marginSource"] = None if margin is None else msrc
            if initial is not None:
                out["pnlTotal"] = round(total - initial, 2)
        return out
    bal = _num_or_none(snap.get("balance"))
    out["balance"] = None if bal is None else round(bal, 2)
    if bal is not None and initial is not None:
        out["pnlTotal"] = round(bal - initial, 2)
    if bal is not None and out["unrealized"] is not None:
        out["equity"], out["equitySource"] = round(bal + out["unrealized"], 2), "local"
    elif positions:
        out["unavailable"] = "sin precio Blofin fresco"
    return out


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


@dataclass(frozen=True)
class MarketQuote:
    pair: str
    price: float
    ts: int
    received: float  # monotonic: never persist/reuse a quote across a restart
    can_open: bool = True
    source: str = "Blofin"
    bid: float = None
    ask: float = None


def _fresh_timestamp(ts, max_age):
    try:
        stamp = float(ts)
        age = now_epoch() - stamp / 1000.0
        return (not isinstance(ts, bool) and math.isfinite(stamp) and stamp > 0
                and stamp.is_integer() and -QUOTE_FUTURE_SECONDS <= age <= max_age)
    except (TypeError, ValueError, OverflowError):
        return False


def valid_quote(quote, pair):
    return (isinstance(quote, MarketQuote) and quote.source == "Blofin"
            and isinstance(pair, str) and quote.pair == pair and _PAIR_RE.fullmatch(pair) is not None
            and _as_price(quote.price) is not None
            and _fresh_timestamp(quote.ts, QUOTE_MAX_AGE_SECONDS)
            and 0 <= time.monotonic() - quote.received <= QUOTE_MAX_AGE_SECONDS)


def _position_quote_valid(quote, pos):
    # Drop delayed/out-of-order responses even if they are still within TTL.
    previous = pos.get("lastMarket") or {}
    if not isinstance(previous, dict):
        return False
    try:
        return valid_quote(quote, pos.get("pair")) and quote.ts >= float(previous.get("ts") or 0)
    except (TypeError, ValueError, OverflowError):
        return False


def _quote_audit(quote):
    out = {"source": quote.source, "instId": quote.pair, "ts": quote.ts,
           "price": quote.price, "observedAt": _now()}
    if quote.bid is not None:
        out.update(bid=quote.bid, ask=quote.ask, priceBasis="mid")
    return out


def _market_warning(pair, reason):
    # Callers can be doing I/O outside LOCK; add_log itself is not thread-safe.
    with LOCK:
        add_log("Blofin %s: %s. Sin sustitución de fuente ni fill." % (pair, reason))


# ------------------------------------------------------------ observability
# Per-endpoint counters (ok, failures by exact reason, latency) and per-pair
# health. Exposed in /api/state ("market") and /api/health.
_STATS_LOCK = threading.Lock()
_MARKET_STATS = {}
_PAIR_HEALTH = {}
_FAIL = threading.local()


def _endpoint_name(url):
    if url.startswith(DEMO_MARKET):
        return "demo:" + url[len(DEMO_MARKET):].split("?", 1)[0]
    return url[len(BLOFIN_MARKET):].split("?", 1)[0] if url.startswith(BLOFIN_MARKET) else "?"


_CLOCK = {"skew": None, "samples": deque(maxlen=20), "alerted": False}


def _note_clock(date_header):
    """Local clock minus Blofin's HTTP Date (1 s resolution, median of 20)."""
    if not date_header or _clock is not None:
        return
    try:
        from email.utils import parsedate_to_datetime
        sample = time.time() - parsedate_to_datetime(date_header).timestamp()
    except (TypeError, ValueError, OverflowError):
        return
    with _STATS_LOCK:
        _CLOCK["samples"].append(sample)
        ordered = sorted(_CLOCK["samples"])
        _CLOCK["skew"] = round(ordered[len(ordered) // 2], 1)
        alert = len(ordered) >= 5 and abs(_CLOCK["skew"]) > CLOCK_SKEW_ALERT_SECONDS and not _CLOCK["alerted"]
        if alert:
            _CLOCK["alerted"] = True
        elif abs(_CLOCK["skew"]) <= CLOCK_SKEW_ALERT_SECONDS:
            _CLOCK["alerted"] = False
    if alert:
        _alert("reloj", "El reloj local difiere %.1f s de Blofin: los precios pueden rechazarse como obsoletos "
                        "(límite %d s). Sincroniza la hora (NTP) de Home Assistant." % (_CLOCK["skew"], QUOTE_MAX_AGE_SECONDS))


def _record_market(endpoint, ok, reason=None, latency=None):
    with _STATS_LOCK:
        st = _MARKET_STATS.setdefault(endpoint, {"ok": 0, "fail": 0, "reasons": {}, "lat": deque(maxlen=200)})
        if ok:
            st["ok"] += 1
        else:
            st["fail"] += 1
            key = str(reason or "desconocido")[:80]
            st["reasons"][key] = st["reasons"].get(key, 0) + 1
            if len(st["reasons"]) > 30:
                st["reasons"].pop(next(iter(st["reasons"])))
        if latency is not None:
            st["lat"].append(latency)


def _set_fail(reason):
    _FAIL.reason = reason


def last_fail_reason():
    return getattr(_FAIL, "reason", None)


def _pct(values, q):
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, int(q * len(ordered)))] * 1000, 1)


def market_stats():
    with _STATS_LOCK:
        out = {}
        for name, st in _MARKET_STATS.items():
            total = st["ok"] + st["fail"]
            out[name] = {"ok": st["ok"], "fail": st["fail"],
                         "failRate": round(st["fail"] / total, 4) if total else 0.0,
                         "reasons": dict(st["reasons"]),
                         "p50ms": _pct(st["lat"], 0.5), "p95ms": _pct(st["lat"], 0.95)}
        pairs = {p: dict(h) for p, h in _PAIR_HEALTH.items() if h.get("failures") or h.get("degraded")}
        skew = _CLOCK["skew"]
    return {"endpoints": out, "pairs": pairs, "degraded": sorted(p for p, h in pairs.items() if h.get("degraded")),
            "clockSkewSeconds": skew}


def note_pair(pair, ok, reason=None):
    """Circuit breaker: N consecutive failures or >60 s without a valid price."""
    now = time.monotonic()
    newly = recovered = False
    with _STATS_LOCK:
        h = _PAIR_HEALTH.setdefault(pair, {"failures": 0, "degraded": False, "since": None, "lastReason": None})
        if ok:
            recovered = h["degraded"]
            h.update(failures=0, degraded=False, since=None, lastReason=None)
        else:
            if h.get("lastFail") is not None and now - h["lastFail"] > PAIR_DEGRADED_SECONDS and not h["degraded"]:
                # A pair priced rarely (no position): an old failure is not a streak.
                h.update(failures=0, since=None)
            h["failures"] += 1
            h["lastReason"] = reason
            h["lastFail"] = now
            if h["since"] is None:
                h["since"] = now
            if not h["degraded"] and (h["failures"] >= PAIR_DEGRADED_FAILURES
                                      or now - h["since"] > PAIR_DEGRADED_SECONDS):
                h["degraded"] = newly = True
            h["ageSeconds"] = round(now - h["since"], 1)
    if newly:
        log.warning("Par %s DEGRADADO tras %d fallos: %s. Sin entradas nuevas en este par.",
                    pair, h["failures"], reason)
        _alert("degradado", "%s degradado: %s" % (pair, reason))
    if recovered:
        log.info("Par %s recuperado.", pair)


def pair_degraded(pair):
    with _STATS_LOCK:
        return bool((_PAIR_HEALTH.get(pair) or {}).get("degraded"))


ALERTS = deque(maxlen=50)


def _alert(kind, msg):
    ALERTS.append({"at": _now(), "kind": kind, "msg": str(msg)[:300]})


def _retry_after_seconds(value):
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        try:
            seconds = parsedate_to_datetime(value).timestamp() - now_epoch()
        except (TypeError, ValueError, OverflowError):
            return None
    return max(0.0, seconds) if math.isfinite(seconds) else None


def _wait_market_turn(deadline):
    """Space GETs across all threads, also honoring a shared Retry-After."""
    global _NEXT_REQUEST_AT
    while True:
        with _RATE_LOCK:
            now = time.monotonic()
            turn = max(now, _NEXT_REQUEST_AT, _RATE_LIMIT_UNTIL)
            if turn >= deadline:
                return False
            _NEXT_REQUEST_AT = turn + HTTP_MIN_INTERVAL
        if turn > now:
            time.sleep(turn - now)
        with _RATE_LOCK:
            if _RATE_LIMIT_UNTIL <= time.monotonic():
                return time.monotonic() < deadline
        # Another request installed a longer cooldown while this one slept.


def _session():
    global _SESSION
    with _SESSION_LOCK:
        if _SESSION is None:
            sess = requests.Session()
            adapter = requests.adapters.HTTPAdapter(pool_connections=4, pool_maxsize=8, max_retries=0)
            sess.mount("https://", adapter)
            sess.headers["User-Agent"] = "granmago-scanner/%s" % VERSION
            _SESSION = sess
        return _SESSION


def fetch_json(url, *, deadline=None):
    """Only Blofin GETs retry, never entry/close/accounting operations.

    Three attempts with jittered exponential backoff, inside one 8s request
    budget. Long Retry-After creates a process-wide cooldown and returns
    immediately rather than blocking other positions. A quote shares its
    budget across requests. There is no stale response cache. On failure the
    exact reason is available from last_fail_reason() and counted per endpoint.
    """
    global _RATE_LIMIT_UNTIL
    if not (url.startswith(BLOFIN_MARKET) or url.startswith(DEMO_MARKET)):
        raise ValueError("only Blofin public market data is allowed")
    endpoint = _endpoint_name(url)
    _set_fail(None)
    if deadline is None:
        deadline = time.monotonic() + HTTP_BUDGET_SECONDS
    for attempt in range(HTTP_MAX_ATTEMPTS):
        if not _wait_market_turn(deadline):
            log.warning("Blofin GET %s suspendido: presupuesto agotado o Retry-After activo", endpoint)
            _set_fail("presupuesto/Retry-After")
            _record_market(endpoint, False, "presupuesto/Retry-After")
            return None
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _set_fail("presupuesto agotado")
            _record_market(endpoint, False, "presupuesto agotado")
            return None
        retry_delay = 0.25 * (2 ** attempt) * (1 + random.uniform(0, 0.5))
        resp = None
        started = time.monotonic()
        try:
            # No redirect to another host/provider. Connect/read both bounded.
            timeout = (min(HTTP_CONNECT_TIMEOUT, remaining / 2), min(HTTP_READ_TIMEOUT, remaining / 2))
            resp = _session().get(url, timeout=timeout, allow_redirects=False, stream=True)
            if resp.status_code == 200:
                # Read in 16 KB blocks so a trickling body cannot defeat the
                # overall deadline by resetting the socket read timeout.
                payload = bytearray()
                for chunk in resp.iter_content(chunk_size=16384):
                    if time.monotonic() > deadline or len(payload) >= 2 * 1024 * 1024:
                        log.warning("Blofin GET %s respuesta tardía/demasiado grande; sin fill", endpoint)
                        _set_fail("respuesta tardía/grande")
                        _record_market(endpoint, False, "respuesta tardía/grande")
                        return None
                    payload.extend(chunk)
                body = json.loads(payload)
                _record_market(endpoint, True, latency=time.monotonic() - started)
                _note_clock(resp.headers.get("Date"))
                return body
            if resp.status_code not in (408, 429, 500, 502, 503, 504):
                log.warning("Blofin GET %s HTTP %s: no se reintenta", endpoint, resp.status_code)
                _set_fail("HTTP %s" % resp.status_code)
                _record_market(endpoint, False, "HTTP %s" % resp.status_code)
                return None
            retry_after = _retry_after_seconds(resp.headers.get("Retry-After"))
            if resp.status_code == 429:
                retry_after = max(1.0, retry_after or 0.0)
            if retry_after is not None:
                with _RATE_LOCK:
                    _RATE_LIMIT_UNTIL = max(_RATE_LIMIT_UNTIL, time.monotonic() + retry_after)
                retry_delay = max(retry_delay, retry_after)
            log.warning("Blofin GET %s HTTP %s, intento %d/%d", endpoint, resp.status_code, attempt + 1, HTTP_MAX_ATTEMPTS)
            _set_fail("HTTP %s" % resp.status_code)
            _record_market(endpoint, False, "HTTP %s" % resp.status_code)
        except requests.RequestException as exc:
            kind = type(exc).__name__
            log.warning("Blofin GET fallo transitorio (%s) %s, intento %d/%d", kind, endpoint, attempt + 1, HTTP_MAX_ATTEMPTS)
            _set_fail(kind)
            _record_market(endpoint, False, kind)
        except ValueError:
            log.warning("Blofin GET %s JSON inválido; sin fill", endpoint)
            _set_fail("JSON inválido")
            _record_market(endpoint, False, "JSON inválido")
            return None
        finally:
            if resp is not None:
                resp.close()
        if attempt + 1 == HTTP_MAX_ATTEMPTS or retry_delay >= deadline - time.monotonic():
            return None
        time.sleep(retry_delay)
    return None


class _Reject(ValueError):
    pass


def _api_code(body, what):
    if body is None:
        raise _Reject("%s sin respuesta (%s)" % (what, last_fail_reason() or "red"))
    if not isinstance(body, dict):
        raise _Reject("%s respuesta no es objeto" % what)
    if str(body.get("code")) != "0":
        raise _Reject("%s code=%s msg=%s" % (what, body.get("code"), str(body.get("msg"))[:60]))
    if not isinstance(body.get("data"), list) or not body["data"]:
        raise _Reject("%s data vacía" % what)
    return body["data"]


def fetch_candles(inst_id, bar, limit=120):
    """Blofin candles only. Opening timestamps are NOT fresh execution quotes.

    The just-closed candle is accepted for CANDLE_CLOSE_GRACE_SECONDS after
    the boundary, so the ~70 s Blofin publication delay is not a failure.
    Every rejection logs its exact reason and is counted per endpoint.
    """
    if not isinstance(inst_id, str) or not _PAIR_RE.fullmatch(inst_id) or bar not in BAR_SECONDS:
        _market_warning(str(inst_id), "par/intervalo inválido")
        return []
    body = fetch_json(BLOFIN_MARKET + "candles?instId=%s&bar=%s&limit=%s" % (inst_id, bar, limit))
    try:
        data = _api_code(body, "velas %s" % bar)
        rows = []
        now_ms = (now_epoch() + QUOTE_FUTURE_SECONDS) * 1000
        for c in data:
            if not isinstance(c, (list, tuple)) or len(c) < 5 or any(isinstance(v, bool) for v in c[:5]):
                raise _Reject("vela %s malformada" % bar)
            ts = int(c[0])
            o, h, l, close = (float(v) for v in c[1:5])
            if str(ts) != str(c[0]) or ts <= 0:
                raise _Reject("vela %s ts inválido %r" % (bar, c[0]))
            if ts > now_ms:
                raise _Reject("vela %s futura ts=%s" % (bar, ts))
            if (not all(math.isfinite(v) and v > 0 for v in (o, h, l, close))
                    or not l <= min(o, close) <= max(o, close) <= h):
                raise _Reject("vela %s OHLC incoherente ts=%s" % (bar, ts))
            rows.append({"ts": ts, "o": o, "h": h, "l": l, "c": close})
        rows.sort(key=lambda c: c["ts"])
        if len({c["ts"] for c in rows}) != len(rows):
            raise _Reject("velas %s duplicadas" % bar)
        age = now_epoch() - rows[-1]["ts"] / 1000
        if age > BAR_SECONDS[bar] + CANDLE_CLOSE_GRACE_SECONDS:
            raise _Reject("velas %s obsoletas: última ts=%s edad=%ds > %ds" % (
                bar, rows[-1]["ts"], age, BAR_SECONDS[bar] + CANDLE_CLOSE_GRACE_SECONDS))
        _record_market("candles:" + bar, True)
        return rows
    except _Reject as exc:
        _record_market("candles:" + bar, False, str(exc).split(" ts=")[0].split(":")[0])
        _market_warning(inst_id, "velas rechazadas: %s" % exc)
        return []
    except (TypeError, ValueError, IndexError, KeyError, OverflowError) as exc:
        _record_market("candles:" + bar, False, "vela inválida")
        _market_warning(inst_id, "velas %s inválidas: %s" % (bar, type(exc).__name__))
        return []


_INSTRUMENTS = {}
_INSTRUMENTS_LOCK = threading.Lock()


def _valid_meta(meta, inst_id):
    return (isinstance(meta, dict) and meta.get("instId") == inst_id and meta.get("instType") == "SWAP"
            and meta.get("contractType") == "linear" and meta.get("quoteCurrency") == "USDT"
            and meta.get("settleCurrency") == "USDT" and meta.get("state") == "live"
            and meta.get("apiPermission") in ("full", "reduce_only"))


def instrument_meta(inst_id, force=False, *, deadline=None):
    """Instrument metadata, cached INSTRUMENTS_TTL_SECONDS. force=True refetches
    (used right before a real/demo order). None when not live/compatible."""
    return _instrument_meta_from(BLOFIN_MARKET, _INSTRUMENTS, inst_id, force, deadline)


_DEMO_INSTRUMENTS = {}


def demo_instrument_meta(inst_id, force=False, *, deadline=None):
    """Order specs of the Blofin DEMO exchange (1.7.4). Demo orders must use
    the demo lotSize/minSize/tickSize/maxMarketSize: on 17/11/2026 the public
    lists differed for ADA, DOGE, XRP, PEPE (lot 0.01 live vs 0.1 demo), LINK,
    ZEC (0.1 vs 1), DOT, ENA, PEPE (demo tick 10x) and maxMarketSize of SOL,
    TAO, WLD, XRP, INJ, ETH. Live specs made those demo orders invalid."""
    return _instrument_meta_from(DEMO_MARKET, _DEMO_INSTRUMENTS, inst_id, force, deadline)


def _instrument_meta_from(base, cache, inst_id, force, deadline):
    now = time.monotonic()
    if not force:
        with _INSTRUMENTS_LOCK:
            hit = cache.get(inst_id)
        if hit and now - hit[0] < INSTRUMENTS_TTL_SECONDS:
            return hit[1]
    body = fetch_json(base + "instruments?instId=" + inst_id, deadline=deadline)
    try:
        items = _api_code(body, "instrumento")
        if base == DEMO_MARKET and isinstance(items, list):
            # Keep only the requested instrument (the list may come unfiltered).
            items = [i for i in items if isinstance(i, dict) and i.get("instId") == inst_id] or items[:0]
        if not items:
            raise _Reject("instrumento %s no existe en %s" % (inst_id, "Blofin demo" if base == DEMO_MARKET else "Blofin"))
        if len(items) != 1 or not _valid_meta(items[0], inst_id):
            raise _Reject("instrumento no live/compatible (state=%s apiPermission=%s)" % (
                (items[0] or {}).get("state"), (items[0] or {}).get("apiPermission")))
    except _Reject as exc:
        with _INSTRUMENTS_LOCK:
            cache.pop(inst_id, None)
        _set_fail(str(exc))
        return None
    except (TypeError, IndexError, KeyError, AttributeError):
        with _INSTRUMENTS_LOCK:
            cache.pop(inst_id, None)
        _set_fail("instrumento malformado")
        return None
    with _INSTRUMENTS_LOCK:
        cache[inst_id] = (time.monotonic(), items[0])
    return items[0]


def fetch_quote(inst_id):
    """Execution quote from the Blofin order book (books?size=1).

    Freshness is the book generation ts (<= QUOTE_MAX_AGE_SECONDS); the price
    is the bid/ask mid. Bid and ask must be finite, positive, bid <= ask and
    the spread below 2%. Instrument metadata comes from a 10-minute cache.
    The ticker 'last' is not needed: its ts is the last trade, which goes
    stale on quiet pairs while the book is alive (Jarvis report C1).
    """
    if not isinstance(inst_id, str) or not _PAIR_RE.fullmatch(inst_id):
        _market_warning(str(inst_id), "símbolo inválido")
        return None
    deadline = time.monotonic() + HTTP_BUDGET_SECONDS
    meta = instrument_meta(inst_id, deadline=deadline)
    if meta is None:
        reason = last_fail_reason() or "instrumento no disponible"
        _record_market("quote", False, "instrumento")
        note_pair(inst_id, False, reason)
        _market_warning(inst_id, "instrumento rechazado: %s" % reason)
        return None
    body = fetch_json(BLOFIN_MARKET + "books?instId=%s&size=1" % inst_id, deadline=deadline)
    try:
        items = _api_code(body, "libro")
        book = items[0]
        if not isinstance(book, dict):
            raise _Reject("libro malformado")
        bids, asks = book.get("bids"), book.get("asks")
        if not bids or not asks:
            raise _Reject("libro sin bid/ask")
        raw = (bids[0][0], asks[0][0])
        if any(isinstance(v, bool) for v in raw):
            raise _Reject("libro bid/ask no numérico")
        bid, ask = (float(v) for v in raw)
        if not all(math.isfinite(v) and v > 0 for v in (bid, ask)):
            raise _Reject("libro bid/ask no positivos")
        if bid > ask:
            raise _Reject("libro cruzado bid=%s > ask=%s" % (bid, ask))
        if ask / bid > MAX_SPREAD_RATIO:
            raise _Reject("spread excesivo %.2f%%" % ((ask / bid - 1) * 100))
        ts = book.get("ts")
        if not _fresh_timestamp(ts, QUOTE_MAX_AGE_SECONDS):
            try:
                age = "%.1fs" % (now_epoch() - float(ts) / 1000)
            except (TypeError, ValueError):
                age = repr(ts)
            raise _Reject("libro obsoleto ts_age=%s > %ss" % (age, QUOTE_MAX_AGE_SECONDS))
        mid = (bid + ask) / 2.0
        quote = MarketQuote(inst_id, mid, int(ts), time.monotonic(), meta["apiPermission"] == "full",
                            bid=bid, ask=ask)
        _record_market("quote", True)
        note_pair(inst_id, True)
        return quote
    except _Reject as exc:
        _record_market("quote", False, re.split(r" (?:ts_age|bid=|\d)", str(exc))[0])
        note_pair(inst_id, False, str(exc))
        _market_warning(inst_id, "precio rechazado: %s" % exc)
        return None
    except (TypeError, ValueError, IndexError, KeyError, OverflowError) as exc:
        _record_market("quote", False, "libro inválido")
        note_pair(inst_id, False, "libro inválido %s" % type(exc).__name__)
        _market_warning(inst_id, "precio rechazado: libro inválido (%s)" % type(exc).__name__)
        return None


def fetch_price(inst_id):
    """Scalar compatibility helper; live execution keeps the timestamped quote."""
    quote = fetch_quote(inst_id)
    return quote.price if valid_quote(quote, inst_id) else None


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


# Last closed confirmation bar per pair. Not persisted. Used for the momentum
# and time exits of an open course trade. The scan row only keeps a fresh signal.
_GRANMAGO_BARS = {}


def _attach_granmago(row, c4, c_d, c_w):
    """Latest course signal, if the just-closed bar is an entry. None otherwise."""
    if strategy_mode() != "granmago" or not row:
        return row
    now_ms = int(now_epoch() * 1000)
    closed_4h = granmago_strategy.drop_forming(c4, granmago_strategy.H4_MS, now_ms)
    closed_d = granmago_strategy.drop_forming(c_d, granmago_strategy.DAY_MS, now_ms)
    closed_w = granmago_strategy.drop_forming(c_w, granmago_strategy.WEEK_MS, now_ms)
    signals, bars = granmago_strategy.find_signals(
        closed_4h,
        closed_d,
        closed_w,
        variant=granmago_variant(),
        min_rr=granmago_min_rr(),
        stop_pad_pct=granmago_stop_pad_pct(),
        style=granmago_style(),
        setups=granmago_setups(),
    )
    row["granmago"] = None
    if row.get("pair"):
        with LOCK:
            _GRANMAGO_BARS[row["pair"]] = bars
    if signals and bars:
        latest = signals[-1]
        # Only the bar that just closed. An older signal is not chased.
        if latest["closeMs"] == max(bars):
            row["granmago"] = latest
    return row


def _close_granmago_signals(successful_pairs):
    """Closed bars decide momentum/time exits; only a fresh ticker can fill."""
    with LOCK:
        pending = []
        if strategy_mode() != "granmago" or not STATE["running"]:
            return
        for pos in _positions():
            if pos.get("strategy") != "granmago" or pos.get("pair") not in successful_pairs:
                continue
            bars = _GRANMAGO_BARS.get(pos["pair"]) or {}
            if bars:
                decision = granmago_strategy.exit_decision(pos, bars[max(bars)])
                if decision is not None:
                    pending.append((pos, decision[0]))
    for expected, reason in pending:
        pair = expected["pair"]
        quote = fetch_quote(pair)
        with LOCK:
            if (STATE["running"] and _find_position(pair) is expected
                    and valid_quote(quote, pair)):
                close_position(reason, quote.price, pair, quote=quote)


def analyze_pair(pair, threshold=None):
    if threshold is None:
        threshold = entry_threshold()
    if strategy_mode() == "granmago":
        c4 = fetch_candles(pair, "4H", 300)
        c_d = fetch_candles(pair, "1D", 220)
        c_w = fetch_candles(pair, "1W", 80)
    else:
        c4 = fetch_candles(pair, "4H", 150)
        c_d = fetch_candles(pair, "1D", 80)
        c_w = fetch_candles(pair, "1W", 60)
    row = score_market(pair, c4, c_d, c_w, threshold)
    row = _attach_granmago(row, c4, c_d, c_w)
    if row:
        row["analyzedAtMs"] = int(now_epoch() * 1000)
    return row


def scan_all():
    if not SCAN_LOCK.acquire(blocking=False):
        return None
    try:
        refresh_demo_universe()
        scan_started = time.monotonic()
        with LOCK:
            pairs = active_pairs()
            add_log("Escaneo de %d pares..." % len(pairs))
            threshold = entry_threshold()
            save_state()
        results = []
        failed = []
        for pair in pairs:
            if STOP.is_set():
                break
            _beat("granmago-scan")
            try:
                row = analyze_pair(pair, threshold)
            except Exception as exc:
                log.warning("analyze %s: %s", pair, exc)
                row = None
            if row:
                results.append(row)
            else:
                failed.append(pair)
            time.sleep(0.3)
        # Pairs that failed (typically right at the 4H boundary) get one more
        # try at the end of the scan instead of being skipped for this cycle.
        if failed and not STOP.is_set():
            log.info("Reintento al final del escaneo: %s", ", ".join(failed))
            if STOP.wait(SCAN_RETRY_DELAY_SECONDS) if _clock is None else False:
                failed = []
            recovered = []
            for pair in failed:
                if STOP.is_set():
                    break
                _beat("granmago-scan")
                try:
                    row = analyze_pair(pair, threshold)
                except Exception as exc:
                    log.warning("analyze %s (reintento): %s", pair, exc)
                    row = None
                if row:
                    results.append(row)
                    recovered.append(pair)
            with LOCK:
                STATE["lastScanRetry"] = {"failed": failed, "recovered": recovered, "at": _now()}
        results.sort(key=lambda r: max(r["scoreLong"], r["scoreShort"]), reverse=True)
        if strategy_mode() == "granmago":
            best_l = [r for r in results if isinstance(r.get("granmago"), dict) and r["granmago"].get("side") == "long"]
            best_s = [r for r in results if isinstance(r.get("granmago"), dict) and r["granmago"].get("side") == "short"]
        else:
            best_l = sorted((r for r in results if r["validLong"]), key=lambda r: r["scoreLong"], reverse=True)
            best_s = sorted((r for r in results if r["validShort"]), key=lambda r: r["scoreShort"], reverse=True)
        _close_granmago_signals({r["pair"] for r in results})
        with LOCK:
            STATE["lastScan"] = results
            STATE["bestLong"] = best_l[0] if best_l else None
            STATE["bestShort"] = best_s[0] if best_s else None
            STATE["lastScanAt"] = _now()
            STATE["lastScanSeconds"] = round(time.monotonic() - scan_started, 1)
            if STATE["lastScanSeconds"] > SCAN_MAX_AGE_SECONDS * 0.8:
                # Rows analysed at the start would be too old for maybe_open.
                add_log("AVISO: el escaneo tardó %.0f s (límite de vigencia %d s): señales del principio "
                        "del escaneo pueden descartarse." % (STATE["lastScanSeconds"], SCAN_MAX_AGE_SECONDS))
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


def open_from_result(res, side, *, quote=None):
    """Append one paper trade. False when the pair is taken or the size is refused.

    risk_pct is applied to the current realized balance, same as a single
    trade. If this notional would push the book past balance × leverage,
    the new trade is shrunk to the room that is left, or skipped at zero.
    Entry price is worsened by slippage. The taker fee is taken from the
    balance immediately. Both are zero unless the add-on options set them.
    """
    if quote is not None:
        if not valid_quote(quote, res.get("pair")) or not quote.can_open:
            return False
        res = dict(res, price=quote.price)
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
        "strategy": "scanner",
    })
    add_log(
        "ENTRADA AUTO %s %s @ %s (score %s) [%d/%d]"
        % (side.upper(), pair, _fmt(entry), score, len(_positions()), max_positions())
    )
    if quote is not None:
        _find_position(pair)["entryMarket"] = _quote_audit(quote)
        _find_position(pair)["lastMarket"] = _quote_audit(quote)
    return True


def _used_signals():
    book = STATE.get("usedSignals")
    if not isinstance(book, dict):
        book = {}
        STATE["usedSignals"] = book
    return book


def _signal_used(pair, side, confirm_ms, close_ms):
    key = _signal_key(pair, side, confirm_ms, close_ms)
    return key is not None and key in _used_signals()


def _remember_signal(pair, side, confirm_ms, close_ms):
    key = _signal_key(pair, side, confirm_ms, close_ms)
    if key is None:
        return
    book = _used_signals()
    book[key] = close_ms
    if len(book) > 400:
        STATE["usedSignals"] = _cap_used_signals(book)


def _outside_bracket(side, price, stop, target):
    """True when a fill would already be through the stop or the target.

    Equality counts: a long printed at the stop is stopped, and a print at
    the target is already closed.
    """
    px = _as_price(price)
    sl = _as_price(stop)
    tp = _as_price(target)
    if px is None or sl is None or tp is None:
        return True
    if side == "long":
        return px <= sl or px >= tp
    if side == "short":
        return px >= sl or px <= tp
    return True


def open_granmago(sig, *, quote=None):
    """Open one course trade from a signal dict. False when it cannot be sized.

    The stop and the target come from the signal, not from the scanner's
    swing or R multiple. With adds enabled, this fill risks 1/(1+adds) of
    risk_pct so the reserved adds can still lose the rest at the same stop.
    Trailing is not used on these positions.

    A live scan passes the current print as ``market``. The fill is that
    price, worsened by slippage, and the trade is refused when the print is
    already through the stop or the target. Research callers leave ``market``
    unset and fill at the signal price. The same pair, direction and candle
    close can open at most one trade.
    """
    plan = _granmago_plan(sig, quote=quote)
    if not plan:
        return False
    entry_fee = _fee_amount(plan["notional"])
    if entry_fee:
        STATE["balance"] = round(float(STATE["balance"]) - entry_fee, 2)
    pos = _granmago_position(plan, entry_fee=entry_fee)
    _positions().append(pos)
    if quote is not None:
        pos["entryMarket"] = _quote_audit(quote)
        pos["lastMarket"] = _quote_audit(quote)
    _remember_signal(plan["pair"], plan["side"], plan["sig"].get("confirmMs"), plan["sig"].get("closeMs"))
    _log_entry(pos, plan["sig"])
    return True


def _granmago_plan(sig, *, quote=None):
    """Every check and the size of a course trade, without touching the book.

    Returns a plan dict or False. Paper applies it with a simulated fill;
    demo sends it to Blofin and applies the real fill.
    """
    if not isinstance(sig, dict):
        return False
    if quote is not None:
        if not valid_quote(quote, sig.get("pair")) or not quote.can_open:
            return False
        sig = dict(sig, market=quote.price)
    pair = sig.get("pair")
    side = sig.get("side")
    if side not in ("long", "short") or not pair or _find_position(pair):
        if pair and _find_position(pair):
            add_log("Ya hay posición en %s, no se duplica." % pair)
        return False
    if cooldown_active(pair):
        return False
    if not regime_allows(sig, side):
        return False
    signal = _as_price(sig.get("price"))
    stop = _as_price(sig.get("sl"))
    target = _as_price(sig.get("tp"))
    reference = _as_price(sig.get("market"))
    if reference is None:
        reference = signal
    if signal is None or stop is None or target is None or reference is None:
        return False
    if _signal_used(pair, side, sig.get("confirmMs"), sig.get("closeMs")):
        add_log("Señal ya operada en %s %s, no se repite." % (side.upper(), pair))
        return False
    if _outside_bracket(side, reference, stop, target):
        add_log(
            "No se abre %s %s: precio %s fuera de stop %s / objetivo %s."
            % (side.upper(), pair, _fmt(reference), _fmt(stop), _fmt(target))
        )
        return False
    entry = _slip_price(side, reference, closing=False)
    if entry is None or _outside_bracket(side, entry, stop, target):
        return False
    dist = abs(entry - stop)
    if dist > entry * 0.08:
        add_log("SL demasiado amplio en %s" % pair)
        return False
    adds = granmago_max_adds()
    legs = 1 + adds
    risk_pct = float(STATE["settings"]["riskPct"]) / 100.0
    full_risk = STATE["balance"] * risk_pct
    leg_risk = full_risk / legs
    if leg_risk <= 0:
        return False
    notional = round(leg_risk / (dist / entry), 2)
    room = _notional_room()
    capped = False
    if room <= 0 or notional > room:
        capped = True
        notional = math.floor(max(0.0, room) * 100 + 1e-9) / 100.0
        if notional <= 0:
            add_log("Sin hueco de nocional para %s (tope balance × apalancamiento)." % pair)
            return False
        leg_risk = notional * (dist / entry)
        add_log(
            "Tamaño de %s limitado a %.2f USDT de nocional (tope balance × apalancamiento)."
            % (pair, notional)
        )
    if not directional_risk_ok(side, leg_risk):
        add_log(
            "Riesgo %s en la misma dirección ya está en el tope (%.2f R)."
            % (side, max_same_direction_risk_r())
        )
        return False
    return {"pair": pair, "side": side, "entry": entry, "reference": reference, "signal": signal,
            "stop": stop, "target": target, "notional": notional, "capped": capped,
            "legRisk": round(leg_risk, 2), "dist": dist, "adds": adds,
            "addBudget": round(full_risk / legs, 2) if adds else 0.0, "sig": sig}


def _granmago_position(plan, *, entry_fee):
    sig = plan["sig"]
    pair, side, entry, notional = plan["pair"], plan["side"], plan["entry"], plan["notional"]
    stop, target, leg_risk, adds = plan["stop"], plan["target"], plan["legRisk"], plan["adds"]
    return {
        "pair": pair,
        "side": side,
        "entry": entry,
        "signalPrice": plan["signal"],
        "sl": stop,
        "tp": target,
        "notional": notional,
        "initialNotional": notional,
        "notionalCapped": plan["capped"],
        "leverage": _leverage(),
        "riskAmt": leg_risk,
        "initialRiskAmt": leg_risk,
        "openRiskAmt": leg_risk,
        "initialRiskDist": plan["dist"],
        "entryFee": entry_fee,
        "time": _now(),
        "unrealized": 0,
        "price": entry,
        "initialSl": stop,
        "initialTp": target,
        "bestPrice": entry,
        "trailingActive": False,
        "partialTaken": False,
        "tradeId": _next_trade_id(),
        "stopMode": "granmago",
        "strategy": "granmago",
        "setup": sig.get("setup"),
        "variant": sig.get("variant") or granmago_variant(),
        "addPrice": _as_price(sig.get("addPrice")),
        "sweepExtreme": _as_price(sig.get("sweepExtreme")),
        "addsDone": 0,
        "maxAdds": adds,
        "addBudget": plan["addBudget"],
        "addBlocked": False,
        "confirmMs": sig.get("confirmMs"),
        "closeMs": sig.get("closeMs"),
        "momentumExit": granmago_momentum_exit(),
        "maxHoldBars": granmago_max_hold_bars(),
        "breakevenR": granmago_breakeven_r(),
        "granmagoBe": False,
    }


def _log_entry(pos, sig):
    add_log(
        "ENTRADA GRANMAGO %s %s %s @ %s SL %s TP %s [%d/%d]%s"
        % (
            str(sig.get("setup") or ""),
            pos["side"].upper(),
            pos["pair"],
            _fmt(pos["entry"]),
            _fmt(pos["sl"]),
            _fmt(pos["tp"]),
            len(_positions()),
            max_positions(),
            " [Blofin DEMO orden %s]" % pos.get("entryOrderId") if pos.get("exchange") else "",
        )
    )


def _maybe_add(pos, mark, *, quote=None):
    """One extra fill at the swept extreme. The stop does not move."""
    if pos.get("addBlocked"):
        return False
    try:
        done = int(pos.get("addsDone") or 0)
        allowed = int(pos.get("maxAdds") or 0)
    except (TypeError, ValueError):
        return False
    if done >= allowed:
        return False
    side = pos.get("side")
    if not granmago_strategy.add_distance_ok(side, mark, pos.get("addPrice"), pos.get("sl")):
        return False
    try:
        budget = float(pos.get("addBudget") or 0.0)
    except (TypeError, ValueError):
        return False
    if budget <= 0:
        return False
    if quote is not None and (not _position_quote_valid(quote, pos) or not quote.can_open):
        return False
    fill = _slip_price(side, quote.price if quote is not None else pos.get("addPrice"), closing=False)
    stop = _as_price(pos.get("sl"))
    if fill is None or stop is None:
        return False
    if side == "long" and fill <= stop:
        return False
    if side == "short" and fill >= stop:
        return False
    dist = abs(fill - stop)
    if dist <= 0:
        return False
    notional = round(budget / (dist / fill), 2)
    room = _notional_room()
    if room <= 0:
        return False
    if notional > room:
        notional = math.floor(max(0.0, room) * 100 + 1e-9) / 100.0
    if notional <= 0:
        return False
    leg_risk = notional * (dist / fill)
    old_notional = float(pos.get("notional") or 0.0)
    old_entry = float(pos.get("entry") or 0.0)
    if old_notional <= 0 or old_entry <= 0:
        return False
    new_notional = old_notional + notional
    pos["entry"] = (old_notional * old_entry + notional * fill) / new_notional
    pos["notional"] = round(new_notional, 2)
    pos["initialNotional"] = pos["notional"]
    fee = _fee_amount(notional)
    if fee:
        STATE["balance"] = round(float(STATE["balance"]) - fee, 2)
        try:
            pos["entryFee"] = float(pos.get("entryFee") or 0.0) + fee
        except (TypeError, ValueError):
            pos["entryFee"] = fee
    pos["addsDone"] = done + 1
    try:
        pos["initialRiskAmt"] = round(float(pos.get("initialRiskAmt") or 0.0) + leg_risk, 2)
    except (TypeError, ValueError):
        pos["initialRiskAmt"] = round(leg_risk, 2)
    try:
        pos["openRiskAmt"] = round(float(pos.get("openRiskAmt") or 0.0) + leg_risk, 2)
    except (TypeError, ValueError):
        pos["openRiskAmt"] = round(leg_risk, 2)
    pos["riskAmt"] = pos["openRiskAmt"]
    pos["initialRiskDist"] = abs(pos["entry"] - stop)
    add_log(
        "PROMEDIO %s %s @ %s (entrada media %s, %d/%d)"
        % (side.upper(), pos.get("pair"), _fmt(fill), _fmt(pos["entry"]), pos["addsDone"], allowed)
    )
    return True


def _apply_granmago_mark(pos, mark, *, quote=None):
    """Hard stop, optional add, optional breakeven, then the swing target.

    No trailing and no scanner partial. A stop is checked before an add, so
    a print through both the extreme and the stop does not average in.
    """
    pair = pos["pair"]
    side = pos["side"]
    pnl = position_pnl(pos, mark)
    pos["price"] = mark
    if pnl is not None:
        pos["unrealized"] = round(pnl, 2)
    update_best_price(pos, mark)
    if _stop_hit(side, mark, pos.get("sl")):
        close_position("SL", mark, pair, quote=quote)
        return "SL"
    _maybe_add(pos, mark, quote=quote)
    if _find_position(pair) is None:
        return "SL"
    pos = _find_position(pair)
    try:
        be_r = float(pos.get("breakevenR") or 0.0)
    except (TypeError, ValueError):
        be_r = 0.0
    if be_r > 0 and not pos.get("granmagoBe"):
        reached = price_r(pos, mark)
        if reached is not None and reached >= be_r:
            if _move_stop_to_breakeven(pos):
                pos["granmagoBe"] = True
                if pos.get("exchange"):
                    pos["slDirty"] = True  # the trading worker moves the SL on Blofin
                    _TRADE_WAKE.set()
                add_log("Breakeven GranMago %s SL → %s" % (pair, _fmt(pos["sl"])))
    if _target_hit(side, mark, pos.get("tp")):
        close_position("TP", mark, pair, quote=quote)
        return "TP"
    return None


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


def close_position(reason, price, pair=None, fraction=1.0, *, quote=None):
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
    exchange_pos = _find_position(pair)
    if exchange_pos is not None and exchange_pos.get("exchange"):
        # Demo: the exchange is the truth. Queue a market close for the trading
        # worker (no network under LOCK); the real fill is booked afterwards.
        return _request_exchange_close(exchange_pos, reason, fraction)
    idx = None
    for i, item in enumerate(positions):
        if isinstance(item, dict) and item.get("pair") == pair:
            idx = i
            break
    if idx is None:
        return None
    pos = positions[idx]
    if quote is not None:
        if not _position_quote_valid(quote, pos):
            add_log("No se cierra %s: ticker Blofin caducado/inválido." % pair)
            return None
        price = quote.price
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
    closed = {
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
    }
    if pos.get("closeMs"):
        closed["closeMs"] = pos.get("closeMs")
        closed["confirmMs"] = pos.get("confirmMs")
    if quote is not None:
        closed["exitMarket"] = _quote_audit(quote)
    STATE["history"].insert(0, closed)
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


def _apply_mark(pos, price, *, quote=None):
    """SL, partial, TP or trailing for one position. The caller holds LOCK.

    Returns the exit reason, or 'partial' when a slice was closed and the
    rest is still open, or None. A stop is always checked first. A print that
    reaches the full target fills a pending partial before the remainder.
    Live fills always use quote.price; offline replays use the partial level.
    """
    if quote is not None:
        if not _position_quote_valid(quote, pos):
            return None
        price = quote.price
        pos["lastMarket"] = _quote_audit(quote)
        pos["priceStale"] = False
    ensure_position_fields(pos)
    pair = pos["pair"]
    side = pos["side"]
    mark = _as_price(price)
    if mark is None:
        return None
    if pos.get("strategy") == "granmago":
        return _apply_granmago_mark(pos, mark, quote=quote)
    pnl = position_pnl(pos, mark)
    pos["price"] = mark
    if pnl is not None:
        pos["unrealized"] = round(pnl, 2)
    update_best_price(pos, mark)
    if _stop_hit(side, mark, pos.get("sl")):
        reason = _stop_reason(pos)
        close_position(reason, mark, pair, quote=quote)
        return reason
    if _partial_hit(pos, mark):
        level = partial_target_price(pos)
        close_position("partial", level if level is not None else mark, pair, fraction=partial_tp_fraction(), quote=quote)
        if _find_position(pair) is None:
            return "partial"
        pos = _find_position(pair)
    if _target_hit(side, mark, pos.get("tp")):
        close_position("TP", mark, pair, quote=quote)
        return "TP"
    if pos.get("trailingActive"):
        moved = tighten_stop(pos)
        if moved:
            add_log("Trailing %s SL → %s (mejor %s)" % (pair, _fmt(pos["sl"]), _fmt(pos["bestPrice"])))
        if _stop_hit(side, mark, pos.get("sl")):
            close_position("trailing", mark, pair, quote=quote)
            return "trailing"
        return None
    if trailing_should_activate(pos, mark):
        activate_trailing(pos)
        add_log(
            "Trailing activo en %s. SL %s (mejor %s)"
            % (pair, _fmt(pos["sl"]), _fmt(pos["bestPrice"]))
        )
        if _stop_hit(side, mark, pos.get("sl")):
            close_position("trailing", mark, pair, quote=quote)
            return "trailing"
    return None


def _monitor_one(pair, display_only=False):
    with LOCK:
        expected = _find_position(pair)
    if expected is None:
        return
    quote = fetch_quote(pair)
    with LOCK:
        pos = _find_position(pair)
        if pos is not expected:
            return
        if display_only or not STATE["running"]:
            # Bot stopped: keep price/PnL current for the panel, never act.
            if _position_quote_valid(quote, pos):
                pos["lastMarket"] = _quote_audit(quote)
                pos["priceStale"] = False
                pos["price"] = quote.price
                pnl = position_pnl(pos, quote.price)
                if pnl is not None:
                    pos["unrealized"] = round(pnl, 2)
            else:
                pos["priceStale"] = True
            return
        if not _position_quote_valid(quote, pos):
            pos["priceStale"] = True
            add_log("Sin ticker Blofin fresco de %s: no se actualiza PnL ni se ejecuta SL/TP." % pair)
            return
        _apply_mark(pos, quote.price, quote=quote)


_MONITOR_POOL = None


def _monitor_pool():
    global _MONITOR_POOL
    if _MONITOR_POOL is None:
        _MONITOR_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="granmago-mon")
    return _MONITOR_POOL


def monitor_position(display_only=False):
    with LOCK:
        pairs = [p.get("pair") for p in _positions() if isinstance(p, dict) and p.get("pair")]
    if not pairs:
        return
    # One illiquid/rate-limited pair must not hold up the other positions.
    # A persistent pool and the shared session keep TLS connections alive
    # (no new handshake every cycle); all book mutation still uses LOCK.
    list(_monitor_pool().map(lambda pair: _monitor_one(pair, display_only), pairs))
    with LOCK:
        STATE["lastTickAt"] = _now()
        save_state()


def maybe_open():
    """Live entry gateway: never use lastScan/candle prices as execution quotes.

    Network outside LOCK; recheck the exact scan, mode, book and running state
    afterwards. Replays call _maybe_open_replay explicitly and never do I/O.
    """
    opened = False
    with LOCK:
        if not STATE["running"] or len(_positions()) >= max_positions():
            return
        if STATE.get("killSwitch"):
            return
        if TRADING_ERROR:
            return
        if TRADER is not None and STATE.get("initialBalance") is None:
            return  # demo balance not synced from Blofin yet
        scan = STATE.get("lastScan")
        rows = json.loads(json.dumps(scan or []))
        mode = strategy_mode()
        if mode != "granmago":
            rows.sort(key=lambda r: max(_score_num(r.get("scoreLong")), _score_num(r.get("scoreShort"))), reverse=True)
    for row in rows:
        pair = row.get("pair")
        with LOCK:
            if (not STATE["running"] or STATE.get("lastScan") is not scan
                    or strategy_mode() != mode or len(_positions()) >= max_positions()):
                break
            if not pair or _find_position(pair):
                continue
            if mode == "granmago":
                sig = row.get("granmago")
                if not isinstance(sig, dict) or _signal_used(pair, sig.get("side"), sig.get("confirmMs"), sig.get("closeMs")):
                    continue
                side = sig.get("side")
            else:
                side, _ = _qualifying_side(row, entry_threshold())
                if side is None:
                    continue
        was_degraded = pair_degraded(pair)
        quote = fetch_quote(pair)
        if was_degraded:
            # A degraded pair needs one healthy cycle before it can open again.
            if quote is not None:
                with LOCK:
                    add_log("%s recuperado del estado degradado: sin entrada en este ciclo." % pair)
            continue
        with LOCK:
            if (not STATE["running"] or STATE.get("lastScan") is not scan
                    or strategy_mode() != mode or len(_positions()) >= max_positions()
                    or STATE.get("killSwitch")):
                break
            reference = _as_price(row.get("price"))
            if (not valid_quote(quote, pair) or not quote.can_open
                    or not _fresh_timestamp(row.get("analyzedAtMs"), SCAN_MAX_AGE_SECONDS)
                    or reference is None or abs(quote.price / reference - 1) > ENTRY_MAX_DIVERGENCE):
                add_log("No se abre %s: falta ticker Blofin fresco/coherente o escaneo vigente." % pair)
                continue
            if mode == "granmago" and TRADER is not None:
                plan = _granmago_plan(dict(sig, pair=pair, market=quote.price), quote=quote)
                if not plan:
                    continue
                # Persist "signal used" before any order: a crash after the POST
                # can never repeat this signal (audit C4). Blofin dedupes the
                # deterministic clientOrderId as a second guard.
                _remember_signal(pair, plan["side"], sig.get("confirmMs"), sig.get("closeMs"))
                STATE.setdefault("opening", {})[pair] = _now()
                save_state()
            elif mode == "granmago":
                payload = dict(sig, pair=pair, market=quote.price)
                opened = open_granmago(payload, quote=quote) or opened
                save_state()
                continue
            else:
                side, _ = _qualifying_side(row, entry_threshold())
                if side is not None and TRADER is None:
                    opened = open_from_result(dict(row, price=quote.price), side, quote=quote) or opened
                save_state()
                continue
        # Demo order outside LOCK.
        opened = _open_on_exchange(plan, quote) or opened
    if opened:
        kick()


def _maybe_open_replay():
    """OFFLINE ONLY: fill slots from historical/synthetic scan prices, no HTTP.

    Not called by the Flask API or live loops. Production uses maybe_open.
    Scanner mode ranks by score. Gran Mago mode opens the course signal on
    the bar that just closed and ignores the score. One position per coin,
    long or short, never both. The same signal (pair, direction, candle
    close) is not opened again after it has already traded. The fill uses
    the price on the scan row, which is the latest print, not the signal's
    close. A manual scan does not call this. Nothing is opened while the
    bot is stopped.
    """
    opened = False
    with LOCK:
        if not STATE["running"]:
            return
        limit = max_positions()
        if len(_positions()) >= limit:
            return
        rows = STATE.get("lastScan") or []
        occupied = {p.get("pair") for p in _positions() if isinstance(p, dict)}
        if strategy_mode() == "granmago":
            fresh = False
            for row in rows:
                if len(_positions()) >= limit:
                    break
                if not isinstance(row, dict):
                    continue
                sig = row.get("granmago")
                pair = row.get("pair")
                if not isinstance(sig, dict) or not pair or pair in occupied:
                    continue
                fresh = True
                if _signal_used(pair, sig.get("side"), sig.get("confirmMs"), sig.get("closeMs")):
                    continue
                market = _as_price(row.get("price"))
                if market is None:
                    add_log("Sin precio actual de %s, no se abre la señal." % pair)
                    continue
                payload = dict(sig)
                payload["pair"] = pair
                payload["market"] = market
                if open_granmago(payload):
                    opened = True
                    occupied.add(pair)
            if not fresh:
                add_log("Ningún setup Gran Mago en el cierre de vela. Esperando...")
            save_state()
        else:
            threshold = entry_threshold()
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


# ------------------------------------------------------------ supervision
# Each background loop beats once per iteration. _supervised restarts a loop
# that raised or returned while the process is still running; /api/health
# reports the age of every beat (a hung loop shows up as stale).
THREADS = {}
THREAD_STALE_SECONDS = {"granmago-monitor": 60, "granmago-scan": 300, "granmago-trading": 300}


def _beat(name):
    with _STATS_LOCK:
        t = THREADS.setdefault(name, {"restarts": 0, "lastError": None})
        t["beat"] = time.monotonic()


def _supervised(name, target, pause=5.0):
    def run():
        while not STOP.is_set():
            _beat(name)
            try:
                target()
            except Exception as exc:  # never let a loop die silently
                log.exception("hilo %s", name)
                with _STATS_LOCK:
                    THREADS[name]["lastError"] = "%s: %s" % (type(exc).__name__, str(exc)[:120])
            if STOP.is_set():
                return
            with _STATS_LOCK:
                THREADS[name]["restarts"] += 1
            _alert("hilo", "%s se reinicia (%s)" % (name, THREADS[name].get("lastError") or "terminó"))
            if STOP.wait(pause):
                return
    return run


def thread_health():
    now = time.monotonic()
    with _STATS_LOCK:
        out = {}
        for name, t in THREADS.items():
            age = None if t.get("beat") is None else round(now - t["beat"], 1)
            limit = THREAD_STALE_SECONDS.get(name, 300)
            out[name] = {"beatAgeSeconds": age, "restarts": t["restarts"], "lastError": t["lastError"],
                         "stale": age is None or age > limit}
    return out


def scan_loop():
    """Full market scan on the scan interval, whether or not a position is open."""
    while not STOP.is_set():
        _beat("granmago-scan")
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
        _beat("granmago-monitor")
        with LOCK:
            running = STATE["running"]
            has_pos = bool(STATE.get("openPositions"))
        if not next_actions(running, has_pos)["monitor"]:
            if has_pos:
                # Stopped with open positions (in demo they live on Blofin):
                # refresh prices for the panel only, no SL/TP from here.
                try:
                    monitor_position(display_only=True)
                except Exception:
                    log.exception("monitor (display)")
                if not _wait(MONITOR_SECONDS):
                    return
                continue
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


# ====================================================================== trading
# Paper by default. Demo (Blofin Demo Trading) only when trading_mode=demo.
# trading_mode=live is rejected by blofin_trading.TradingConfig (blocked).
TRADER = None
TRADING_CONFIG = None
TRADING_ERROR = None
JOURNAL = None
_TRADE_WAKE = threading.Event()
RECONCILE_SECONDS = 60
_LAST_RECONCILE = 0.0
_LAST_FX = 0.0


def trading_mode():
    if TRADING_ERROR:
        return "blocked"
    return TRADING_CONFIG.mode if TRADING_CONFIG else "paper"


# ------------------------------------------------------------ ledgers (1.7.1)
# Paper and demo keep separate books. The active one lives in the top-level
# keys; the other is parked in paperSnapshot / demoLedger. Caller holds LOCK.
BOOK_KEYS = ("balance", "initialBalance", "history", "openPositions", "tradeSeq", "usedSignals", "cooldowns")
LEDGER_MIGRATION = "1.7.1-ledgers"
PAPER_REBUILD_MIGRATION = "1.7.5-paper-rebuild"
# A paper book always satisfies balance = initialBalance + sum(pnl of its trades)
# - entry fees of its open positions (up to rounding). A gap this large means
# the book carries a Blofin demo balance (the 1.7.0 contamination).
PAPER_REBUILD_MIN_GAP_USDT = 5.0
PAPER_REBUILD_MIN_GAP_PCT = 0.01
# ...and only when the paper balance looks like a Blofin demo figure we hold
# (demo initial/balance/equity, within 5 %): the exact 1.7.0 signature.
PAPER_REBUILD_DEMO_MATCH_PCT = 0.05


def _book_copy():
    return json.loads(json.dumps({k: STATE.get(k) for k in BOOK_KEYS}))


def _book_load(book):
    book = json.loads(json.dumps(book or {}))
    STATE["balance"] = float(book["balance"]) if book.get("balance") is not None else 0.0
    STATE["initialBalance"] = book.get("initialBalance")
    STATE["history"] = list(book.get("history") or [])
    STATE["openPositions"] = list(book.get("openPositions") or [])
    STATE["tradeSeq"] = int(book.get("tradeSeq") or 0)
    STATE["usedSignals"] = dict(book.get("usedSignals") or {})
    STATE["cooldowns"] = dict(book.get("cooldowns") or {})


def _fresh_demo_book():
    # initialBalance stays None until the first valid Blofin balance (trading_cycle).
    return {"balance": 0.0, "initialBalance": None, "history": [], "openPositions": [],
            "tradeSeq": 0, "usedSignals": {}, "cooldowns": {}}


def _max_trade_id(rows):
    ids = [0]
    for row in rows:
        try:
            ids.append(int(row.get("tradeId") or 0))
        except (TypeError, ValueError, AttributeError):
            continue
    return max(ids)


def _same_money(a, b):
    try:
        return abs(float(a) - float(b)) < 0.005
    except (TypeError, ValueError):
        return False


def _migrate_ledgers_1_7_1():
    """One-off repair of 1.7.0 demo states.

    1.7.0 mixed paper and demo in one book and load_state dropped
    paperSnapshot, so the snapshot was retaken with the demo balance. Rebuild
    the paper book from the paper trades (rows without "exchange"):
    balance = initialBalance + sum(pnl). Demo keeps only exchange rows.
    """
    done = STATE.setdefault("migrations", [])
    if LEDGER_MIGRATION in done:
        return
    if STATE.get("ledgerMode") == "demo" or STATE.get("balanceSource") == "blofin-demo":
        history = [h for h in (STATE.get("history") or []) if isinstance(h, dict)]
        positions = [p for p in _positions() if isinstance(p, dict)]
        paper_hist = [h for h in history if not h.get("exchange")]
        demo_hist = [h for h in history if h.get("exchange")]
        snap = STATE.get("paperSnapshot")
        if not isinstance(snap, dict) or _same_money(snap.get("balance"), STATE.get("balance")):
            try:
                initial = float(STATE.get("initialBalance") or 10000)
            except (TypeError, ValueError):
                initial = 10000.0
            pnl = sum(float(h.get("pnl") or 0) for h in paper_hist)
            STATE["paperSnapshot"] = {
                "balance": round(initial + pnl, 4), "initialBalance": initial,
                "history": paper_hist, "openPositions": [p for p in positions if not p.get("exchange")],
                "tradeSeq": max(int(STATE.get("tradeSeq") or 0) if not demo_hist else 0, _max_trade_id(paper_hist)),
                "usedSignals": dict(STATE.get("usedSignals") or {}), "cooldowns": {},
                "at": _now(), "rebuiltBy": LEDGER_MIGRATION,
            }
            add_log("Migración 1.7.1: libro paper reconstruido (%.2f USDT, %d operaciones)." % (
                initial + pnl, len(paper_hist)))
        else:
            snap.setdefault("initialBalance", STATE.get("initialBalance"))
            snap.setdefault("tradeSeq", _max_trade_id(snap.get("history") or []))
            snap.setdefault("usedSignals", dict(STATE.get("usedSignals") or {}))
            snap.setdefault("cooldowns", {})
        demo_pos = [p for p in positions if p.get("exchange")]
        _book_load(dict(_fresh_demo_book(), balance=STATE.get("balance") or 0.0, history=demo_hist,
                        openPositions=demo_pos, tradeSeq=_max_trade_id(demo_hist + demo_pos),
                        usedSignals=dict(STATE.get("usedSignals") or {})))
        STATE["ledgerMode"] = "demo"
        STATE["balanceSource"] = "blofin-demo"
        add_log("Migración 1.7.1: libro demo separado (%d operaciones demo)." % len(demo_hist))
    done.append(LEDGER_MIGRATION)


def _paper_book_expected(book):
    """(expected balance, paper rows) for a paper book, or (None, reason)."""
    try:
        initial = float(book.get("initialBalance"))
    except (TypeError, ValueError):
        return None, "sin initialBalance"
    if not math.isfinite(initial) or initial <= 0:
        return None, "initialBalance inválido"
    rows = [h for h in (book.get("history") or []) if isinstance(h, dict)]
    if len(rows) >= 200:
        return None, "historial truncado (200)"  # older trades are gone: cannot rebuild
    paper = [h for h in rows if not h.get("exchange")]
    try:
        pnl = sum(float(h.get("pnl") or 0) for h in paper)
        fees = sum(float(p.get("entryFee") or 0) for p in (book.get("openPositions") or [])
                   if isinstance(p, dict) and not p.get("exchange"))
    except (TypeError, ValueError):
        return None, "pnl ilegible"
    return round(initial + pnl - fees, 4), paper


def _demo_balance_refs():
    refs = []
    if STATE.get("ledgerMode") == "demo" or STATE.get("balanceSource") == "blofin-demo":
        refs += [STATE.get("balance"), STATE.get("initialBalance")]
    demo = STATE.get("demoLedger") if isinstance(STATE.get("demoLedger"), dict) else {}
    refs += [demo.get("balance"), demo.get("initialBalance")]
    acc = STATE.get("exchangeAccount") if isinstance(STATE.get("exchangeAccount"), dict) else {}
    refs += [acc.get(k) for k in ("totalEquity", "balance", "available", "equity")]
    out = []
    for r in refs:
        try:
            r = float(r)
        except (TypeError, ValueError):
            continue
        if math.isfinite(r) and r > 0:
            out.append(r)
    return out


def _backup_paper_book(book, label):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.fromtimestamp(now_epoch()).strftime("%Y%m%d-%H%M%S")
    path = DATA_DIR / ("paper_snapshot_backup_%s_%s.json" % (label, stamp))
    n = 1
    while path.exists():
        n += 1
        path = DATA_DIR / ("paper_snapshot_backup_%s_%s_%d.json" % (label, stamp, n))
    path.write_text(json.dumps(book, indent=2), encoding="utf-8")
    return path


def _migrate_paper_rebuild_1_7_5():
    """Repair paper books still carrying the 1.7.0 demo balance. Caller holds LOCK.

    1.7.1 only rebuilt the paper snapshot when it equalled the CURRENT balance;
    by then trading_cycle had refreshed the balance from Blofin (999.282 vs
    999.416), so the corrupt snapshot was kept and the flag set anyway. This
    checks the paper book itself (parked snapshot in demo, active book in
    paper) against initialBalance + its own paper trades. Never touches the
    demo book, demo positions or Blofin. Runs once; idempotent anyway.
    """
    done = STATE.setdefault("migrations", [])
    if PAPER_REBUILD_MIGRATION in done:
        return None
    report = None
    mode = STATE.get("ledgerMode") or "paper"
    if mode == "demo" or STATE.get("balanceSource") == "blofin-demo":
        book, where = STATE.get("paperSnapshot"), "paperSnapshot"
    else:
        book, where = _book_copy(), "activo"
    if isinstance(book, dict):
        expected, paper = _paper_book_expected(book)
        try:
            current = float(book.get("balance"))
        except (TypeError, ValueError):
            current = None
        gap = None if expected is None or current is None else abs(current - expected)
        looks_demo = current is not None and any(
            abs(current - r) <= PAPER_REBUILD_DEMO_MATCH_PCT * r for r in _demo_balance_refs())
        if expected is None:
            log.warning("Migración 1.7.5: libro paper (%s) sin comprobar: %s.", where, paper)
            add_log("Migración 1.7.5: libro paper (%s) sin comprobar: %s." % (where, paper))
        elif gap is not None and gap > max(PAPER_REBUILD_MIN_GAP_USDT,
                                           PAPER_REBUILD_MIN_GAP_PCT * float(book["initialBalance"])) and not looks_demo:
            log.warning("Migración 1.7.5: libro paper (%s) %.2f no cuadra con sus operaciones (%.2f) pero no "
                        "parece un saldo demo: sin cambios.", where, current, expected)
            add_log("Migración 1.7.5: libro paper (%s) %.2f no cuadra con sus operaciones (%.2f); no parece saldo "
                    "demo, sin cambios." % (where, current, expected))
        elif gap is not None and gap > max(
                PAPER_REBUILD_MIN_GAP_USDT, PAPER_REBUILD_MIN_GAP_PCT * float(book["initialBalance"])):
            backup = _backup_paper_book(book, where)
            report = {"where": where, "old": current, "new": expected, "trades": len(paper),
                      "backup": backup.name}
            if where == "paperSnapshot":
                fixed = dict(book, balance=expected, history=paper, at=_now(), rebuiltBy=PAPER_REBUILD_MIGRATION,
                             previousBalance=current, backup=backup.name)
                fixed.setdefault("tradeSeq", _max_trade_id(paper))
                fixed.setdefault("usedSignals", {})
                fixed.setdefault("cooldowns", {})
                STATE["paperSnapshot"] = fixed
            else:
                STATE["balance"] = expected
                STATE["history"] = paper
            log.warning("Migración 1.7.5: libro paper (%s) reconstruido %s -> %.2f USDT (%d operaciones paper); "
                        "copia en %s", where, current, expected, len(paper), backup)
            add_log("Migración 1.7.5: libro paper reconstruido de %s a %.2f USDT (%d operaciones). Copia: %s" % (
                current, expected, len(paper), backup.name))
        else:
            add_log("Migración 1.7.5: libro paper correcto (%.2f USDT, %d operaciones)." % (current, len(paper)))
    done.append(PAPER_REBUILD_MIGRATION)
    STATE["lastPaperRebuild"] = report
    return report


def _switch_ledger(target):
    """Make the paper or demo book active. The paper snapshot is taken only
    when the active book really is paper, never from a Blofin balance."""
    current = STATE.get("ledgerMode") or "paper"
    if target not in ("paper", "demo") or current == target:
        return
    if target == "demo":
        if STATE.get("balanceSource") != "blofin-demo":
            STATE["paperSnapshot"] = dict(_book_copy(), at=_now())
        _book_load(STATE.get("demoLedger") or _fresh_demo_book())
        STATE["balanceSource"] = "blofin-demo"
        add_log("Libro DEMO activo. El libro paper queda guardado (%.2f USDT)." % float(
            (STATE.get("paperSnapshot") or {}).get("balance") or 0))
    else:
        STATE["demoLedger"] = dict(_book_copy(), at=_now())
        snap = STATE.get("paperSnapshot")
        if isinstance(snap, dict):
            _book_load(snap)
            if STATE.get("initialBalance") is None:
                STATE["initialBalance"] = STATE["balance"]
        else:
            fresh = default_state()
            _book_load({k: fresh[k] for k in BOOK_KEYS})
        STATE["balanceSource"] = "paper"
        add_log("Libro PAPER restaurado (%.2f USDT, %d operaciones)." % (STATE["balance"], len(STATE["history"])))
        if STATE["demoLedger"].get("openPositions"):
            add_log("AVISO: quedan posiciones DEMO abiertas en Blofin que este modo no gestiona: "
                    + ", ".join(p.get("pair", "?") for p in STATE["demoLedger"]["openPositions"]))
    STATE["ledgerMode"] = target


def init_trading(environ=None, transport=None, sleep=time.sleep):
    """Read trading options. Returns the mode. Never raises: a blocked or
    invalid configuration leaves the bot stopped with a clear error."""
    global TRADER, TRADING_CONFIG, TRADING_ERROR, JOURNAL
    TRADER = TRADING_CONFIG = JOURNAL = None
    TRADING_ERROR = None
    env = os.environ if environ is None else environ
    try:
        cfg = blofin_trading.TradingConfig.from_env(env)
    except (blofin_trading.LiveTradingBlocked, blofin_trading.TradingConfigError, ValueError) as exc:
        TRADING_ERROR = str(exc)
        log.error("TRADING BLOQUEADO: %s", exc)
        with LOCK:
            STATE["running"] = False
            add_log("TRADING BLOQUEADO: %s" % exc)
        _alert("trading", str(exc))
        return "blocked"
    TRADING_CONFIG = cfg
    blofin_trading.install_redactor(cfg.secrets())
    with LOCK:
        _migrate_ledgers_1_7_1()
        _migrate_paper_rebuild_1_7_5()
        _switch_ledger(cfg.mode)
        save_state()
    if cfg.mode == "demo":
        import fiscal_journal
        TRADER = blofin_trading.DemoTrader(cfg, DATA_DIR, transport=transport, sleep=sleep,
                                           instrument_meta=lambda inst, force=False: demo_instrument_meta(inst, force))
        JOURNAL = fiscal_journal.FiscalJournal(DATA_DIR / "fiscal_journal_demo.sqlite", mode="demo",
                                               engine_version=VERSION, instance=env.get("GRANMAGO_INSTANCE", "ha"))
        with LOCK:
            add_log("Modo DEMO Blofin (%s). El SL/TP vive en el exchange." % cfg.base_url)
    if _as_bool(env.get("KILL_SWITCH"), False):
        with LOCK:
            STATE["killSwitch"] = True
            add_log("Kill switch activo por opción del complemento: sin entradas nuevas.")
    return cfg.mode


def start_trading():
    """Demo startup, before any scan: account checks, pending intents, reconcile."""
    if TRADER is None:
        return
    refresh_demo_universe(force=True)  # before the first scan: IOST & co. never reach an order
    TRADER.prepare_account()
    TRADER.resolve_pending_intents()
    trading_cycle(force_reconcile=True)


def _exchange_book():
    return [{"pair": p["pair"], "side": p["side"], "contracts": p.get("contracts")}
            for p in _positions() if isinstance(p, dict) and p.get("exchange")]


def _open_on_exchange(plan, quote):
    pair = plan["pair"]
    demo_ids = _DEMO_UNIVERSE.get("ids")
    if demo_ids and pair not in demo_ids:
        with LOCK:
            STATE.get("opening", {}).pop(pair, None)
            add_log("Entrada DEMO %s no enviada: el instrumento no existe en Blofin demo." % pair)
        return False
    try:
        fill = TRADER.open_position({
            "pair": pair, "side": plan["side"], "notional": plan["notional"], "entry": quote.price,
            "sl": plan["stop"], "tp": plan["target"], "riskAmt": plan["legRisk"],
            "confirmMs": plan["sig"].get("confirmMs"), "closeMs": plan["sig"].get("closeMs")})
    except Exception as exc:  # never let an order error kill the scan loop
        log.exception("demo entry")
        TRADER.mark_unsafe("error_entrada: %s" % type(exc).__name__)
        fill = None
    with LOCK:
        STATE.get("opening", {}).pop(pair, None)
        if not fill:
            add_log("Entrada DEMO %s %s no ejecutada (ver alertas de trading)." % (plan["side"].upper(), pair))
            save_state()
            return False
        dist = abs(fill["entry"] - fill["sl"])
        plan = dict(plan, entry=fill["entry"], stop=fill["sl"], target=fill["tp"], notional=fill["notional"],
                    dist=dist, legRisk=round(fill["contracts"] * fill["contractValue"] * dist, 4))
        pos = _granmago_position(plan, entry_fee=fill["fee"])
        audit = _quote_audit(quote)
        pos.update(exchange="blofin-demo", contracts=fill["contracts"], contractValue=fill["contractValue"],
                   entryOrderId=fill["orderId"], clientOrderId=fill["clientOrderId"], tpslId=fill["tpslId"],
                   openedMs=fill["openedMs"], maxAdds=0, addBudget=0.0,
                   entryMarket=dict(audit, source="Blofin-demo-fill", price=fill["entry"],
                                    observedAt=datetime.fromtimestamp(fill["openedMs"] / 1000, timezone.utc)
                                    .astimezone().isoformat(timespec="seconds")),
                   lastMarket=audit)
        _positions().append(pos)
        _log_entry(pos, plan["sig"])
        save_state()
    return True


def _request_exchange_close(pos, reason, fraction=1.0):
    if fraction < 1 - 1e-12:
        add_log("Cierre parcial no soportado en demo (%s %s): se ignora." % (pos["pair"], reason))
        return None
    if not pos.get("closeRequested"):
        pos["closeRequested"] = {"reason": reason, "at": _now()}
        add_log("Cierre DEMO solicitado %s (%s): orden a mercado en Blofin." % (pos["pair"], reason))
    _TRADE_WAKE.set()
    return None


def _book_exchange_close(pos, reason, fills, funding):
    """Record a close with real Blofin fills. Caller holds LOCK."""
    pair = pos["pair"]
    cv = float(pos.get("contractValue") or 0)
    contracts = float(pos.get("contracts") or 0)
    entry = float(pos["entry"])
    direction = 1 if pos["side"] == "long" else -1
    gross = fills["grossPnl"]
    if not gross:
        gross = (fills["exit"] - entry) * direction * fills["contracts"] * cv
    entry_fee = float(pos.get("entryFee") or 0)
    net = gross - fills["fees"] - entry_fee + (funding or 0.0)
    risk = _open_risk_amt(pos)
    closed = {
        "time": _history_time(), "pair": pair, "side": pos["side"], "entry": entry, "exit": fills["exit"],
        "pnl": round(net, 4), "reason": reason, "initialSl": pos.get("initialSl"),
        "riskAmt": round(risk, 4), "initialRiskAmt": pos.get("initialRiskAmt"),
        "rMultiple": None if risk <= 0 else round(net / risk, 4),
        "tradePnl": round(net, 4), "tradeR": None if risk <= 0 else round(net / risk, 4),
        "fees": round(fills["fees"] + entry_fee, 6), "notional": round(contracts * cv * entry, 4),
        "tradeId": pos.get("tradeId"), "exchange": pos.get("exchange"),
        "exitOrderId": ",".join(fills["orderIds"]), "exchangeTradeIds": ",".join(fills["tradeIds"]),
        "fundingUsdt": funding,
        "exitMarket": {"source": "Blofin-demo-fill", "instId": pair, "ts": fills["ts"], "price": fills["exit"],
                       "observedAt": datetime.fromtimestamp(fills["ts"] / 1000, timezone.utc)
                       .astimezone().isoformat(timespec="seconds")},
    }
    if pos.get("closeMs"):
        closed["closeMs"] = pos.get("closeMs")
    STATE["history"].insert(0, closed)
    del STATE["history"][200:]
    if pos in _positions():
        _positions().remove(pos)
    _remember_cooldown(pair)
    add_log("CERRADA DEMO %s %s → PnL %.2f USDT (fills Blofin)" % (pair, reason, net))
    if JOURNAL is not None:
        try:
            JOURNAL.record_close(closed, pos)
        except Exception as exc:
            log.error("diario fiscal demo: %r", exc)
            _alert("diario", "fallo al escribir el diario: %r" % exc)
    return closed


def _guess_exchange_reason(pos, exit_price):
    sl, tp = _as_price(pos.get("sl")), _as_price(pos.get("tp"))
    if sl is None or tp is None:
        return "exchange"
    return "SL (exchange)" if abs(exit_price - sl) <= abs(exit_price - tp) else "TP (exchange)"


def _settle_closed(pos, reason=None):
    """Fetch the real exit of a position that no longer exists on Blofin."""
    fills = TRADER.exit_fills(pos["pair"], pos["side"], int(pos.get("openedMs") or 0))
    if not fills:
        return False
    funding = TRADER.funding(pos["pair"], int(pos.get("openedMs") or 0), fills["ts"])
    with LOCK:
        if pos not in _positions():
            return True
        _book_exchange_close(pos, reason or _guess_exchange_reason(pos, fills["exit"]), fills, funding)
        save_state()
    return True


_CLOSE_TRIES = {}


def trading_cycle(force_reconcile=False):
    """One pass of the demo worker: closes, stop moves, reconcile, balance."""
    global _LAST_RECONCILE, _LAST_FX
    if TRADER is None:
        return
    with LOCK:
        mono = time.monotonic()
        # In memory only (monotonic clock): a restart retries at once.
        closes = [p for p in _positions() if p.get("exchange") and p.get("closeRequested")
                  and mono - _CLOSE_TRIES.get(p.get("clientOrderId") or p["pair"], -1e9) >= CLOSE_RETRY_SECONDS]
        for p in closes:
            _CLOSE_TRIES[p.get("clientOrderId") or p["pair"]] = mono
        moves = [p for p in _positions() if p.get("exchange") and p.get("slDirty") and not p.get("closeRequested")]
    for pos in closes:
        reason = pos["closeRequested"]["reason"]
        try:
            done = TRADER.close_position(pos["pair"], reason, pos.get("clientOrderId") or "")
            if done and not _settle_closed(pos, reason):
                with LOCK:
                    pos["exchangeMissingSince"] = pos.get("exchangeMissingSince") or _now()
        except Exception as exc:
            log.exception("demo close")
            TRADER.alert("Cierre %s falló: %r" % (pos["pair"], exc), "error")
    for pos in moves:
        try:
            meta = demo_instrument_meta(pos["pair"])  # demo tickSize, not live (1.7.4)
            new_id, sl = TRADER.replace_stop(pos["pair"], pos["side"], pos["sl"], pos["tp"], pos.get("tpslId"),
                                             pos.get("clientOrderId") or "", float((meta or {}).get("tickSize") or 0))
            with LOCK:
                pos.update(tpslId=new_id, sl=sl, slDirty=False)
                add_log("SL DEMO %s movido a %s en Blofin." % (pos["pair"], _fmt(sl)))
                save_state()
        except Exception as exc:
            TRADER.alert("No se pudo mover el SL de %s (sigue el anterior): %r" % (pos["pair"], exc), "error")
    now = time.monotonic()
    if force_reconcile or now - _LAST_RECONCILE >= RECONCILE_SECONDS:
        _LAST_RECONCILE = now
        with LOCK:
            book = _exchange_book()
        report = TRADER.reconcile(book)
        if report.get("error") is None:
            with LOCK:
                missing = [p for p in _positions() if p.get("exchange") and p["pair"] in report["missingOnExchange"]]
            for pos in missing:
                try:
                    if not _settle_closed(pos):
                        TRADER.alert("%s ya no está en Blofin y no hay fills de cierre todavía." % pos["pair"], "error")
                        TRADER.mark_unsafe("cierre_sin_fills_%s" % pos["pair"])
                    else:
                        TRADER.clear_unsafe("cierre_sin_fills_%s" % pos["pair"])
                except Exception as exc:
                    TRADER.alert("No se pudo liquidar %s: %r" % (pos["pair"], exc), "error")
            for pair in report["withoutSl"]:
                with LOCK:
                    pos = _find_position(pair)
                if pos is not None and not pos.get("closeRequested"):
                    tid = TRADER.ensure_protection(pair, pos["side"], pos["sl"], pos["tp"], pos.get("clientOrderId") or "gme")
                    with LOCK:
                        if tid is None:
                            pos["closeRequested"] = {"reason": "sin_sl", "at": _now()}
                        else:
                            pos["tpslId"] = tid
            with LOCK:
                for pos in _positions():
                    e = (TRADER.last_positions or {}).get(pos.get("pair")) if pos.get("exchange") else None
                    if e:
                        try:
                            mark = blofin_trading.num(e.get("markPrice"))
                            upnl = blofin_trading.num(e.get("unrealizedPnl"))
                            if mark is not None and mark > 0 and upnl is not None:
                                pos["exchangeMark"] = {"markPrice": mark, "unrealizedPnl": upnl,
                                                       "at": _now(), "atMs": int(now_epoch() * 1000)}
                        except (TypeError, ValueError, AttributeError):
                            pass
            try:
                acc = TRADER.account()
                acc["fetchedAtMs"] = int(now_epoch() * 1000)  # server clock, same as the panel ages
                # Same cycle as the reconcile that just read the positions.
                acc["positionsMargin"] = TRADER.positions_margin(TRADER.last_positions)
                with LOCK:
                    STATE["exchangeAccount"] = acc
                # 1.7.6: USDT equity (details[USDT].equity), not totalEquity (USD).
                bal = next((acc[k] for k in ("equity", "totalEquity", "available") if acc.get(k) is not None), None)
                if bal is not None:
                    with LOCK:
                        STATE["balance"] = round(bal, 4)
                        STATE["balanceSource"] = "blofin-demo"
                        _migrate_demo_base_usdt(acc)
                        if STATE.get("initialBalance") is None:
                            # Demo starts from the first balance Blofin reports
                            # (minus the PnL of demo trades already booked and
                            # the open PnL already inside that equity).
                            booked = sum(float(h.get("pnl") or 0) for h in STATE.get("history") or [])
                            open_pnl = sum((_num_or_none((p.get("exchangeMark") or {}).get("unrealizedPnl")) or 0.0)
                                           for p in _positions() if p.get("exchange"))
                            STATE["initialBalance"] = round(bal - booked - open_pnl, 4)
                            add_log("Saldo inicial DEMO fijado en %.2f USDT (Blofin)." % STATE["initialBalance"])
            except Exception as exc:
                TRADER.alert("Balance demo no disponible: %r" % exc)
        with LOCK:
            STATE["lastReconcileAt"] = _now()
            save_state()
    if JOURNAL is not None and now - _LAST_FX >= 6 * 3600:
        _LAST_FX = now
        try:
            if JOURNAL.pending_fx():
                import fiscal_journal
                JOURNAL.resolve_pending(fiscal_journal.fetch_ecb_rates())
        except Exception as exc:
            log.warning("FX BCE pendiente: %r", exc)


def trading_loop():
    while not STOP.is_set():
        _beat("granmago-trading")
        try:
            trading_cycle()
        except Exception:
            log.exception("trading")
        _TRADE_WAKE.wait(5)
        _TRADE_WAKE.clear()


def health():
    """Compact read-only health for supervisors and HA REST sensors."""
    with LOCK:
        open_n = len(_positions())
        kill = bool(STATE.get("killSwitch"))
        running = bool(STATE.get("running"))
        last_tick = STATE.get("lastTickAt")
    stats = market_stats()
    trading = TRADER.status() if TRADER is not None else {"mode": trading_mode()}
    threads = thread_health()
    ok = (not TRADING_ERROR) and (TRADER is None or TRADER.safe) and not any(t["stale"] for t in threads.values())
    return {
        "threads": threads, "clockSkewSeconds": stats.get("clockSkewSeconds"),
        "version": VERSION, "mode": trading_mode(), "running": running, "killSwitch": kill,
        "openPositions": open_n, "lastTickAt": last_tick, "ok": ok, "tradingError": TRADING_ERROR,
        "degradedPairs": stats["degraded"], "safe": trading.get("safe", True),
        "unsafeReasons": trading.get("unsafeReasons", []),
        "alerts": (list(ALERTS)[-5:]) + (trading.get("alerts", [])[-5:]),
    }


# =============================================================== HTTP control
# C5: control endpoints only from HA ingress (172.30.32.2), loopback, or with
# the control_token option in the X-GranMago-Token header. Reads stay open.
INGRESS_IP = "172.30.32.2"


def _control_allowed():
    token = os.environ.get("CONTROL_TOKEN") or ""
    sent = request.headers.get("X-GranMago-Token") or ""
    if token and sent and hmac.compare_digest(sent.encode(), token.encode()):
        return True
    return request.remote_addr in ("127.0.0.1", "::1", INGRESS_IP)


def control(fn):
    import functools

    @functools.wraps(fn)
    def wrapper(*a, **kw):
        if not _control_allowed():
            log.warning("Control %s rechazado desde %s", request.path, request.remote_addr)
            return jsonify({"ok": False, "error": "no_autorizado",
                            "hint": "usa el panel de HA (ingress) o la cabecera X-GranMago-Token"}), 403
        return fn(*a, **kw)
    return wrapper


HTML_PATH = Path(__file__).resolve().parent / "static" / "index.html"


@app.route("/")
def index():
    html = HTML_PATH.read_text(encoding="utf-8")
    ingress = request.headers.get("X-Ingress-Path", "").rstrip("/")
    return html.replace("__INGRESS__", ingress)


def _finite(obj):
    """Replace NaN/Infinity (invalid JSON: the panel's res.json() would fail and
    show "Sin conexión") with None, recursively."""
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _finite(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_finite(v) for v in obj]
    return obj


@app.route("/api/state")
def api_state():
    return jsonify(_finite(public_state()))


@app.route("/api/start", methods=["POST"])
@control
def api_start():
    if TRADING_ERROR:
        return jsonify({"ok": False, "error": "trading_bloqueado", "detail": TRADING_ERROR}), 409
    body = request.get_json(silent=True) or {}
    with LOCK:
        _apply_settings_locked(body)
        if TRADER is None and not STATE["history"] and not _positions() and body.get("initialBalance"):
            try:
                bal = float(body["initialBalance"])
            except (TypeError, ValueError):
                bal = STATE["balance"]
            if math.isfinite(bal) and bal > 0:  # 1.7.4: NaN/Infinity never become the balance
                STATE["balance"] = bal
                STATE["initialBalance"] = bal
        STATE["running"] = True
        add_log("Bot iniciado en el servidor. Sigue aunque cierres esta página.")
        save_state()
    kick()
    return jsonify(_finite(public_state()))


@app.route("/api/stop", methods=["POST"])
@control
def api_stop():
    with LOCK:
        STATE["running"] = False
        add_log("Bot detenido: sin entradas nuevas." + (
            " El SL/TP sigue en Blofin y la conciliación sigue activa." if TRADER is not None else ""))
        save_state()
    kick()
    return jsonify(_finite(public_state()))


@app.route("/api/reset", methods=["POST"])
@control
def api_reset():
    if TRADER is not None:
        return jsonify({"ok": False, "error": "reset_desactivado_en_demo"}), 409
    body = request.get_json(silent=True) or {}
    with LOCK:
        try:
            bal = float(body.get("initialBalance") or STATE["settings"].get("initialHint") or os.environ.get("INITIAL_BALANCE") or 10000)
        except (TypeError, ValueError):
            bal = 10000
        if not math.isfinite(bal) or bal <= 0:  # 1.7.4: NaN passed "bal <= 0"
            bal = 10000
        running = STATE["running"]
        settings = STATE["settings"]
        keep = {k: STATE.get(k) for k in ("demoLedger", "killSwitch", "migrations")}
        STATE.clear()
        STATE.update(default_state())
        STATE.update({k: v for k, v in keep.items() if v is not None})
        STATE["balance"] = bal
        STATE["initialBalance"] = bal
        STATE["running"] = running
        STATE["settings"] = settings
        add_log("Reset. Balance %.2f" % bal)
        save_state()
    return jsonify(_finite(public_state()))


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


@app.route("/api/settings", methods=["POST"])
@control
def api_settings():
    if TRADER is not None:
        return jsonify({"ok": False, "error": "ajustes_solo_por_opciones_en_demo"}), 409
    body = request.get_json(silent=True) or {}
    with LOCK:
        _apply_settings_locked(body)
        save_state()
    return jsonify(_finite(public_state()))


@app.route("/api/scan", methods=["POST"])
@control
def api_scan():
    if SCAN_LOCK.locked():
        return jsonify({"ok": True, "busy": True})
    threading.Thread(target=scan_all, name="manual-scan", daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/close_position", methods=["POST"])
@control
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
            expected = _find_position(pair)
            if expected is None:
                return jsonify({"ok": False, "error": "sin_posicion"}), 409
            if expected.get("exchange"):
                if TRADER is None:
                    # 1.7.4: trading blocked, nobody would send this close; queuing
                    # it would fire it on Blofin whenever demo comes back.
                    add_log("No se cierra %s: posición DEMO y el trading está bloqueado (%s)." % (pair, TRADING_ERROR))
                    return jsonify({"ok": False, "error": "trading_bloqueado", "detail": TRADING_ERROR}), 409
                _request_exchange_close(expected, "manual")
                save_state()
                return jsonify(dict(public_state_locked_free(), ok=True, queued=True)), 202
        quote = fetch_quote(pair)
        with LOCK:
            if _find_position(pair) is not expected:
                return jsonify({"ok": False, "error": "posicion_cambiada"}), 409
            if not _position_quote_valid(quote, expected):
                expected["priceStale"] = True
                add_log("No se pudo cerrar %s: sin precio de mercado." % pair)
                save_state()
                return jsonify({"ok": False, "error": "sin_precio"}), 503
            close_position("manual", quote.price, pair, quote=quote)
            save_state()
        return jsonify(_finite(public_state()))
    finally:
        _closing.release()


def public_state_locked_free():
    return {"openPositions": [dict(p) for p in _positions()], "killSwitch": bool(STATE.get("killSwitch"))}


@app.route("/api/health")
def api_health():
    return jsonify(_finite(health()))


def kill_switch(enabled, close_all=False):
    """Stop new entries; optionally close every open position."""
    with LOCK:
        STATE["killSwitch"] = bool(enabled)
        add_log("Kill switch %s%s." % ("ACTIVADO" if enabled else "desactivado",
                                       " y cierre de todas las posiciones" if enabled and close_all else ""))
        _alert("kill_switch", "activado" if enabled else "desactivado")
        pairs = [p["pair"] for p in _positions()] if enabled and close_all else []
        if TRADER is None:
            # 1.7.4: demo positions cannot be closed while trading is blocked.
            held = [p["pair"] for p in _positions() if p.get("exchange") and p["pair"] in pairs]
            if held:
                add_log("Kill switch: %s son posiciones DEMO y el trading está bloqueado; no se cierran." % ", ".join(held))
            pairs = [p for p in pairs if p not in held]
        for pair in pairs:
            pos = _find_position(pair)
            if pos is not None and pos.get("exchange"):
                _request_exchange_close(pos, "kill_switch")
        save_state()
    closed = []
    for pair in pairs:
        with LOCK:
            pos = _find_position(pair)
        if pos is None or pos.get("exchange"):
            continue
        quote = fetch_quote(pair)
        with LOCK:
            if _find_position(pair) is pos and _position_quote_valid(quote, pos):
                close_position("kill_switch", quote.price, pair, quote=quote)
                closed.append(pair)
            elif _find_position(pair) is pos:
                pos["priceStale"] = True
                add_log("Kill switch: %s sin precio fresco, sigue abierta; reintenta." % pair)
            save_state()
    kick()
    return {"killSwitch": bool(enabled), "closeRequested": pairs, "closedPaper": closed}


@app.route("/api/kill_switch", methods=["POST"])
@control
def api_kill_switch():
    body = request.get_json(silent=True) or {}
    if "enabled" not in body:
        return jsonify({"ok": False, "error": "falta_enabled"}), 400
    return jsonify(dict(ok=True, **kill_switch(_as_bool(body.get("enabled"), False),
                                               _as_bool(body.get("closeAll"), False))))


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    init_trading()
    if TRADER is not None:
        start_trading()
        threading.Thread(target=_supervised("granmago-trading", trading_loop), name="granmago-trading",
                         daemon=True).start()
    with LOCK:
        add_log("Mercado exclusivo Blofin: libro (mid, ts del libro ≤ %ds) para entradas, PnL y cierres; sin fallback." % QUOTE_MAX_AGE_SECONDS)
        add_log("Umbral de entrada: %d." % entry_threshold())
        add_log("Máximo de posiciones: %d (una por moneda)." % max_positions())
        add_log("Universo: %d pares (%s)." % (
            len(active_pairs()),
            ", ".join(p.replace("-USDT", "") for p in active_pairs()),
        ))
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
        if strategy_mode() == "granmago":
            add_log(
                "Estrategia Gran Mago variante %s, estilo %s, setups %s, promedios %d, salida por agotamiento %s, máximo %d velas."
                % (
                    granmago_variant().upper(),
                    granmago_style(),
                    granmago_setups(),
                    granmago_max_adds(),
                    "sí" if granmago_momentum_exit() else "no",
                    granmago_max_hold_bars(),
                )
            )
        else:
            add_log("Estrategia: scanner de score. El modo Gran Mago está apagado.")
        if STATE["running"]:
            add_log("Bot reanudado al arrancar el complemento.")
        save_state()
    threading.Thread(target=_supervised("granmago-monitor", monitor_loop), name="granmago-monitor",
                     daemon=True).start()
    threading.Thread(target=_supervised("granmago-scan", scan_loop), name="granmago-scan", daemon=True).start()
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
