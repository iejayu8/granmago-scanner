"""Blofin demo responses: literal docs.blofin.com examples and malformed variants.

Every parser must either read the right number or fail safe (None, refuse,
safe state). None of them may raise an unexpected exception or turn an
unreadable value into "no position" / a 0 price / NaN.
"""
import json
import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_blofin_trading import TraderCase, bt, plan  # noqa: E402

DOCS_BALANCE = {"ts": "1697021343571", "totalEquity": "10011254.077985990315787910",
                "isolatedEquity": "861.763132108800000000",
                "details": [{"currency": "USDT", "equity": "10014042.988958415234430699548",
                             "balance": "10013119.885958415234430699", "ts": "1697021343571",
                             "isolatedEquity": "862.003200000000000000048", "available": "9996399.4708691159703362725",
                             "availableEquity": "9996399.4708691159703362725", "frozen": "15805.149672632160010000",
                             "orderFrozen": "14920.994472632160010000", "equityUsd": "10011254.077985990315787910",
                             "isolatedUnrealizedPnl": "-22.151999999999999999952", "bonus": "0"}]}
DOCS_POSITIONS = [
    {"positionId": "7982", "instId": "ETH-USDT", "instType": "SWAP", "marginMode": "isolated", "positionSide": "net",
     "adl": "5", "positions": "1", "availablePositions": "1", "averagePrice": "1591.800000000000000000",
     "margin": "53.060000000000000000", "markPrice": "1591.69", "marginRatio": "72.453752328329172684",
     "liquidationPrice": "1066.104078762306610407", "unrealizedPnl": "-0.011",
     "unrealizedPnlRatio": "-0.000207312476441764", "maintenanceMargin": "0.636676",
     "createTime": "1695352782370", "updateTime": "1695352782372", "leverage": "3"},
    {"positionId": "5483", "instId": "BCH-USDT", "instType": "SWAP", "marginMode": "cross", "positionSide": "net",
     "adl": "5", "positions": "-290", "availablePositions": "-290", "averagePrice": "1890.000000000000000000",
     "markPrice": "1889", "marginRatio": "172426.851188979265788635",
     "liquidationPrice": "3418227.599542259480103079", "unrealizedPnl": "2.9",
     "unrealizedPnlRatio": "0.001587301587301587", "initialMargin": "1826.033333333333333333",
     "maintenanceMargin": "54.781", "createTime": "1695090016933", "updateTime": "1695090016931",
     "leverage": "3"}]
DOCS_TPSL = [{"tpslId": "2411", "instId": "ETH-USDT", "marginMode": "cross", "positionSide": "net", "side": "sell",
              "tpTriggerPrice": "1666.000000000000000000", "tpOrderPrice": None, "tpTriggerPriceType": "last",
              "slTriggerPrice": "1222.000000000000000000", "slOrderPrice": None, "slTriggerPriceType": "last",
              "size": "1", "state": "live", "leverage": "3", "reduceOnly": "false", "actualSize": None,
              "clientOrderId": "aabbc", "createTime": "1697016700775", "brokerId": ""}]
DOCS_FILLS = [{"instId": "ETH-USDT", "tradeId": "7772187", "orderId": "28697026", "fillPrice": "1587.800000000000000000",
               "fillSize": "2.000000000000000000", "fillPnl": "0.000000000000000000", "positionSide": "long",
               "side": "sell", "fee": "0.190536000000000000", "ts": "1696853354238", "brokerId": ""},
              {"instId": "ETH-USDT", "tradeId": "7772186", "orderId": "28697025", "fillPrice": "1587.800000000000000000",
               "fillSize": "1.000000000000000000", "fillPnl": "0.000000000000000000", "positionSide": "short",
               "side": "sell", "fee": "0.095268000000000000", "ts": "1696853354224", "brokerId": ""}]
DOCS_FUNDING = [{"billId": "1009299785757124571", "instId": "BTC-USDT", "currency": "USDT", "fundingFee": "0.0001",
                 "fundingTime": "1703462400000", "marginMode": "cross", "positionSize": "100",
                 "ts": "1703462401000", "fundingKind": "0"}]
DOCS_ORDER_UNFILLED = {"orderId": "29531103", "clientOrderId": "", "instId": "ETH-USDT", "state": "live",
                       "filledSize": "0.000000000000000000", "averagePrice": "0.000000000000000000",
                       "fee": "0.000000000000000000", "createTime": "1697031292762", "updateTime": "1697031292788"}


def ok(data):
    return lambda p, b: {"code": "0", "msg": "success", "data": data}


class DocsExamplesTest(TraderCase):
    def test_balance(self):
        self.fake.h_get_account_balance = ok(DOCS_BALANCE)
        acc = self.trader.account()
        self.assertAlmostEqual(acc["totalEquity"], 10011254.077985990315787910)
        self.assertAlmostEqual(acc["equity"], 10014042.988958415)
        self.assertAlmostEqual(acc["balance"], 10013119.885958415)
        self.assertAlmostEqual(acc["available"], 9996399.470869116)
        self.assertAlmostEqual(acc["isolatedUnrealizedPnl"], -22.152)
        self.assertEqual(acc["ts"], "1697021343571")

    def test_positions_and_marks(self):
        self.fake.h_get_account_positions = ok(DOCS_POSITIONS)
        pos = self.trader.positions()
        self.assertEqual(sorted(pos), ["BCH-USDT", "ETH-USDT"])
        self.assertEqual(bt.num(pos["BCH-USDT"]["positions"]), -290)
        self.assertEqual(bt.num(pos["ETH-USDT"]["markPrice"]), 1591.69)

    def test_tpsl_pending_counts_as_protection(self):
        self.fake.h_get_trade_orders_tpsl_pending = ok(DOCS_TPSL)
        self.fake.h_get_account_positions = ok(DOCS_POSITIONS[:1])
        self.fake.h_get_trade_orders_pending = ok([])
        report = self.trader.reconcile([{"pair": "ETH-USDT", "side": "long", "contracts": 1}])
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["withoutSl"], [])

    def test_fills_positive_fee_and_funding(self):
        self.fake.h_get_trade_fills_history = ok(DOCS_FILLS)
        out = self.trader.exit_fills("ETH-USDT", "long", 1696853354000)
        self.assertEqual(out["contracts"], 3)
        self.assertAlmostEqual(out["exit"], 1587.8)
        self.assertAlmostEqual(out["fees"], 0.285804)
        self.assertEqual(out["ts"], 1696853354238)
        self.fake.h_get_account_funding_fees = ok(DOCS_FUNDING)
        self.assertAlmostEqual(self.trader.funding("BTC-USDT", 0, 1703462402000), 0.0001)

    def test_unfilled_order_detail_never_books_a_zero_entry(self):
        self.trader.prepare_account()
        real = self.fake.h_get_trade_order_detail

        def detail(p, b):
            out = real(p, b)
            out["data"]["averagePrice"] = "0.000000000000000000"  # docs value before the fill
            return out
        self.fake.h_get_trade_order_detail = detail
        fill = self.trader.open_position(plan())
        self.assertIsNotNone(fill)
        self.assertGreater(fill["entry"], 600)


class FreeBalanceAndMarginTest(TraderCase):
    """1.7.3: 'Saldo libre' = details.available, 'Margen en uso' = positions' margin."""

    def display(self, acc, positions=()):
        import test_demo_server as tds
        snap = {"tradingMode": "demo", "initialBalance": 10000000.0,
                "openPositions": [dict(p, priceStale=False, unrealized=0.0) for p in positions],
                "exchangeAccount": dict(acc, ageSeconds=1.0)}
        return tds.server._display_figures(snap)

    def test_docs_literal_balance_and_positions(self):
        self.fake.h_get_account_balance = ok(DOCS_BALANCE)
        self.fake.h_get_account_positions = ok(DOCS_POSITIONS)
        acc = self.trader.account()
        self.assertAlmostEqual(acc["available"], 9996399.4708691159703362725)
        self.assertAlmostEqual(acc["currencyIsolatedEquity"], 862.0032)
        self.assertAlmostEqual(acc["orderFrozen"], 14920.99447263216)
        self.assertAlmostEqual(acc["frozen"], 15805.14967263216)
        margin = self.trader.positions_margin(self.trader.positions())
        self.assertAlmostEqual(margin, 53.06 + 1826.033333333333)  # isolated `margin` + cross `initialMargin`
        d = self.display(dict(acc, positionsMargin=margin), [{"pair": "ETH-USDT"}, {"pair": "BCH-USDT"}])
        self.assertEqual(d["balance"], 9996399.47)
        self.assertEqual(d["balanceSource"], "available")
        self.assertEqual(d["marginUsed"], 1879.09)
        self.assertEqual(d["marginSource"], "positions")
        self.assertEqual(d["equity"], 10014042.99)  # 1.7.6: details[USDT].equity; totalEquity is USD

    def test_fallbacks(self):
        self.fake.h_get_account_balance = ok(DOCS_BALANCE)
        acc = self.trader.account()
        # positions unreadable/absent -> isolatedEquity - isolatedUnrealizedPnl
        self.assertIsNone(self.trader.positions_margin({"X": {"margin": "abc"}}))
        d = self.display(dict(acc, positionsMargin=None), [{"pair": "ETH-USDT"}])
        self.assertEqual(d["marginSource"], "isolatedEquity")
        self.assertEqual(d["marginUsed"], round(862.0032 + 22.152, 2))
        # no available -> cash balance; nothing at all -> None, never NaN
        d = self.display(dict(acc, available=None, positionsMargin=0.0))
        self.assertEqual((d["balance"], d["balanceSource"], d["marginUsed"]), (10013119.89, "balance", 0.0))
        d = self.display({"totalEquity": 5.0, "available": None, "balance": None}, [{"pair": "ETH-USDT"}])
        self.assertIsNone(d["balance"])
        self.assertIsNone(d["marginUsed"])
        self.assertEqual(self.trader.positions_margin({}), 0.0)


class MalformedTest(TraderCase):
    def test_balance_variants(self):
        for data in ({}, [], [DOCS_BALANCE], {"details": "x"}, {"totalEquity": "abc", "details": [None, 3]},
                     {"totalEquity": "NaN"}, {"totalEquity": "inf"}, None, "garbage",
                     {"totalEquity": 1000.5, "details": [{"currency": "USDT", "balance": 999}]}):
            self.fake.h_get_account_balance = ok(data)
            acc = self.trader.account()
            for k, v in acc.items():
                if isinstance(v, float):
                    self.assertTrue(math.isfinite(v), (data, k, v))
            json.dumps(acc, allow_nan=False)
        self.fake.h_get_account_balance = ok([DOCS_BALANCE])
        self.assertAlmostEqual(self.trader.balance(), 10014042.988958415234430699548)

    def test_unreadable_position_is_never_no_position(self):
        self.fake.h_get_trade_orders_pending = ok([])
        for bad in ({"instId": "ETH-USDT", "positions": "abc"}, {"positions": "3"}):
            self.fake.h_get_account_positions = ok([bad])
            report = self.trader.reconcile([])
            self.assertFalse(report["ok"], bad)
            self.assertFalse(self.trader.safe)
            self.trader.clear_unsafe("reconciliacion_fallida")
        # empty / zero / missing size: no position
        for data in ([], None, [{"instId": "ETH-USDT", "positions": "0"}], [{"instId": "ETH-USDT", "positions": ""}]):
            self.fake.h_get_account_positions = ok(data)
            self.assertEqual(self.trader.positions(), {}, data)

    def test_fills_variants(self):
        bad = [dict(DOCS_FILLS[0], fillSize="0"), dict(DOCS_FILLS[0], fillPrice=None), dict(DOCS_FILLS[0], ts="x"),
               {"side": "sell"}, None, dict(DOCS_FILLS[0], fee=None, fillPnl="")]
        self.fake.h_get_trade_fills_history = ok(bad)
        out = self.trader.exit_fills("ETH-USDT", "long", 1)  # unreadable ts never counts
        self.assertEqual(out["contracts"], 2)
        self.assertEqual(out["fees"], 0)
        self.fake.h_get_trade_fills_history = ok([dict(DOCS_FILLS[0], fillSize="0")])
        self.assertIsNone(self.trader.exit_fills("ETH-USDT", "long", 0))  # was ZeroDivisionError
        self.fake.h_get_trade_fills_history = ok(None)
        self.assertIsNone(self.trader.exit_fills("ETH-USDT", "long", 0))
        # negative fee (older answers / fake) is still a cost
        self.fake.h_get_trade_fills_history = ok([dict(DOCS_FILLS[0], fee="-0.19")])
        self.assertAlmostEqual(self.trader.exit_fills("ETH-USDT", "long", 0)["fees"], 0.19)

    def test_funding_variants(self):
        # 1.8.2: one unreadable bill makes the total unknown (None -> funding_status
        # 'unavailable'); 1.7.2 skipped it and booked a wrong total as exchange data.
        for data, want in (([], 0.0), (None, 0.0), ([{"fundingFee": "abc"}, {"fee": "-0.5"}], None),
                           ([None, {"amount": "0.2"}, {"fundingFee": ""}], None),
                           ([{"fee": "-0.5"}, {"amount": "0.2"}], -0.3), ({"fundingFee": "-0.1"}, None)):
            self.fake.h_get_account_funding_fees = ok(data)
            got = self.trader.funding("ETH-USDT", 0, 1)
            if want is None:
                self.assertIsNone(got, data)
            else:
                self.assertAlmostEqual(got, want)

    def test_num(self):
        for v, want in (("1.5", 1.5), (2, 2.0), ("", None), (None, None), ("NaN", None), ("-inf", None),
                        ("abc", None), ([], None), ("-0.000", 0.0)):
            self.assertEqual(bt.num(v), want, v)


class ServerMarksTest(unittest.TestCase):
    """Bad Blofin marks never reach the panel; NaN never breaks /api/state."""

    def test_nan_in_state_is_valid_json(self):
        import test_demo_server as tds
        server = tds.server
        old = server.STATE
        try:
            server.STATE = server.default_state()
            server.STATE["balance"] = float("nan")
            server.STATE["openPositions"] = [dict(tds.base.position(), sl=float("inf"), priceStale=False)]
            body = server.app.test_client().get("/api/state").get_data(as_text=True)
            bad = []
            json.loads(body, parse_constant=bad.append)
            self.assertEqual(bad, [], "JSON inválido para el panel")
            self.assertIsNone(json.loads(body)["balance"])
        finally:
            server.STATE = old


if __name__ == "__main__":
    unittest.main()
