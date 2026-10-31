#!/usr/bin/env python3
"""GranMago Scanner — paper trading loop for the Home Assistant add-on.

The loop lives in this process. Closing the browser does not stop it.
"""

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from flask import Flask, jsonify, request

PAIRS = [
    "BTC-USDT", "ETH-USDT", "SOL-USDT", "XRP-USDT", "LINK-USDT",
]

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
SESSION.headers["User-Agent"] = "granmago-scanner/1.0"

LOCK = threading.Lock()
SCAN_LOCK = threading.Lock()
KICK = threading.Event()
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


def default_state():
    balance = _env_float("INITIAL_BALANCE", 10000)
    return {
        "running": False,
        "balance": balance,
        "initialBalance": balance,
        "openPosition": None,
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
        },
    }


def load_state():
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            base = default_state()
            if isinstance(data, dict):
                base.update({k: data[k] for k in base if k in data and k != "settings"})
                if isinstance(data.get("settings"), dict):
                    base["settings"].update(data["settings"])
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


def rsi(values, period=14):
    if len(values) < period + 1:
        return [50.0] * len(values)
    deltas = [values[i] - values[i-1] for i in range(1, len(values))]
    gains = [max(d, 0) for d in deltas]
    losses = [max(-d, 0) for d in deltas]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    out = [50.0] * (period)
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        rs = avg_gain / avg_loss if avg_loss > 0 else 100
        out.append(100 - 100 / (1 + rs))
    return out


def macd_hist(closes):
    if len(closes) < 26:
        return 0.0
    ema12 = ema(closes, 12)
    ema26 = ema(closes, 26)
    if ema12 is None or ema26 is None:
        return 0.0
    # approximate last hist
    return (ema12 - ema26) * 0.5  # rough; full series would be better but sufficient for gate


def analyze_pair(pair):
    """v7 Pullback-to-EMA + RSI Recovery on 15m."""
    candles = fetch_candles(pair, "15m", 300)
    if len(candles) < 200:
        return None
    closes = [c["c"] for c in candles]
    volumes = [c.get("vol", 0) or 0 for c in candles]
    last = candles[-1]
    ema9 = ema(closes, 9)
    ema21 = ema(closes, 21)
    ema55 = ema(closes, 55)
    ema200 = ema(closes, 200)
    rsi_series = rsi(closes, 14)
    rsi_now = rsi_series[-1]
    # lookback last 3 bars for pullback
    rsi_window = rsi_series[-4:]
    vol_sma = sum(volumes[-20:]) / 20 if len(volumes) >= 20 else 0
    vol_ok = volumes[-1] > 0.9 * vol_sma if vol_sma > 0 else False
    mh = macd_hist(closes)

    # LONG conditions (v7 defaults)
    long_ok = (
        ema200 and last["c"] > ema200 and
        ema21 and ema55 and ema21 > ema55 and
        any(r < 46 for r in rsi_window) and
        rsi_now >= 49 and
        ema9 and last["c"] > ema9 and
        mh > -50 and
        vol_ok
    )
    # SHORT mirror
    short_ok = (
        ema200 and last["c"] < ema200 and
        ema21 and ema55 and ema21 < ema55 and
        any(r > 54 for r in rsi_window) and
        rsi_now <= 51 and
        ema9 and last["c"] < ema9 and
        mh < 50 and
        vol_ok
    )
    score_long = 80 if long_ok else 40
    score_short = 80 if short_ok else 40
    return {
        "pair": pair,
        "price": last["c"],
        "bias": "ALCISTA" if last["c"] > ema200 else "BAJISTA",
        "scoreLong": score_long,
        "scoreShort": score_short,
        "nearEMA": True,
        "stoch": round(rsi_now, 1),
        "dailyLong": bool(long_ok),
        "dailyShort": bool(short_ok),
        "ema50_4": ema9,
        "recentLow": min(c["l"] for c in candles[-10:]),
        "recentHigh": max(c["h"] for c in candles[-10:]),
        "validLong": long_ok,
        "validShort": short_ok,
    }


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
    interval = {"15m": "15m", "4H": "4h", "1D": "1d", "1W": "1w"}.get(bar, "15m")
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


def scan_all():
    if not SCAN_LOCK.acquire(blocking=False):
        return None
    try:
        with LOCK:
            add_log("Escaneo de %d pares..." % len(PAIRS))
            save_state()
        results = []
        for pair in PAIRS:
            if STOP.is_set():
                break
            try:
                row = analyze_pair(pair)
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
    entry = res["price"]
    # Fixed SL/TP from v7 (2.5% / 4.0%)
    sl_pct = 0.025
    tp_pct = 0.040
    if side == "long":
        sl = entry * (1 - sl_pct)
        tp = entry * (1 + tp_pct)
    else:
        sl = entry * (1 + sl_pct)
        tp = entry * (1 - tp_pct)
    risk_pct = float(STATE["settings"]["riskPct"]) / 100
    lev = int(STATE["settings"]["leverage"])
    risk_amt = STATE["balance"] * risk_pct
    notional = risk_amt / sl_pct
    STATE["openPosition"] = {
        "pair": res["pair"],
        "side": side,
        "entry": entry,
        "sl": sl,
        "tp": tp,
        "notional": round(notional, 2),
        "leverage": lev,
        "riskAmt": round(risk_amt, 2),
        "time": _now(),
        "unrealized": 0,
        "price": entry,
    }
    add_log("ENTRADA v7 %s %s @ %s (SL %.1f%% TP %.1f%%)" % (
        side.upper(), res["pair"], _fmt(entry), sl_pct*100, tp_pct*100))


def close_position(reason, price):
    pos = STATE["openPosition"]
    if not pos:
        return
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
    STATE["openPosition"] = None


def monitor_position():
    with LOCK:
        pos = STATE["openPosition"]
        if not pos:
            return
        pair = pos["pair"]
        side = pos["side"]
        sl = pos["sl"]
        tp = pos["tp"]
    price = fetch_price(pair)
    if price is None:
        with LOCK:
            add_log("Sin precio de %s, se reintenta" % pair)
            STATE["lastTickAt"] = _now()
            save_state()
        return
    with LOCK:
        pos = STATE["openPosition"]
        if not pos or pos["pair"] != pair:
            return
        if side == "long":
            pnl_pct = (price - pos["entry"]) / pos["entry"]
        else:
            pnl_pct = (pos["entry"] - price) / pos["entry"]
        pos["price"] = price
        pos["unrealized"] = round(pnl_pct * pos["notional"], 2)
        hit = None
        if (side == "long" and price <= sl) or (side == "short" and price >= sl):
            hit = "SL"
        elif (side == "long" and price >= tp) or (side == "short" and price <= tp):
            hit = "TP"
        if hit:
            close_position(hit, price)
        STATE["lastTickAt"] = _now()
        save_state()


def maybe_open():
    with LOCK:
        if STATE["openPosition"] or not STATE["running"]:
            return
        best_l = STATE["bestLong"]
        best_s = STATE["bestShort"]
        if best_l and best_l["validLong"]:
            open_from_result(best_l, "long")
        elif best_s and best_s["validShort"]:
            open_from_result(best_s, "short")
        else:
            add_log("Ningún setup v7 válido. Esperando...")
        save_state()


def _fmt(price):
    if price >= 1000:
        return "%.0f" % price
    if price >= 1:
        return "%.2f" % price
    return "%.4f" % price


def loop():
    log.info("loop started, running=%s", STATE["running"])
    if STATE["running"]:
        add_log("Bot reanudado al arrancar el complemento.")
        save_state()
    while not STOP.is_set():
        with LOCK:
            running = STATE["running"]
            has_pos = STATE["openPosition"] is not None
            interval = max(30, int(STATE["settings"].get("scanSeconds") or 90))
        if running and has_pos:
            try:
                monitor_position()
            except Exception as exc:
                log.exception("monitor")
                with LOCK:
                    add_log("Fallo vigilando la posición, el bot sigue: %s" % exc)
                    save_state()
            KICK.wait(MONITOR_SECONDS)
        elif running:
            try:
                scan_all()
                maybe_open()
            except Exception as exc:
                log.exception("scan")
                with LOCK:
                    add_log("Fallo en el ciclo, el bot sigue: %s" % exc)
                    STATE["lastTickAt"] = _now()
                    save_state()
            KICK.wait(interval)
        else:
            KICK.wait(2)
        KICK.clear()


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
        if not STATE["history"] and not STATE["openPosition"] and body.get("initialBalance"):
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
    KICK.set()
    return jsonify(public_state())


@app.route("/api/stop", methods=["POST"])
def api_stop():
    with LOCK:
        STATE["running"] = False
        add_log("Bot detenido.")
        save_state()
    KICK.set()
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


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    threading.Thread(target=loop, name="granmago-loop", daemon=True).start()
    log.info("GranMago Scanner on 0.0.0.0:%s data=%s", PORT, DATA_DIR)
    from waitress import serve
    serve(app, host="0.0.0.0", port=PORT, threads=4)


if __name__ == "__main__":
    main()
