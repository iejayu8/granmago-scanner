"""Blofin Demo trading layer against an in-memory fake (no network, no keys)."""
import io
import logging
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "granmago"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import blofin_trading as bt  # noqa: E402
from fake_blofin import KEY, PASSPHRASE, SECRET, FakeBlofin, reference_sign  # noqa: E402

XMR = {"instId": "XMR-USDT", "contractValue": "0.01", "lotSize": "1", "minSize": "1", "tickSize": "0.01",
       "maxMarketSize": "10000", "state": "live", "apiPermission": "full"}
BTC = {"instId": "BTC-USDT", "contractValue": "0.001", "lotSize": "0.1", "minSize": "0.1", "tickSize": "0.1",
       "maxMarketSize": "100", "state": "live", "apiPermission": "full"}


def config():
    return bt.TradingConfig("demo", KEY, SECRET, PASSPHRASE, leverage=3)


def plan(**kw):
    base = dict(pair="XMR-USDT", side="long", notional=300.0, entry=613.76, sl=600.004, tp=650.009,
                riskAmt=7.0, confirmMs=1763100000000, closeMs=1763114400000)
    base.update(kw)
    return base


class TraderCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.fake = FakeBlofin()
        self.sleeps = []
        self.trader = bt.DemoTrader(config(), self.tmp.name, transport=self.fake,
                                    sleep=self.sleeps.append, instrument_meta=lambda inst, force=False: dict(XMR))

    def tearDown(self):
        self.tmp.cleanup()


class SignatureTest(unittest.TestCase):
    def test_matches_docs_algorithm(self):
        args = ("/api/v1/trade/order", "POST", "1597026383085", "9f1c0a7e-8c1e-4f8b-a6a1-1b2c3d4e5f60",
                '{"instId":"BTC-USDT","size":"1"}')
        self.assertEqual(bt.sign(SECRET, *args), reference_sign(SECRET, *args))
        # GET: query string is part of the path and the body is empty.
        get = ("/api/v1/trade/order-detail?instId=BTC-USDT&clientOrderId=abc", "GET", "1", "n", "")
        self.assertEqual(bt.sign(SECRET, *get), reference_sign(SECRET, *get))
        self.assertNotEqual(bt.sign(SECRET, *get), bt.sign(SECRET + "x", *get))

    def test_client_signs_every_request_and_fake_verifies(self):
        fake = FakeBlofin()
        client = bt.BlofinPrivateClient(config(), transport=fake)
        self.assertEqual(client.request("GET", "/api/v1/account/balance")["totalEquity"], "1000.5")
        client.request("GET", "/api/v1/trade/orders-tpsl-pending", params={"instId": "XMR-USDT", "x": None})
        self.assertEqual(fake.calls[-1][2], {"instId": "XMR-USDT"})
        client.request("GET", "/api/v1/trade/orders-tpsl-pending", params={"instId": None})  # no bare "?"
        bad = bt.BlofinPrivateClient(bt.TradingConfig("demo", KEY, "wrong-secret", PASSPHRASE), transport=fake)
        with self.assertRaises(bt.BlofinError) as ctx:
            bad.request("GET", "/api/v1/account/balance")
        self.assertEqual(ctx.exception.code, "152409")

    def test_demo_endpoint_and_live_block(self):
        self.assertEqual(config().base_url, "https://demo-trading-openapi.blofin.com")
        with self.assertRaises(bt.LiveTradingBlocked):
            bt.TradingConfig("live", KEY, SECRET, PASSPHRASE)
        with self.assertRaises(bt.LiveTradingBlocked):
            bt.TradingConfig.from_env({"TRADING_MODE": "live", "BLOFIN_API_KEY": KEY})
        with self.assertRaises(bt.TradingConfigError):
            bt.TradingConfig("demo", KEY, "", PASSPHRASE)
        self.assertEqual(bt.TradingConfig.from_env({}).mode, "paper")


class SizingTest(unittest.TestCase):
    def test_floor_to_lot_and_min_size(self):
        contracts, _ = bt.size_contracts(300.0, 613.76, XMR)
        self.assertEqual(contracts, 48.0)  # 300 / 6.1376 = 48.87 -> 48 (floor, never more risk)
        contracts, _ = bt.size_contracts(10.0, 95000.0, BTC)
        self.assertEqual(contracts, 0.1)  # 10/95 = 0.105 -> 0.1
        contracts, why = bt.size_contracts(5.0, 95000.0, BTC)
        self.assertFalse(contracts)
        self.assertIn("min", why.lower())
        contracts, _ = bt.size_contracts(1e9, 95000.0, BTC)
        self.assertEqual(contracts, 100.0)  # capped at maxMarketSize

    def test_bracket_rounds_to_tick_on_the_safe_side(self):
        self.assertEqual(bt.round_bracket("long", 600.004, 650.009, 0.01), (600.01, 650.0))
        self.assertEqual(bt.round_bracket("short", 650.009, 600.004, 0.01), (650.0, 600.01))
        self.assertEqual(bt.fmt_num(48.0), "48")
        self.assertEqual(bt.fmt_num(600.01), "600.01")


class ClientOrderIdTest(unittest.TestCase):
    def test_deterministic_and_valid(self):
        a = bt.client_order_id("XMR-USDT", "long", 1, 2)
        self.assertEqual(a, bt.client_order_id("XMR-USDT", "long", 1, 2))
        self.assertNotEqual(a, bt.client_order_id("XMR-USDT", "short", 1, 2))
        self.assertNotEqual(a, bt.client_order_id("XMR-USDT", "long", 1, 3))
        self.assertLessEqual(len(a), 32)
        self.assertTrue(a.isalnum())


class OpenTest(TraderCase):
    def test_open_sets_up_account_and_attaches_sl_tp(self):
        self.assertTrue(self.trader.prepare_account())
        self.assertEqual(self.fake.position_mode, "net_mode")
        self.assertEqual(self.fake.margin_mode, "isolated")
        fill = self.trader.open_position(plan())
        self.assertIsNotNone(fill)
        self.assertEqual(fill["contracts"], 48.0)
        self.assertEqual(fill["sl"], 600.01)
        self.assertEqual(self.fake.leverage["XMR-USDT"], "3")
        body = self.fake.posts("/api/v1/trade/order")[0][3]
        self.assertEqual((body["slTriggerPrice"], body["tpTriggerPrice"], body["slOrderPrice"]), ("600.01", "650", "-1"))
        self.assertEqual(body["clientOrderId"], fill["clientOrderId"])
        self.assertGreater(fill["fee"], 0)
        self.assertEqual(self.fake.posts("/api/v1/trade/order-tpsl"), [])  # attached SL found

    def test_duplicate_client_order_id_never_opens_twice(self):
        self.trader.prepare_account()
        first = self.trader.open_position(plan())
        again = self.trader.open_position(plan())
        self.assertEqual(again["orderId"], first["orderId"])
        self.assertEqual(len(self.fake.orders), 1)

    def test_timeout_after_post_queries_instead_of_resending(self):
        self.trader.prepare_account()
        self.fake.timeout_after_order = True
        fill = self.trader.open_position(plan())
        self.assertIsNotNone(fill)
        self.assertEqual(len(self.fake.posts("/api/v1/trade/order")), 1)
        lookups = [c for c in self.fake.calls if c[1] == "/api/v1/trade/order-detail"]
        self.assertEqual(lookups[0][2]["clientOrderId"], fill["clientOrderId"])
        self.assertTrue(self.trader.safe)

    def test_timeout_with_unknown_outcome_goes_safe_state(self):
        self.trader.prepare_account()
        self.fake.timeout_before_order = True
        self.assertIsNone(self.trader.open_position(plan()))
        self.assertEqual(len(self.fake.posts("/api/v1/trade/order")), 1)  # no blind resend
        self.assertFalse(self.trader.safe)
        self.assertFalse(self.trader.can_open())
        self.assertIsNone(self.trader.open_position(plan(confirmMs=5)))  # blocked while unsafe
        self.assertEqual(len(self.fake.posts("/api/v1/trade/order")), 1)
        log = (Path(self.tmp.name) / "trading_intents.jsonl").read_text()
        self.assertIn('"unknown"', log)

    def test_missing_attached_sl_places_position_tpsl(self):
        self.trader.prepare_account()
        self.fake.ignore_attached = True
        fill = self.trader.open_position(plan())
        self.assertIsNotNone(fill)
        tpsl = self.fake.posts("/api/v1/trade/order-tpsl")
        self.assertEqual(len(tpsl), 1)
        self.assertEqual(tpsl[0][3]["slTriggerPrice"], "600.01")
        self.assertEqual(fill["tpslId"], self.fake.tpsl[0]["tpslId"])

    def test_no_sl_possible_closes_the_position(self):
        self.trader.prepare_account()
        self.fake.ignore_attached = True
        self.fake.tpsl_error = True
        self.assertIsNone(self.trader.open_position(plan()))
        self.assertEqual(len(self.fake.posts("/api/v1/trade/close-position")), 1)
        self.assertEqual(self.fake.positions["XMR-USDT"]["positions"], 0)

    def test_risk_after_rounding_is_bounded(self):
        self.trader.prepare_account()
        self.assertIsNone(self.trader.open_position(plan(riskAmt=1.0)))
        self.assertEqual(self.fake.posts("/api/v1/trade/order"), [])

    def test_read_only_key_and_restricted_instrument_refuse(self):
        self.fake.read_only = 1
        self.assertFalse(self.trader.prepare_account())
        self.assertFalse(self.trader.can_open())
        other = bt.DemoTrader(config(), self.tmp.name, transport=FakeBlofin(), sleep=lambda s: None,
                              instrument_meta=lambda i, f=False: dict(XMR, apiPermission="reduce_only"))
        other.prepare_account()
        self.assertIsNone(other.open_position(plan()))


class ReconcileTest(TraderCase):
    def test_clean_book_is_ok(self):
        self.trader.prepare_account()
        fill = self.trader.open_position(plan())
        report = self.trader.reconcile([{"pair": "XMR-USDT", "side": "long", "contracts": fill["contracts"]}])
        self.assertTrue(report["ok"], report)

    def test_orphan_and_size_mismatch_go_safe(self):
        self.trader.prepare_account()
        self.trader.open_position(plan())
        report = self.trader.reconcile([])
        self.assertEqual(report["unknownOnExchange"], ["XMR-USDT"])
        self.assertFalse(self.trader.can_open())
        report = self.trader.reconcile([{"pair": "XMR-USDT", "side": "long", "contracts": 47}])
        self.assertTrue(report["sizeMismatch"])
        self.assertFalse(self.trader.safe)

    def test_stop_hit_on_exchange_is_reported_missing_with_fills(self):
        self.trader.prepare_account()
        fill = self.trader.open_position(plan())
        self.fake.trigger_stop("XMR-USDT")
        report = self.trader.reconcile([{"pair": "XMR-USDT", "side": "long", "contracts": 48}])
        self.assertEqual(report["missingOnExchange"], ["XMR-USDT"])
        exits = self.trader.exit_fills("XMR-USDT", "long", fill["openedMs"])
        self.assertAlmostEqual(exits["exit"], 600.01)
        self.assertLess(exits["grossPnl"], 0)
        self.assertTrue(exits["tradeIds"])
        self.assertAlmostEqual(self.trader.funding("XMR-USDT", 0, 1), -0.05)

    def test_without_sl_detected(self):
        self.trader.prepare_account()
        self.trader.open_position(plan())
        self.trader.cancel_tpsl("XMR-USDT")
        report = self.trader.reconcile([{"pair": "XMR-USDT", "side": "long", "contracts": 48}])
        self.assertEqual(report["withoutSl"], ["XMR-USDT"])

    def test_pending_unknown_intent_blocks_at_startup(self):
        self.trader.prepare_account()
        self.trader.open_position(plan())
        fresh = bt.DemoTrader(config(), self.tmp.name, transport=self.fake, sleep=lambda s: None,
                              instrument_meta=lambda i, f=False: dict(XMR))
        self.assertEqual(fresh.resolve_pending_intents(), [])  # all filled & recorded
        fresh.intents.append({"clientOrderId": "gmeorphan", "kind": "entry", "status": "pending", "pair": "XMR-USDT"})
        self.fake.timeout_before_order = False
        unresolved = fresh.resolve_pending_intents()
        self.assertEqual(unresolved, [])  # not on the exchange -> absent
        self.assertIn('"absent"', (Path(self.tmp.name) / "trading_intents.jsonl").read_text())


class CloseTest(TraderCase):
    def test_close_and_replace_stop(self):
        self.trader.prepare_account()
        fill = self.trader.open_position(plan())
        new_id, sl = self.trader.replace_stop("XMR-USDT", "long", 613.761, fill["tp"], fill["tpslId"], fill["clientOrderId"], 0.01)
        live = [t for t in self.fake.tpsl if t["state"] == "live"]
        self.assertEqual([t["tpslId"] for t in live], [new_id])
        self.assertEqual(sl, 613.77)
        self.assertTrue(self.trader.close_position("XMR-USDT", "manual", fill["clientOrderId"]))
        self.assertEqual([t for t in self.fake.tpsl if t["state"] == "live"], [])
        # Closing again is harmless (102005 accepted).
        self.assertTrue(self.trader.close_position("XMR-USDT", "manual", fill["clientOrderId"]))


class RateLimitTest(unittest.TestCase):
    def test_sliding_window(self):
        now = [0.0]
        slept = []

        def sleep(s):
            slept.append(s)
            now[0] += s

        lim = bt.SlidingWindowLimiter(25, 10, clock=lambda: now[0], sleep=sleep)
        for _ in range(25):
            lim.acquire()
        self.assertEqual(slept, [])
        lim.acquire()  # 26th waits for the window
        self.assertTrue(slept and 9.9 < slept[0] <= 10.1)
        with self.assertRaises(ValueError):
            bt.SlidingWindowLimiter(31, 10)
        tight = bt.SlidingWindowLimiter(1, 10, clock=lambda: now[0], sleep=sleep)
        tight.acquire()
        with self.assertRaises(bt.RateLimited):
            tight.acquire(max_wait=1)

    def test_client_respects_limiter(self):
        now = [0.0]
        lim = bt.SlidingWindowLimiter(3, 30, clock=lambda: now[0], sleep=lambda s: None)  # wait 30 s > max_wait 15 s
        client = bt.BlofinPrivateClient(config(), transport=FakeBlofin(), limiter=lim)
        for _ in range(3):
            client.request("GET", "/api/v1/account/balance")
        with self.assertRaises(bt.RateLimited):
            client.request("GET", "/api/v1/account/balance")


class RedactionTest(unittest.TestCase):
    def test_secrets_never_reach_logs_or_repr(self):
        cfg = config()
        for text in (repr(cfg), str(cfg), repr(bt.BlofinPrivateClient(cfg, transport=FakeBlofin()))):
            for secret in (KEY, SECRET, PASSPHRASE):
                self.assertNotIn(secret, text)
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        logger = logging.getLogger("granmago.trading")
        logger.addHandler(handler)
        red = bt.install_redactor(cfg.secrets())
        try:
            logger.warning("key=%s secret=%s pass=%s", KEY, SECRET, PASSPHRASE)
        finally:
            logger.removeHandler(handler)
            for name in ("", "granmago", "granmago.trading", "waitress", "urllib3"):
                logging.getLogger(name).removeFilter(red)
            for h in logging.getLogger().handlers:
                h.removeFilter(red)
        out = stream.getvalue()
        self.assertIn("***", out)
        for secret in (KEY, SECRET, PASSPHRASE):
            self.assertNotIn(secret, out)


if __name__ == "__main__":
    unittest.main()
