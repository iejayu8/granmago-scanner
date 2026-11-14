"""Blofin private trading layer for GranMago (demo first, live blocked).

Everything that touches a Blofin account lives here. The strategy in
server.py decides *what* to trade; this module decides *how*, and the
exchange is the source of truth.

Safety model
* Modes: ``paper`` (default, no account), ``demo`` (Blofin Demo Trading,
  https://demo-trading-openapi.blofin.com) and ``live``. ``live`` is BLOCKED
  in code: ``TradingConfig.from_env`` raises ``LiveTradingBlocked``.
* Signed requests per docs.blofin.com "Signature": prehash = requestPath
  (with query) + METHOD + timestamp(ms) + nonce + body; HMAC-SHA256 with the
  secret, hex digest, then Base64 of that hex string.
* Never retry an order POST blindly. Every entry/close has a deterministic
  clientOrderId and an intent written (fsync) before the POST. After a
  timeout or an ambiguous answer the order is looked up by clientOrderId.
* SL and TP live on the exchange: attached to the market order and verified
  in GET orders-tpsl-pending; if missing a full-position TPSL (size -1,
  reduceOnly) is placed; if that fails the position is closed at market.
* Isolated margin, One-way (net_mode), leverage set per instrument; sizes in
  contracts rounded down to lotSize, checked against minSize/maxMarketSize;
  SL/TP rounded to tickSize on the safe side.
* Private requests share one sliding-window limiter, by default 25 per 10 s
  (Blofin: 30 trading requests per 10 s per user).
* Secrets never leave this object: ``repr`` is redacted, and
  ``SecretRedactor`` scrubs them from every log record.
"""
import base64
import hashlib
import hmac
import json
import logging
import math
import os
import threading
import time
import uuid
from collections import deque
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from pathlib import Path
from urllib.parse import urlencode

log = logging.getLogger("granmago.trading")

DEMO_BASE = "https://demo-trading-openapi.blofin.com"
LIVE_BASE = "https://openapi.blofin.com"
MODES = ("paper", "demo", "live")
TRADE_WINDOW_SECONDS = 10.0
TRADE_WINDOW_MAX = 25          # documented limit is 30 / 10 s per user
ORDER_TIMEOUT = (3.0, 6.0)     # connect, read
QUERY_TIMEOUT = (3.0, 5.0)
CLOCK_MAX_SKEW_SECONDS = 5.0
LIVE_BLOCKED_MESSAGE = (
    "trading_mode=live (dinero real) está BLOQUEADO en esta versión. Solo se permiten paper y demo "
    "(Blofin Demo Trading). Cambia trading_mode a paper o demo.")


class LiveTradingBlocked(RuntimeError):
    pass


class TradingConfigError(ValueError):
    pass


class BlofinError(RuntimeError):
    def __init__(self, code, msg, path=""):
        super().__init__("Blofin %s code=%s msg=%s" % (path, code, msg))
        self.code = str(code)
        self.msg = str(msg)
        self.path = path


class AmbiguousResult(RuntimeError):
    """The request may or may not have reached Blofin (timeout, reset, 5xx)."""


class RateLimited(RuntimeError):
    pass


# --------------------------------------------------------------------- config
class TradingConfig:
    __slots__ = ("mode", "base_url", "_key", "_secret", "_passphrase", "leverage", "trigger_type")

    def __init__(self, mode, key="", secret="", passphrase="", leverage=3, trigger_type="last"):
        if mode not in MODES:
            raise TradingConfigError("trading_mode debe ser paper, demo o live")
        if mode == "live":
            raise LiveTradingBlocked(LIVE_BLOCKED_MESSAGE)
        self.mode = mode
        self.base_url = DEMO_BASE if mode == "demo" else ""
        self._key, self._secret, self._passphrase = key or "", secret or "", passphrase or ""
        if mode == "demo" and not (self._key and self._secret and self._passphrase):
            raise TradingConfigError("modo demo sin blofin_api_key/blofin_api_secret/blofin_api_passphrase")
        self.leverage = int(leverage)
        if trigger_type not in ("last", "mark", "index"):
            raise TradingConfigError("trigger price type inválido")
        self.trigger_type = trigger_type

    @classmethod
    def from_env(cls, environ=None):
        env = os.environ if environ is None else environ
        mode = (env.get("TRADING_MODE") or "paper").strip().lower()
        return cls(mode, env.get("BLOFIN_API_KEY", ""), env.get("BLOFIN_API_SECRET", ""),
                   env.get("BLOFIN_API_PASSPHRASE", ""), int(float(env.get("LEVERAGE") or 3)),
                   (env.get("TRIGGER_PRICE_TYPE") or "last").strip().lower())

    def secrets(self):
        return [s for s in (self._key, self._secret, self._passphrase) if s]

    def __repr__(self):
        return "TradingConfig(mode=%r, base_url=%r, key=%s)" % (self.mode, self.base_url, "***" if self._key else "")

    __str__ = __repr__


class SecretRedactor(logging.Filter):
    """Scrub secret values from every log record (message and args)."""

    def __init__(self, secrets):
        super().__init__()
        self.secrets = [s for s in secrets if s and len(s) >= 4]

    def scrub(self, text):
        for s in self.secrets:
            text = text.replace(s, "***")
        return text

    def filter(self, record):
        if self.secrets:
            try:
                msg = record.getMessage()
            except Exception:
                return True
            record.msg = self.scrub(msg)
            record.args = ()
        return True


def install_redactor(secrets):
    red = SecretRedactor(secrets)
    root = logging.getLogger()
    root.addFilter(red)
    for h in root.handlers:
        h.addFilter(red)
    for name in ("granmago", "granmago.trading", "waitress", "urllib3"):
        logging.getLogger(name).addFilter(red)
    return red


# -------------------------------------------------------------------- signing
def sign(secret, request_path, method, timestamp, nonce, body=""):
    prehash = "%s%s%s%s%s" % (request_path, method.upper(), timestamp, nonce, body or "")
    hexdigest = hmac.new(secret.encode(), prehash.encode(), hashlib.sha256).hexdigest()
    return base64.b64encode(hexdigest.encode()).decode()


def client_order_id(pair, side, confirm_ms, close_ms, purpose="e"):
    """Deterministic, alphanumeric, 32 chars: same signal -> same id."""
    raw = "%s|%s|%s|%s|%s" % (pair, side, confirm_ms, close_ms, purpose)
    return ("gm" + purpose[:1] + hashlib.sha256(raw.encode()).hexdigest())[:32]


# ------------------------------------------------------------- rate limiting
class SlidingWindowLimiter:
    def __init__(self, max_calls=TRADE_WINDOW_MAX, window=TRADE_WINDOW_SECONDS, clock=time.monotonic,
                 sleep=time.sleep):
        if max_calls > 30:
            raise ValueError("Blofin allows 30 trading requests per 10 s")
        self.max_calls, self.window = max_calls, window
        self._clock, self._sleep = clock, sleep
        self._calls = deque()
        self._lock = threading.Lock()

    def acquire(self, max_wait=15.0):
        deadline = self._clock() + max_wait
        while True:
            with self._lock:
                now = self._clock()
                while self._calls and now - self._calls[0] >= self.window:
                    self._calls.popleft()
                if len(self._calls) < self.max_calls:
                    self._calls.append(now)
                    return
                wait = self.window - (now - self._calls[0]) + 0.01
            if self._clock() + wait > deadline:
                raise RateLimited("presupuesto de trading agotado (%d/%ss)" % (self.max_calls, self.window))
            self._sleep(wait)

    def in_window(self):
        with self._lock:
            now = self._clock()
            return sum(1 for t in self._calls if now - t < self.window)


# ------------------------------------------------------------------- sizing
def _dec(x):
    return Decimal(str(x))


def round_to_step(value, step, mode):
    v, s = _dec(value), _dec(step)
    if s <= 0:
        return float(v)
    q = (v / s).to_integral_value(rounding=mode)
    return float(q * s)


def size_contracts(notional_usdt, price, meta):
    """Contracts for a target notional, floored to lotSize. (contracts, reason)."""
    cv, lot, min_size = float(meta["contractValue"]), float(meta["lotSize"]), float(meta["minSize"])
    max_mkt = float(meta.get("maxMarketSize") or 0) or math.inf
    if notional_usdt <= 0 or price <= 0 or cv <= 0 or lot <= 0:
        return 0.0, "parámetros de tamaño inválidos"
    raw = notional_usdt / (price * cv)
    contracts = round_to_step(raw, lot, ROUND_FLOOR)
    if contracts < min_size:
        return 0.0, "skip_min_size (%.8g < minSize %s)" % (contracts, meta["minSize"])
    if contracts > max_mkt:
        contracts = round_to_step(max_mkt, lot, ROUND_FLOOR)
    return contracts, None


def round_bracket(side, sl, tp, tick):
    """SL toward the entry (never further = never more risk); TP toward the entry."""
    if side == "long":
        return round_to_step(sl, tick, ROUND_CEILING), round_to_step(tp, tick, ROUND_FLOOR)
    return round_to_step(sl, tick, ROUND_FLOOR), round_to_step(tp, tick, ROUND_CEILING)


def fmt_num(x):
    d = _dec(x).normalize()
    s = format(d, "f")
    return s


# --------------------------------------------------------------- transport
def requests_transport():
    import requests
    session = requests.Session()
    session.headers["User-Agent"] = "granmago-scanner-trading"

    def call(method, url, headers, data, timeout):
        try:
            resp = session.request(method, url, headers=headers, data=data, timeout=timeout,
                                   allow_redirects=False)
        except (requests.Timeout, requests.ConnectionError) as exc:
            raise AmbiguousResult("%s %s" % (type(exc).__name__, url.split("?")[0])) from None
        if resp.status_code in (500, 502, 503, 504):
            raise AmbiguousResult("HTTP %s" % resp.status_code)
        try:
            body = resp.json()
        except ValueError:
            raise AmbiguousResult("respuesta no JSON HTTP %s" % resp.status_code) from None
        return resp.status_code, body, resp.headers.get("Date")

    return call


class IntentLog:
    """Append-only JSONL of order intents/outcomes (fsync before each POST)."""

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def append(self, record):
        record = dict(record, at=int(time.time() * 1000))
        line = json.dumps(record, sort_keys=True, default=str)
        with self._lock:
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
                fh.flush()
                os.fsync(fh.fileno())

    def pending(self):
        """clientOrderIds whose last record is still 'pending' or 'unknown'."""
        last = {}
        if not self.path.exists():
            return {}
        with open(self.path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                cid = rec.get("clientOrderId")
                if cid:
                    last[cid] = rec
        return {cid: rec for cid, rec in last.items() if rec.get("status") in ("pending", "unknown")}


# ------------------------------------------------------------------ client
class BlofinPrivateClient:
    def __init__(self, config, transport=None, limiter=None, clock=time.time, nonce=None):
        if config.mode != "demo":
            raise TradingConfigError("cliente privado solo en modo demo")
        self.config = config
        self._transport = transport or requests_transport()
        self.limiter = limiter or SlidingWindowLimiter()
        self._clock = clock
        self._nonce = nonce or (lambda: str(uuid.uuid4()))
        self.stats = {"requests": 0, "errors": 0, "ambiguous": 0, "lastError": None}
        self.server_skew = None

    def __repr__(self):
        return "BlofinPrivateClient(%r)" % (self.config,)

    def request(self, method, path, params=None, body=None, timeout=QUERY_TIMEOUT):
        method = method.upper()
        request_path = path
        query = urlencode([(k, v) for k, v in (params or {}).items() if v not in (None, "")])
        if query:  # never a bare "?": the signed path must equal the path the server sees
            request_path += "?" + query
        body_str = json.dumps(body) if body is not None else ""
        self.limiter.acquire()  # before the timestamp: a wait never ages the signature
        ts = str(int(self._clock() * 1000))
        nonce = self._nonce()
        headers = {
            "ACCESS-KEY": self.config._key,
            "ACCESS-SIGN": sign(self.config._secret, request_path, method, ts, nonce, body_str),
            "ACCESS-TIMESTAMP": ts,
            "ACCESS-NONCE": nonce,
            "ACCESS-PASSPHRASE": self.config._passphrase,
            "Content-Type": "application/json",
        }
        self.stats["requests"] += 1
        try:
            status, data, date_header = self._transport(method, self.config.base_url + request_path, headers,
                                                         body_str or None, timeout)
        except AmbiguousResult as exc:
            self.stats["ambiguous"] += 1
            self.stats["lastError"] = str(exc)
            raise
        self._note_date(date_header)
        if status == 429 or status == 403:
            self.stats["errors"] += 1
            self.stats["lastError"] = "HTTP %s" % status
            raise BlofinError(str(status), "rate limit / firewall", path)
        if not isinstance(data, dict) or str(data.get("code")) != "0":
            code = data.get("code") if isinstance(data, dict) else status
            msg = data.get("msg") if isinstance(data, dict) else ""
            self.stats["errors"] += 1
            self.stats["lastError"] = "%s %s %s" % (path, code, msg)
            raise BlofinError(code, msg, path)
        return data.get("data")

    def _note_date(self, date_header):
        if not date_header:
            return
        try:
            from email.utils import parsedate_to_datetime
            self.server_skew = round(self._clock() - parsedate_to_datetime(date_header).timestamp(), 1)
        except (TypeError, ValueError):
            pass


# ------------------------------------------------------------------ trader
class DemoTrader:
    """Executes GranMago decisions on a Blofin demo account."""

    def __init__(self, config, data_dir, transport=None, limiter=None, clock=time.time, sleep=time.sleep,
                 instrument_meta=None):
        self.config = config
        self.client = BlofinPrivateClient(config, transport=transport, limiter=limiter, clock=clock)
        self.intents = IntentLog(Path(data_dir) / "trading_intents.jsonl")
        self._sleep = sleep
        self._clock = clock
        self._instrument_meta = instrument_meta  # callable(instId, force) -> meta dict or None
        self._lock = threading.RLock()
        self._leverage_set = set()
        self.account_ready = False
        self.safe = True
        self.unsafe_reasons = []
        self.alerts = deque(maxlen=50)
        self.last_reconcile = None
        self.last_reconcile_result = None
        self.key_info = None

    def __repr__(self):
        return "DemoTrader(%r)" % (self.config,)

    # -------------------------------------------------------------- status
    def alert(self, msg, level="warning"):
        entry = {"at": int(self._clock() * 1000), "level": level, "msg": str(msg)[:400]}
        self.alerts.append(entry)
        getattr(log, "error" if level == "error" else "warning")("TRADING %s", msg)
        return entry

    def mark_unsafe(self, reason):
        if reason not in self.unsafe_reasons:
            self.unsafe_reasons.append(reason)
            self.alert("estado seguro: %s" % reason, "error")
        self.safe = False

    def clear_unsafe(self, prefix):
        self.unsafe_reasons = [r for r in self.unsafe_reasons if not r.startswith(prefix)]
        self.safe = not self.unsafe_reasons

    def can_open(self):
        return self.account_ready and self.safe

    def status(self):
        return {
            "mode": self.config.mode,
            "endpoint": self.config.base_url,
            "accountReady": self.account_ready,
            "safe": self.safe,
            "unsafeReasons": list(self.unsafe_reasons),
            "lastReconcileAt": self.last_reconcile,
            "lastReconcile": self.last_reconcile_result,
            "alerts": list(self.alerts)[-10:],
            "rateWindowUsed": self.client.limiter.in_window(),
            "rateWindowMax": self.client.limiter.max_calls,
            "requests": dict(self.client.stats),
            "serverSkewSeconds": self.client.server_skew,
            "apiKey": None if not self.key_info else {
                "readOnly": self.key_info.get("readOnly"), "ipsBound": bool(self.key_info.get("ips"))},
        }

    # ------------------------------------------------------------- startup
    def prepare_account(self):
        """Key info, clock, net_mode and isolated margin. Fail closed."""
        with self._lock:
            try:
                info = self.client.request("GET", "/api/v1/user/query-apikey") or {}
                self.key_info = {"readOnly": info.get("readOnly"), "ips": list(info.get("ips") or [])}
                if str(info.get("readOnly")) == "1":
                    self.mark_unsafe("api_key_solo_lectura")
                    return False
                if not info.get("ips"):
                    self.alert("La API key no está restringida por IP (recomendado restringirla).")
                if self.client.server_skew is not None and abs(self.client.server_skew) > CLOCK_MAX_SKEW_SECONDS:
                    self.mark_unsafe("reloj_desfasado_%ss" % self.client.server_skew)
                    return False
                mode = self.client.request("GET", "/api/v1/account/position-mode") or {}
                if mode.get("positionMode") != "net_mode":
                    positions = self.client.request("GET", "/api/v1/account/positions") or []
                    if any(float(p.get("positions") or 0) != 0 for p in positions):
                        self.mark_unsafe("position_mode_no_net_con_posiciones")
                        return False
                    self.client.request("POST", "/api/v1/account/set-position-mode",
                                        body={"positionMode": "net_mode"})
                self.client.request("POST", "/api/v1/account/set-margin-mode", body={"marginMode": "isolated"})
                self.account_ready = True
                self.clear_unsafe("cuenta_")
                return True
            except (BlofinError, AmbiguousResult, RateLimited) as exc:
                self.mark_unsafe("cuenta_no_preparada: %s" % exc)
                return False

    def ensure_leverage(self, inst_id):
        if inst_id in self._leverage_set:
            return
        self.client.request("POST", "/api/v1/account/set-leverage", body={
            "instId": inst_id, "leverage": str(self.config.leverage), "marginMode": "isolated",
            "positionSide": "net"})
        self._leverage_set.add(inst_id)

    def balance(self):
        data = self.client.request("GET", "/api/v1/account/balance") or {}
        if isinstance(data, list):
            data = data[0] if data else {}
        for key in ("totalEquity", "equity", "available"):
            try:
                return float(data[key])
            except (KeyError, TypeError, ValueError):
                continue
        return None

    # --------------------------------------------------------------- orders
    def order_detail(self, inst_id, cid=None, order_id=None):
        try:
            return self.client.request("GET", "/api/v1/trade/order-detail",
                                       params={"instId": inst_id, "orderId": order_id, "clientOrderId": cid})
        except BlofinError as exc:
            if exc.code in ("404", "102067", "102068") or "not exist" in exc.msg.lower() or "not found" in exc.msg.lower():
                return None
            raise

    def _lookup_after_ambiguity(self, inst_id, cid, attempts=4, spacing=1.0):
        """Never resend. Ask for the order by clientOrderId a few times."""
        for i in range(attempts):
            try:
                detail = self.order_detail(inst_id, cid=cid)
                if detail:
                    return detail
            except AmbiguousResult:
                pass
            self._sleep(spacing)
        return None

    def _wait_fill(self, inst_id, cid, order_id, attempts=6, spacing=0.5):
        detail = None
        for _ in range(attempts):
            detail = self.order_detail(inst_id, cid=cid, order_id=order_id)
            if detail and detail.get("state") in ("filled", "canceled", "partially_canceled"):
                return detail
            self._sleep(spacing)
        return detail

    def open_position(self, plan):
        """Market entry with attached SL/TP. Returns a fill dict or None.

        plan: pair, side, notional, entry (reference), sl, tp, confirmMs, closeMs.
        """
        with self._lock:
            pair, side = plan["pair"], plan["side"]
            if not self.can_open():
                return self._refuse(plan, "trading en estado seguro / cuenta no preparada")
            meta = self._instrument_meta(pair, True) if self._instrument_meta else None
            if not meta or meta.get("state") != "live" or meta.get("apiPermission") != "full":
                return self._refuse(plan, "instrumento no live o apertura API restringida")
            contracts, why = size_contracts(plan["notional"], plan["entry"], meta)
            if not contracts:
                return self._refuse(plan, why)
            sl, tp = round_bracket(side, plan["sl"], plan["tp"], float(meta["tickSize"]))
            cv = float(meta["contractValue"])
            real_risk = contracts * cv * abs(plan["entry"] - sl)
            if plan.get("riskAmt") and real_risk > float(plan["riskAmt"]) * 1.02 + 1e-9:
                return self._refuse(plan, "riesgo redondeado %.4f > objetivo %.4f" % (real_risk, plan["riskAmt"]))
            cid = client_order_id(pair, side, plan.get("confirmMs"), plan.get("closeMs"), "e")
            body = {
                "instId": pair, "marginMode": "isolated", "positionSide": "net",
                "side": "buy" if side == "long" else "sell", "orderType": "market",
                "size": fmt_num(contracts), "clientOrderId": cid,
                "slTriggerPrice": fmt_num(sl), "slOrderPrice": "-1", "slTriggerPriceType": self.config.trigger_type,
                "tpTriggerPrice": fmt_num(tp), "tpOrderPrice": "-1", "tpTriggerPriceType": self.config.trigger_type,
            }
            try:
                self.ensure_leverage(pair)
            except (BlofinError, AmbiguousResult, RateLimited) as exc:
                return self._refuse(plan, "set-leverage falló: %s" % exc)
            self.intents.append({"clientOrderId": cid, "kind": "entry", "status": "pending", "pair": pair,
                                 "side": side, "size": body["size"], "sl": sl, "tp": tp, "plan": plan})
            order_id = None
            try:
                data = self.client.request("POST", "/api/v1/trade/order", body=body, timeout=ORDER_TIMEOUT)
                row = data[0] if isinstance(data, list) and data else (data or {})
                if str(row.get("code", "0")) != "0":
                    if str(row.get("code")) == "102002":  # duplicate clientOrderId: it already exists
                        detail = self.order_detail(pair, cid=cid)
                    else:
                        self.intents.append({"clientOrderId": cid, "status": "rejected", "code": row.get("code"),
                                             "msg": row.get("msg")})
                        return self._refuse(plan, "orden rechazada %s %s" % (row.get("code"), row.get("msg")))
                else:
                    order_id = row.get("orderId")
            except BlofinError as exc:
                if exc.code == "102002":
                    order_id = None
                else:
                    self.intents.append({"clientOrderId": cid, "status": "rejected", "code": exc.code, "msg": exc.msg})
                    return self._refuse(plan, str(exc))
            except (AmbiguousResult, RateLimited) as exc:
                self.alert("Entrada %s %s ambigua (%s): consulto por clientOrderId, no reenvío." % (side, pair, exc))
                detail = self._lookup_after_ambiguity(pair, cid)
                if not detail:
                    self.intents.append({"clientOrderId": cid, "status": "unknown"})
                    self.mark_unsafe("orden_ambigua_%s" % cid)
                    return None
                order_id = detail.get("orderId")
            detail = self._wait_fill(pair, cid, order_id)
            filled = float((detail or {}).get("filledSize") or 0)
            if not detail or filled <= 0:
                state = (detail or {}).get("state")
                if state in ("canceled", "partially_canceled") or (detail and filled == 0 and state != "live"):
                    self.intents.append({"clientOrderId": cid, "status": "not_filled", "state": state})
                    return self._refuse(plan, "orden sin ejecutar (%s)" % state)
                self.intents.append({"clientOrderId": cid, "status": "unknown", "state": state})
                self.mark_unsafe("entrada_sin_confirmar_%s" % cid)
                return None
            fill = {
                "pair": pair, "side": side, "contracts": filled, "contractValue": cv,
                "entry": float(detail.get("averagePrice") or plan["entry"]),
                "orderId": str(detail.get("orderId") or order_id or ""), "clientOrderId": cid,
                "fee": abs(float(detail.get("fee") or 0)), "sl": sl, "tp": tp,
                "openedMs": int(detail.get("updateTime") or detail.get("createTime") or self._clock() * 1000),
            }
            fill["notional"] = round(fill["contracts"] * cv * fill["entry"], 4)
            self.intents.append({"clientOrderId": cid, "status": "filled", "orderId": fill["orderId"],
                                 "contracts": filled, "avg": fill["entry"]})
            fill["tpslId"] = self.ensure_protection(pair, side, sl, tp, cid)
            if fill["tpslId"] is None:
                return None  # position closed by ensure_protection
            return fill

    def _refuse(self, plan, why):
        self.alert("No se abre %s %s en demo: %s" % (plan.get("side"), plan.get("pair"), why), "info")
        return None

    def active_tpsl(self, inst_id=None):
        data = self.client.request("GET", "/api/v1/trade/orders-tpsl-pending", params={"instId": inst_id}) or []
        # "effective" means the trigger already fired: it no longer protects.
        return [t for t in data if t.get("state") in ("live", None)]

    def ensure_protection(self, pair, side, sl, tp, cid_base):
        """SL must exist on the exchange. Returns tpslId ('attached' if attached), or None after closing."""
        try:
            for t in self.active_tpsl(pair):
                if t.get("instId") == pair and t.get("slTriggerPrice"):
                    return str(t.get("tpslId") or "attached")
            data = self.client.request("POST", "/api/v1/trade/order-tpsl", body={
                "instId": pair, "marginMode": "isolated", "positionSide": "net",
                "side": "sell" if side == "long" else "buy",
                "slTriggerPrice": fmt_num(sl), "slOrderPrice": "-1", "slTriggerPriceType": self.config.trigger_type,
                "tpTriggerPrice": fmt_num(tp), "tpOrderPrice": "-1", "tpTriggerPriceType": self.config.trigger_type,
                "size": "-1", "reduceOnly": "true",
                "clientOrderId": ("gms" + cid_base[3:])[:32]}, timeout=ORDER_TIMEOUT)
            row = data[0] if isinstance(data, list) and data else (data or {})
            if str(row.get("code", "0")) != "0" or not row.get("tpslId"):
                raise BlofinError(row.get("code"), row.get("msg"), "/api/v1/trade/order-tpsl")
            self.alert("SL/TP no estaba en el exchange para %s: colocado TPSL %s." % (pair, row.get("tpslId")))
            return str(row["tpslId"])
        except (BlofinError, AmbiguousResult, RateLimited) as exc:
            self.alert("No hay SL en el exchange para %s (%s): cierro a mercado." % (pair, exc), "error")
            self.close_position(pair, "sin_sl", cid_base)
            return None

    def replace_stop(self, pair, side, new_sl, tp, old_tpsl_id, cid_base, tick):
        """Place the new SL first, then cancel the old one: never unprotected."""
        sl, tp = round_bracket(side, new_sl, tp, tick)
        with self._lock:
            data = self.client.request("POST", "/api/v1/trade/order-tpsl", body={
                "instId": pair, "marginMode": "isolated", "positionSide": "net",
                "side": "sell" if side == "long" else "buy",
                "slTriggerPrice": fmt_num(sl), "slOrderPrice": "-1", "slTriggerPriceType": self.config.trigger_type,
                "tpTriggerPrice": fmt_num(tp), "tpOrderPrice": "-1", "tpTriggerPriceType": self.config.trigger_type,
                "size": "-1", "reduceOnly": "true"}, timeout=ORDER_TIMEOUT)
            row = data[0] if isinstance(data, list) and data else (data or {})
            new_id = str(row.get("tpslId") or "")
            if not new_id:
                raise BlofinError(row.get("code"), row.get("msg"), "/api/v1/trade/order-tpsl")
            self.cancel_tpsl(pair, keep=new_id)
            return new_id, sl

    def cancel_tpsl(self, pair, keep=None):
        try:
            items = [t for t in self.active_tpsl(pair) if str(t.get("tpslId")) != str(keep)]
            if items:
                self.client.request("POST", "/api/v1/trade/cancel-tpsl",
                                    body=[{"instId": pair, "tpslId": str(t.get("tpslId"))} for t in items])
        except (BlofinError, AmbiguousResult, RateLimited) as exc:
            self.alert("No se pudieron cancelar TPSL de %s: %s" % (pair, exc))

    def close_position(self, pair, reason, cid_base=""):
        """Market close of the whole net position. True when no position remains."""
        with self._lock:
            cid = client_order_id(pair, reason, cid_base, int(self._clock() // 60), "x")
            self.intents.append({"clientOrderId": cid, "kind": "close", "status": "pending", "pair": pair,
                                 "reason": reason})
            try:
                self.client.request("POST", "/api/v1/trade/close-position", body={
                    "instId": pair, "marginMode": "isolated", "positionSide": "net", "clientOrderId": cid},
                    timeout=ORDER_TIMEOUT)
            except BlofinError as exc:
                if exc.code not in ("102005", "102022", "102002"):
                    self.intents.append({"clientOrderId": cid, "status": "rejected", "code": exc.code})
                    self.alert("Cierre %s rechazado: %s" % (pair, exc), "error")
                    return False
            except (AmbiguousResult, RateLimited) as exc:
                self.alert("Cierre %s ambiguo (%s): verifico posiciones." % (pair, exc))
            remaining = self.position(pair)
            done = remaining is None
            self.intents.append({"clientOrderId": cid, "status": "done" if done else "unknown"})
            if done:
                self.cancel_tpsl(pair)
            else:
                self.alert("La posición %s sigue abierta tras el cierre." % pair, "error")
            return done

    def positions(self):
        data = self.client.request("GET", "/api/v1/account/positions") or []
        return {p["instId"]: p for p in data if float(p.get("positions") or 0) != 0}

    def position(self, pair):
        return self.positions().get(pair)

    def exit_fills(self, pair, side, since_ms):
        """Closing fills after since_ms: (avg_price, contracts, fees, gross_pnl, order_ids, last_ts)."""
        data = self.client.request("GET", "/api/v1/trade/fills-history",
                                   params={"instId": pair, "begin": str(int(since_ms))}) or []
        close_side = "sell" if side == "long" else "buy"
        fills = [f for f in data if f.get("side") == close_side and int(f.get("ts") or 0) >= since_ms]
        if not fills:
            return None
        qty = sum(float(f["fillSize"]) for f in fills)
        avg = sum(float(f["fillPrice"]) * float(f["fillSize"]) for f in fills) / qty
        return {"exit": avg, "contracts": qty, "fees": sum(abs(float(f.get("fee") or 0)) for f in fills),
                "grossPnl": sum(float(f.get("fillPnl") or 0) for f in fills),
                "orderIds": sorted({str(f.get("orderId")) for f in fills}),
                "tradeIds": sorted({str(f.get("tradeId")) for f in fills}),
                "ts": max(int(f.get("ts") or 0) for f in fills)}

    def funding(self, pair, begin_ms, end_ms):
        """Funding paid(-)/received(+) in the window, or None when unavailable."""
        if end_ms - begin_ms > 7 * 86400 * 1000:
            begin_ms = end_ms - 7 * 86400 * 1000
        try:
            data = self.client.request("GET", "/api/v1/account/funding-fees", params={
                "instId": pair, "begin": str(int(begin_ms)), "end": str(int(end_ms))}) or []
        except (BlofinError, AmbiguousResult, RateLimited):
            return None
        total = 0.0
        for row in data:
            for key in ("fundingFee", "fee", "amount"):
                if row.get(key) not in (None, ""):
                    total += float(row[key])
                    break
        return total

    # ------------------------------------------------------- reconciliation
    def reconcile(self, local_positions):
        """Compare the local book with the exchange. Returns a report dict.

        local_positions: list of dicts with pair, side, contracts.
        """
        with self._lock:
            report = {"at": int(self._clock() * 1000), "ok": True, "missingOnExchange": [],
                      "unknownOnExchange": [], "sizeMismatch": [], "withoutSl": [], "pendingOrders": []}
            try:
                ex = self.positions()
                tpsl = self.active_tpsl()
                pending = self.client.request("GET", "/api/v1/trade/orders-pending") or []
            except (BlofinError, AmbiguousResult, RateLimited) as exc:
                report["ok"] = False
                report["error"] = str(exc)
                self.mark_unsafe("reconciliacion_fallida")
                self.last_reconcile, self.last_reconcile_result = report["at"], report
                return report
            local = {p["pair"]: p for p in local_positions}
            with_sl = {t.get("instId") for t in tpsl if t.get("slTriggerPrice")}
            for pair, p in local.items():
                e = ex.get(pair)
                if e is None:
                    report["missingOnExchange"].append(pair)
                    continue
                size = abs(float(e.get("positions") or 0))
                if p.get("contracts") and abs(size - float(p["contracts"])) > 1e-9:
                    report["sizeMismatch"].append({"pair": pair, "local": p["contracts"], "exchange": size})
                if pair not in with_sl:
                    report["withoutSl"].append(pair)
            for pair in ex:
                if pair not in local:
                    report["unknownOnExchange"].append(pair)
            report["pendingOrders"] = [o.get("clientOrderId") or o.get("orderId") for o in pending]
            problems = report["unknownOnExchange"] or report["sizeMismatch"] or report["pendingOrders"]
            if problems:
                report["ok"] = False
                self.mark_unsafe("discrepancia_reconciliacion")
            else:
                self.clear_unsafe("discrepancia_reconciliacion")
                self.clear_unsafe("reconciliacion_fallida")
            self.last_reconcile, self.last_reconcile_result = report["at"], report
            return report

    def resolve_pending_intents(self):
        """At startup: settle entries whose outcome is unknown before scanning."""
        unresolved = []
        for cid, rec in self.intents.pending().items():
            pair = rec.get("pair")
            if not pair:
                continue
            try:
                detail = self.order_detail(pair, cid=cid)
            except (BlofinError, AmbiguousResult, RateLimited):
                unresolved.append(cid)
                continue
            if detail is None:
                self.intents.append({"clientOrderId": cid, "status": "absent"})
            else:
                self.intents.append({"clientOrderId": cid, "status": "found_" + str(detail.get("state")),
                                     "orderId": detail.get("orderId")})
                if float(detail.get("filledSize") or 0) > 0 and rec.get("kind") == "entry":
                    unresolved.append(cid)  # a real position the local book may not know
        if unresolved:
            self.mark_unsafe("intenciones_sin_resolver")
        return unresolved
