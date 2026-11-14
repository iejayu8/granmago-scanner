"""In-memory fake of the Blofin private REST API (demo) for tests.

Verifies every request signature exactly as docs.blofin.com "Signature"
describes, rejects repeated nonces, and simulates fills, attached TP/SL,
position TPSL orders, closes, fills history and funding. No network.
"""
import base64
import hashlib
import hmac
import itertools
import json
import time
from urllib.parse import parse_qs, urlsplit

import blofin_trading as bt

KEY, SECRET, PASSPHRASE = "demo-key-ABCD1234", "demo-secret-XYZ98765", "demo-pass-QWERTY55"


def reference_sign(secret, path, method, ts, nonce, body):
    # Literal transcription of the docs example (hexdigest -> bytes -> base64).
    prehash = f"{path}{method}{ts}{nonce}{body}"
    hex_signature = hmac.new(secret.encode(), prehash.encode(), hashlib.sha256).hexdigest().encode()
    return base64.b64encode(hex_signature).decode()


class FakeBlofin:
    def __init__(self, prices=None, position_mode="long_short_mode"):
        self.prices = dict(prices or {"XMR-USDT": 613.76})
        self.position_mode = position_mode
        self.margin_mode = "cross"
        self.leverage = {}
        self.orders = {}          # orderId -> order
        self.positions = {}       # instId -> {"positions": signed contracts, "averagePrice": p}
        self.tpsl = []
        self.fills = []
        self.calls = []
        self.nonces = set()
        self.ids = itertools.count(1000)
        self.read_only = 0
        # failure knobs
        self.timeout_after_order = False
        self.timeout_before_order = False
        self.ignore_attached = False
        self.tpsl_error = False
        self.fee_rate = 0.0006
        self.funding_fee = "-0.05"

    # ------------------------------------------------------------ transport
    def __call__(self, method, url, headers, data, timeout):
        parts = urlsplit(url)
        assert parts.scheme + "://" + parts.netloc == bt.DEMO_BASE, url
        path = parts.path + ("?" + parts.query if parts.query else "")
        body = data or ""
        assert headers["ACCESS-KEY"] == KEY and headers["ACCESS-PASSPHRASE"] == PASSPHRASE
        expected = reference_sign(SECRET, path, method, headers["ACCESS-TIMESTAMP"], headers["ACCESS-NONCE"], body)
        if headers["ACCESS-SIGN"] != expected:
            return 200, {"code": "152409", "msg": "Signature verification failed"}, None
        if headers["ACCESS-NONCE"] in self.nonces:
            return 200, {"code": "152407", "msg": "Repeated nonce"}, None
        self.nonces.add(headers["ACCESS-NONCE"])
        params = {k: v[0] for k, v in parse_qs(parts.query).items()}
        payload = json.loads(body) if body else None
        self.calls.append((method, parts.path, params, payload))
        handler = getattr(self, "h_" + method.lower() + parts.path.replace("/api/v1/", "_").replace("/", "_").replace("-", "_"))
        out = handler(params, payload)
        return 200, out, time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime())

    def posts(self, path):
        return [c for c in self.calls if c[0] == "POST" and c[1] == path]

    @staticmethod
    def ok(data):
        return {"code": "0", "msg": "success", "data": data}

    # ------------------------------------------------------------- account
    def h_get_user_query_apikey(self, p, b):
        return self.ok({"apiKey": KEY, "readOnly": self.read_only, "ips": ["203.0.113.7"], "type": 1})

    def h_get_account_position_mode(self, p, b):
        return self.ok({"positionMode": self.position_mode, "multiPosition": "false"})

    def h_post_account_set_position_mode(self, p, b):
        self.position_mode = b["positionMode"]
        return self.ok(b)

    def h_post_account_set_margin_mode(self, p, b):
        self.margin_mode = b["marginMode"]
        return self.ok(b)

    def h_post_account_set_leverage(self, p, b):
        assert b["marginMode"] == "isolated"
        self.leverage[b["instId"]] = b["leverage"]
        return self.ok(b)

    def h_get_account_balance(self, p, b):
        return self.ok({"totalEquity": "1000.5"})

    def h_get_account_positions(self, p, b):
        return self.ok([dict(instId=i, positionSide="net", marginMode="isolated", positions=str(v["positions"]),
                             averagePrice=str(v["averagePrice"])) for i, v in self.positions.items()
                        if v["positions"] != 0])

    def h_get_account_funding_fees(self, p, b):
        return self.ok([{"instId": p.get("instId"), "fundingFee": self.funding_fee, "ts": str(int(time.time() * 1000))}])

    # --------------------------------------------------------------- orders
    def _fill(self, inst, side, size, order_id, closing=False):
        price = self.prices[inst]
        fee = price * size * 1 * self.fee_rate
        pnl = 0.0
        pos = self.positions.get(inst)
        if closing and pos:
            direction = 1 if pos["positions"] > 0 else -1
            pnl = (price - pos["averagePrice"]) * direction * size * self._cv(inst)
        self.fills.append({"instId": inst, "tradeId": str(next(self.ids)), "orderId": order_id, "fillPrice": str(price),
                           "fillSize": str(size), "fillPnl": str(pnl), "side": side, "fee": str(-fee),
                           "ts": str(int(time.time() * 1000) + 1)})
        return price, fee

    def _cv(self, inst):
        return 0.01 if inst == "XMR-USDT" else 1.0

    def h_post_trade_order(self, p, b):
        if self.timeout_before_order:
            raise bt.AmbiguousResult("ReadTimeout (before)")
        cid = b.get("clientOrderId")
        if cid and any(o["clientOrderId"] == cid for o in self.orders.values()):
            return {"code": "0", "data": [{"orderId": "", "clientOrderId": cid, "code": "102002",
                                           "msg": "Duplicate customized order ID"}]}
        assert b["marginMode"] == "isolated" and b["positionSide"] == "net" and b["orderType"] == "market"
        assert self.position_mode == "net_mode"
        oid = str(next(self.ids))
        size = float(b["size"])
        price, fee = self._fill(b["instId"], b["side"], size, oid)
        signed = size if b["side"] == "buy" else -size
        self.positions[b["instId"]] = {"positions": signed, "averagePrice": price}
        self.orders[oid] = dict(orderId=oid, clientOrderId=cid, instId=b["instId"], state="filled",
                                filledSize=str(size), averagePrice=str(price), fee=str(-fee), side=b["side"],
                                createTime=str(int(time.time() * 1000)), updateTime=str(int(time.time() * 1000)))
        if b.get("slTriggerPrice") and not self.ignore_attached:
            self.tpsl.append(dict(tpslId=str(next(self.ids)), instId=b["instId"], state="live",
                                  slTriggerPrice=b["slTriggerPrice"], tpTriggerPrice=b.get("tpTriggerPrice"),
                                  size="-1", clientOrderId=""))
        if self.timeout_after_order:
            raise bt.AmbiguousResult("ReadTimeout (after)")
        return self.ok([{"orderId": oid, "clientOrderId": cid, "code": "0", "msg": "Order placed"}])

    def h_get_trade_order_detail(self, p, b):
        for o in self.orders.values():
            if (p.get("orderId") and o["orderId"] == p["orderId"]) or (
                    not p.get("orderId") and p.get("clientOrderId") and o["clientOrderId"] == p["clientOrderId"]):
                return self.ok(dict(o))
        return {"code": "404", "msg": "not found"}

    def h_get_trade_orders_tpsl_pending(self, p, b):
        return self.ok([dict(t) for t in self.tpsl if t["state"] == "live"
                        and (not p.get("instId") or t["instId"] == p["instId"])])

    def h_post_trade_order_tpsl(self, p, b):
        if self.tpsl_error:
            return {"code": "0", "data": {"tpslId": None, "code": "102038", "msg": "SL trigger price should be lower"}}
        assert b["size"] == "-1" and b["reduceOnly"] == "true"
        tid = str(next(self.ids))
        self.tpsl.append(dict(tpslId=tid, instId=b["instId"], state="live", slTriggerPrice=b.get("slTriggerPrice"),
                              tpTriggerPrice=b.get("tpTriggerPrice"), size="-1", clientOrderId=b.get("clientOrderId")))
        return self.ok({"tpslId": tid, "clientOrderId": b.get("clientOrderId"), "code": "0", "msg": None})

    def h_post_trade_cancel_tpsl(self, p, b):
        ids = {x["tpslId"] for x in b}
        for t in self.tpsl:
            if t["tpslId"] in ids:
                t["state"] = "canceled"
        return self.ok([{"tpslId": i, "code": "0"} for i in ids])

    def h_post_trade_close_position(self, p, b):
        pos = self.positions.get(b["instId"])
        if not pos or pos["positions"] == 0:
            return {"code": "102005", "msg": "Position had been closed"}
        oid = str(next(self.ids))
        side = "sell" if pos["positions"] > 0 else "buy"
        self._fill(b["instId"], side, abs(pos["positions"]), oid, closing=True)
        pos["positions"] = 0
        return self.ok({"instId": b["instId"], "positionSide": "net", "clientOrderId": b.get("clientOrderId")})

    def h_get_trade_orders_pending(self, p, b):
        return self.ok([])

    def h_get_trade_fills_history(self, p, b):
        since = int(p.get("begin") or 0)
        return self.ok([f for f in self.fills if f["instId"] == p.get("instId") and int(f["ts"]) >= since])

    # -------------------------------------------------------------- helpers
    def trigger_stop(self, inst):
        """Exchange-side SL fires: close at the SL trigger price."""
        t = next(t for t in self.tpsl if t["instId"] == inst and t["state"] == "live")
        self.prices[inst] = float(t["slTriggerPrice"])
        pos = self.positions[inst]
        self._fill(inst, "sell" if pos["positions"] > 0 else "buy", abs(pos["positions"]), "sl" + t["tpslId"], closing=True)
        pos["positions"] = 0
        t["state"] = "effective"
