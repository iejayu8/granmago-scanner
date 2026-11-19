"""1.7.6: demo Equity / PnL total in USDT.

Blofin's totalEquity is "The total amount of equity in USD" (docs). Up to 1.7.5
the panel showed it as the USDT Equity and took the demo initial balance from
it, so the cards did not add up. Real account read on 19/11/2026 16:56 (Madrid):
available 742.5429 + margin 255.408 + isolatedUnrealizedPnl 15.8159 =
details[USDT].equity 1013.7669 USDT, while totalEquity = 1012.7891 (USD).
"""
import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_blofin_only as base  # noqa: E402
import test_demo_server as tds  # noqa: E402

server = base.server
ACC_19_11 = {"available": 742.5429095170133, "availableEquity": 742.5429095170133, "balance": 742.5429095170133,
             "currencyIsolatedEquity": 271.2239436343499, "equity": 1013.7668531513632, "frozen": 255.408,
             "isolatedEquity": 270.96234814071454, "isolatedUnrealizedPnl": 15.815943634349898, "orderFrozen": 1e-18,
             "positionsMargin": 255.408, "totalEquity": 1012.7890750214987, "ts": "1795103779579"}
SOL = dict(base.position(pair="SOL-USDT"), side="short", entry=75.12, price=73.6, contracts=10.2, contractValue=1.0,
           notional=766.224, unrealized=15.5, priceStale=False, entryFee=0.4597344, exchange="blofin-demo",
           exchangeMark={"markPrice": 73.59566321216667, "unrealizedPnl": 15.548235235899966, "ageSeconds": 0.7})
HIST = [dict(pair="ENA-USDT", pnl=8.4238, exchange="blofin-demo"), dict(pair="ADA-USDT", pnl=0.8688, exchange="blofin-demo"),
        dict(pair="SOL-USDT", pnl=-10.4076, exchange="blofin-demo")]


def snap(initial):
    return {"tradingMode": "demo", "ledgerMode": "demo", "initialBalance": initial, "openPositions": [copy.deepcopy(SOL)],
            "exchangeAccount": dict(ACC_19_11, ageSeconds=0.4), "history": HIST}


class RealAccountTest(unittest.TestCase):
    def test_equity_is_usdt_and_cards_add_up(self):
        d = server._display_figures(snap(999.416))
        self.assertEqual(d["equity"], 1013.77)  # details[USDT].equity; 1.7.5 showed 1012.79 (USD)
        self.assertAlmostEqual(ACC_19_11["available"] + ACC_19_11["positionsMargin"] + ACC_19_11["isolatedUnrealizedPnl"],
                               ACC_19_11["equity"], places=9)
        self.assertAlmostEqual(d["balance"] + d["marginUsed"] + ACC_19_11["isolatedUnrealizedPnl"], d["equity"], delta=0.01)
        self.assertEqual(d["unrealized"], 15.5)  # 10.2 x (75.12 - 73.60), short
        self.assertAlmostEqual(10.2 * (75.12 - 73.60), 15.504, places=9)

    def test_usd_base_is_converted_once(self):
        old = server.STATE
        try:
            server.STATE = server.default_state()
            server.STATE.update(ledgerMode="demo", initialBalance=999.416, history=copy.deepcopy(HIST))
            with server.LOCK:
                server._migrate_demo_base_usdt(ACC_19_11)
            new = server.STATE["initialBalance"]
            self.assertAlmostEqual(new, 999.416 * 1013.7668531513632 / 1012.7890750214987, places=3)  # 1000.3808
            self.assertEqual(server.STATE["history"], HIST)
            d = server._display_figures(snap(new))
            # same economic result as before (13.37 in USD), now consistently in USDT
            self.assertEqual(d["pnlTotal"], round(1013.7668531513632 - new, 2))
            self.assertAlmostEqual(d["pnlTotal"], 13.39, delta=0.01)
            for _ in range(2):  # idempotent
                with server.LOCK:
                    server._migrate_demo_base_usdt(dict(ACC_19_11, totalEquity=900.0))
                self.assertEqual(server.STATE["initialBalance"], new)
            self.assertEqual(server.STATE["migrations"].count(server.DEMO_USDT_BASE_MIGRATION), 1)
        finally:
            server.STATE = old


class DemoFlowTest(unittest.TestCase):
    http = base.BlofinOnlyTest.http
    setUp = tds.DemoServerTest.setUp
    tearDown = tds.DemoServerTest.tearDown
    demo = tds.DemoServerTest.demo
    enter = tds.DemoServerTest.enter

    def test_new_demo_base_and_pnl_in_usdt(self):
        self.demo()  # fake: details equity 1000.5 USDT, totalEquity 999.53 USD
        self.assertEqual(server.STATE["initialBalance"], 1000.5)
        d = self.client.get("/api/state").get_json()["display"]
        self.assertEqual((d["equity"], d["pnlTotal"]), (1000.5, 0.0))
        self.enter()
        self.fake.prices[base.PAIR] = 620.0
        server.trading_cycle(force_reconcile=True)
        st = self.client.get("/api/state").get_json()
        acc, d = st["exchangeAccount"], st["display"]
        mark = st["openPositions"][0]["exchangeMark"]
        self.assertEqual(d["equity"], round(acc["equity"], 2))
        self.assertEqual(d["pnlTotal"], round(acc["equity"] - 1000.5, 2))
        self.assertAlmostEqual(d["pnlTotal"], mark["unrealizedPnl"], delta=0.01)  # nothing realized yet
        self.assertAlmostEqual(d["balance"] + d["marginUsed"] + mark["unrealizedPnl"], d["equity"], delta=0.02)

    def test_1_7_5_usd_base_migrates_on_first_balance(self):
        self.demo()
        server.STATE["initialBalance"] = 999.53  # what 1.7.5 stored (totalEquity, USD)
        server.STATE["migrations"].remove(server.DEMO_USDT_BASE_MIGRATION)
        server.trading_cycle(force_reconcile=True)
        self.assertAlmostEqual(server.STATE["initialBalance"], 1000.5, places=2)
        self.assertEqual(self.client.get("/api/state").get_json()["display"]["pnlTotal"], 0.0)

    def test_base_taken_with_open_position_excludes_its_open_pnl(self):
        self.demo()
        self.enter()
        self.fake.prices[base.PAIR] = 620.0
        server.trading_cycle(force_reconcile=True)
        server.STATE["initialBalance"] = None  # e.g. demo book reset while a position is open
        server.trading_cycle(force_reconcile=True)
        self.assertAlmostEqual(server.STATE["initialBalance"], 1000.5, places=2)


if __name__ == "__main__":
    unittest.main()
