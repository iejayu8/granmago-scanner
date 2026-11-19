"""1.7.4 (pasada de calidad 17/11/2026): demo specs, blocked demo book, non-finite inputs."""
import math
import sys
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_blofin_only as base  # noqa: E402
import test_demo_server as ds  # noqa: E402

server = base.server
PAIR = base.PAIR
INDEX = Path(__file__).resolve().parents[1] / "granmago" / "static" / "index.html"


def multiple(value, step):
    q = Decimal(str(value)) / Decimal(step)
    return q == q.to_integral_value()


class QA174Test(unittest.TestCase):
    http = base.BlofinOnlyTest.http

    def setUp(self):
        ds.DemoServerTest.setUp(self)
        real = self.http
        # Live and demo publish different specs for the same instrument
        # (17/11/2026: XRP lot 0.01 live / 0.1 demo, PEPE tick 10x on demo...).
        self.demo_spec = dict(lotSize="10", minSize="10", tickSize="0.1", maxMarketSize="40")

        def http(session, method, url, **kw):
            if url.startswith(server.DEMO_MARKET) and "instId=" in url:
                pair = url.split("instId=")[1].split("&")[0]
                meta = dict(ds.xmr_meta(base.instrument(pair)), **self.demo_spec)
                self.urls.append(url)
                return base.response(dict(code="0", data=[meta]))
            return real(session, method, url, **kw)
        self.http = http

    def tearDown(self):
        ds.DemoServerTest.tearDown(self)

    demo = ds.DemoServerTest.demo
    enter = ds.DemoServerTest.enter

    # ------------------------------------------- bug 1: demo order specs
    def test_demo_order_uses_demo_lot_tick_and_max_size(self):
        self.demo()
        self.enter()
        order = self.fake.posts("/api/v1/trade/order")[0][3]
        self.assertTrue(multiple(order["size"], "10"), order["size"])
        self.assertLessEqual(float(order["size"]), 40)
        self.assertTrue(multiple(order["slTriggerPrice"], "0.1"), order["slTriggerPrice"])
        self.assertTrue(multiple(order["tpTriggerPrice"], "0.1"), order["tpTriggerPrice"])
        self.assertTrue(any(u.startswith(server.DEMO_MARKET + "instruments?instId=" + PAIR) for u in self.urls))
        pos = server.STATE["openPositions"][0]
        self.assertEqual(pos["sl"], 598.2)  # long SL rounded toward the entry on the demo tick

    def test_demo_stop_move_uses_demo_tick(self):
        self.demo()
        self.enter()
        pos = server.STATE["openPositions"][0]
        pos.update(sl=605.123, slDirty=True)
        server.trading_cycle()
        live = [t for t in self.fake.tpsl if t["state"] == "live"]
        self.assertEqual(len(live), 1)
        self.assertTrue(multiple(live[0]["slTriggerPrice"], "0.1"), live[0]["slTriggerPrice"])

    def test_pair_missing_on_demo_is_refused_without_order(self):
        self.demo()
        real = self.http

        def http(session, method, url, **kw):
            if url.startswith(server.DEMO_MARKET) and "instId=" in url:
                return base.response(dict(code="0", data=[]))
            return real(session, method, url, **kw)
        self.http = http
        self.assertIsNone(server.demo_instrument_meta(PAIR, True))
        self.enter()
        self.assertEqual(self.fake.posts("/api/v1/trade/order"), [])

    # ------------------------------- bug 2: blocked mode with the demo book
    def blocked_with_demo_position(self):
        self.demo()
        self.enter()
        self.fake.prices[PAIR] = 620.0
        server.trading_cycle(force_reconcile=True)
        pos = server.STATE["openPositions"][0]
        pos["unrealized"] = 3.0
        acc = dict(server.STATE["exchangeAccount"])
        self.assertEqual(server.init_trading({"TRADING_MODE": "demo"}), "blocked")  # keys removed
        self.assertEqual(server.STATE["ledgerMode"], "demo")
        return acc

    def test_blocked_demo_book_equity_is_not_counted_twice(self):
        acc = self.blocked_with_demo_position()
        d = self.client.get("/api/state").get_json()["display"]
        self.assertEqual(d["mode"], "blocked")
        self.assertEqual(d["equitySource"], "blofin")
        self.assertEqual(d["equity"], round(acc["equity"], 2))
        self.assertEqual(d["balance"], round(acc["available"], 2))

    def test_blocked_demo_book_close_is_refused_not_queued(self):
        self.blocked_with_demo_position()
        r = self.client.post("/api/close_position", json={"pair": PAIR})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.get_json()["error"], "trading_bloqueado")
        out = server.kill_switch(True, close_all=True)
        self.assertEqual(out["closeRequested"], [])
        self.assertFalse(server.STATE["openPositions"][0].get("closeRequested"))

    # ------------------------------------ bug 3: non-finite money inputs
    def test_reset_and_start_reject_non_finite_balance(self):
        server.STATE.update(history=[], openPositions=[], balance=1000.0, initialBalance=1000.0)
        for raw in (b'{"initialBalance": NaN}', b'{"initialBalance": Infinity}'):
            r = self.client.post("/api/reset", data=raw, content_type="application/json")
            self.assertEqual(r.status_code, 200)
            self.assertNotIn(b"NaN", r.data)
            self.assertTrue(math.isfinite(server.STATE["balance"]), raw)
            r = self.client.post("/api/start", data=raw, content_type="application/json")
            self.assertTrue(math.isfinite(server.STATE["balance"]), raw)
            self.assertTrue(math.isfinite(server.STATE["initialBalance"]), raw)

    def test_control_responses_are_valid_json(self):
        server.STATE["history"] = [dict(pair=PAIR, side="long", pnl=float("nan"), tradeId=1)]
        for path in ("/api/start", "/api/stop", "/api/settings"):
            r = self.client.post(path, json={})
            self.assertNotIn(b"NaN", r.data, path)
            self.assertIsNone(r.get_json()["history"][0]["pnl"], path)

    def test_panel_post_render_error_is_not_a_connection_error(self):
        html = INDEX.read_text(encoding="utf-8")
        body = html[html.index("async function post("):html.index('$("btnStart").onclick')]
        self.assertIn("Error del panel", body)
        self.assertLess(body.index("Sin conexión"), body.index("render(data)"))


if __name__ == "__main__":
    unittest.main()
