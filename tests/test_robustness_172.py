"""1.7.2 robustness: demo universe, clock skew, pair breaker, rate limit vs
ambiguity, late attached TPSL, loop supervision and a concurrency stress test."""
import json
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_blofin_only as base  # noqa: E402
import test_demo_server as tds  # noqa: E402
from test_blofin_trading import TraderCase, bt, plan  # noqa: E402

server = base.server


class DemoHarness(unittest.TestCase):
    """test_demo_server's harness without re-running its tests."""
    http = base.BlofinOnlyTest.http
    setUp = tds.DemoServerTest.setUp
    tearDown = tds.DemoServerTest.tearDown
    demo = tds.DemoServerTest.demo
    enter = tds.DemoServerTest.enter


class DemoUniverseTest(DemoHarness):
    def test_iost_is_filtered_and_never_ordered(self):
        self.demo()  # start_trading loads the demo universe before any scan
        self.assertNotIn("IOST-USDT", server.active_pairs())
        self.assertIn("IOST-USDT", server._DEMO_UNIVERSE["missing"])
        server.STATE["settings"]["pairs"] = "XMR-USDT,IOST-USDT"
        self.assertEqual(server.active_pairs(), ["XMR-USDT"])
        server.STATE["lastScan"] = [base.scan_row("IOST-USDT")]
        server.maybe_open()
        self.assertEqual(self.fake.posts("/api/v1/trade/order"), [])
        # the entry gateway refuses it as well, even if a stale scan row slips through
        q = base.quote(pair="IOST-USDT")
        self.assertFalse(server._open_on_exchange({"pair": "IOST-USDT", "side": "long"}, q))
        self.assertEqual(self.fake.posts("/api/v1/trade/order"), [])

    def test_refresh_failure_keeps_previous_list(self):
        self.demo()
        before = set(server._DEMO_UNIVERSE["ids"])
        real = self.http

        def http(session, method, url, **kw):
            if url.startswith(server.DEMO_MARKET):
                return base.response(status=503)
            return real(session, method, url, **kw)
        self.http = http
        self.assertEqual(server.refresh_demo_universe(force=True), before)
        self.assertNotIn("IOST-USDT", server.active_pairs())

    def test_paper_does_not_filter(self):
        self.assertIn("IOST-USDT", server.active_pairs())


class ClockSkewTest(unittest.TestCase):
    def setUp(self):
        self.saved = server._clock
        server._clock = None
        server._CLOCK.update(skew=None, alerted=False)
        server._CLOCK["samples"].clear()
        server.ALERTS.clear()

    def tearDown(self):
        server._clock = self.saved
        server._CLOCK.update(skew=None, alerted=False)
        server._CLOCK["samples"].clear()

    def header(self, offset):
        return time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime(time.time() - offset))

    def test_skew_alerts_once_and_recovers(self):
        for _ in range(6):
            server._note_clock(self.header(6))
        self.assertGreaterEqual(server._CLOCK["skew"], 5)
        self.assertEqual([a["kind"] for a in server.ALERTS], ["reloj"])
        self.assertGreaterEqual(server.health()["clockSkewSeconds"], 5)
        for _ in range(20):
            server._note_clock(self.header(0))
        self.assertLessEqual(abs(server._CLOCK["skew"]), 1)
        self.assertFalse(server._CLOCK["alerted"])
        server._note_clock("not a date")  # ignored
        server._note_clock(None)

    def test_backtest_clock_skips(self):
        server._clock = lambda: 0
        server._note_clock(self.header(100))
        self.assertIsNone(server._CLOCK["skew"])


class PairBreakerTest(unittest.TestCase):
    def setUp(self):
        server._PAIR_HEALTH.clear()
        self.t = [1000.0]
        self.p = patch.object(server.time, "monotonic", lambda: self.t[0])
        self.p.start()

    def tearDown(self):
        self.p.stop()
        server._PAIR_HEALTH.clear()

    def test_sparse_failures_do_not_degrade(self):
        for _ in range(5):
            server.note_pair("ABC-USDT", False, "x")
            self.t[0] += server.PAIR_DEGRADED_SECONDS + 1
        self.assertFalse(server.pair_degraded("ABC-USDT"))

    def test_consecutive_failures_degrade_and_recover(self):
        for _ in range(server.PAIR_DEGRADED_FAILURES):
            server.note_pair("ABC-USDT", False, "x")
            self.t[0] += server.MONITOR_SECONDS
        self.assertTrue(server.pair_degraded("ABC-USDT"))
        server.note_pair("ABC-USDT", True)
        self.assertFalse(server.pair_degraded("ABC-USDT"))


class RateLimitAndProtectionTest(TraderCase):
    def test_rate_limit_before_post_is_a_refusal_not_ambiguity(self):
        self.trader.prepare_account()

        def refuse(*a, **kw):
            raise bt.RateLimited("ventana llena")
        with patch.object(self.trader.client.limiter, "acquire", refuse):
            self.assertIsNone(self.trader.open_position(plan()))
        self.assertTrue(self.trader.safe, self.trader.unsafe_reasons)
        self.assertEqual(self.fake.posts("/api/v1/trade/order"), [])
        last = list(self.trader.intents.pending().values())
        self.assertEqual(last, [])  # nothing left to resolve at startup

    def test_late_attached_tpsl_is_not_duplicated(self):
        self.trader.prepare_account()
        real = self.fake.h_get_trade_orders_tpsl_pending
        calls = {"n": 0}

        def late(p, b):
            calls["n"] += 1
            return {"code": "0", "data": []} if calls["n"] == 1 else real(p, b)
        self.fake.h_get_trade_orders_tpsl_pending = late
        fill = self.trader.open_position(plan())
        self.assertIsNotNone(fill)
        self.assertEqual(self.fake.posts("/api/v1/trade/order-tpsl"), [])
        self.assertEqual(len([t for t in self.fake.tpsl if t["state"] == "live"]), 1)

    def test_missing_attached_tpsl_is_placed_once(self):
        self.trader.prepare_account()
        self.fake.ignore_attached = True
        fill = self.trader.open_position(plan())
        self.assertIsNotNone(fill)
        self.assertEqual(len(self.fake.posts("/api/v1/trade/order-tpsl")), 1)


class SupervisionTest(unittest.TestCase):
    def setUp(self):
        server.THREADS.pop("t-test", None)
        server.STOP.clear()

    def tearDown(self):
        server.STOP.clear()
        server.THREADS.pop("t-test", None)

    def test_loop_that_raises_is_restarted_and_reported(self):
        runs = []

        def target():
            runs.append(1)
            if len(runs) == 1:
                raise ValueError("boom")
            if len(runs) == 2:
                return  # a loop that returns is restarted too
            server.STOP.set()
        with patch.object(server.log, "exception"):
            server._supervised("t-test", target, pause=0)()
        self.assertEqual(len(runs), 3)
        self.assertEqual(server.THREADS["t-test"]["restarts"], 2)
        self.assertIn("ValueError", server.THREADS["t-test"]["lastError"])

    def test_stale_thread_turns_health_red(self):
        server._beat("t-test")
        self.assertFalse(server.thread_health()["t-test"]["stale"])
        server.THREADS["t-test"]["beat"] -= 10_000
        self.assertTrue(server.thread_health()["t-test"]["stale"])
        self.assertFalse(server.health()["ok"])


class ConcurrencyStressTest(DemoHarness):
    """Panel polls, kill switch, monitor, trading and scan all at once."""

    def test_parallel_api_and_loops(self):
        self.demo()
        server.TRADER.client.limiter.acquire = lambda *a, **k: None  # stress the locks, not the budget
        self.enter()
        self.assertEqual(len(server.STATE["openPositions"]), 1)
        errors, bodies = [], []
        stop = threading.Event()
        ingress = {"REMOTE_ADDR": server.INGRESS_IP}

        def guard(fn):
            def run():
                client = server.app.test_client()
                while not stop.is_set():
                    try:
                        fn(client)
                    except Exception as exc:  # noqa: BLE001
                        errors.append(repr(exc))
                        return
            return run

        def poll(client):
            r = client.get("/api/state")
            if r.status_code != 200:
                errors.append("state %s" % r.status_code)
            bodies.append(json.loads(r.get_data(as_text=True)))
            client.get("/api/health")

        def kill(client):
            for on in (True, False):
                r = client.post("/api/kill_switch", json={"enabled": on}, environ_base=ingress)
                if r.status_code != 200:
                    errors.append("kill %s" % r.status_code)

        def monitor(client):
            server.monitor_position()

        def trading(client):
            server.trading_cycle(force_reconcile=True)

        def scan(client):
            server.STATE["lastScan"] = [base.scan_row()]
            server.maybe_open()

        workers = [threading.Thread(target=guard(f), daemon=True)
                   for f in (poll, poll, poll, poll, kill, monitor, trading, scan)]
        with patch.object(server.log, "exception", lambda *a, **k: errors.append(("log.exception", a))):
            for w in workers:
                w.start()
            time.sleep(2.0)
            stop.set()
            for w in workers:
                w.join(10)
        self.assertFalse(any(w.is_alive() for w in workers), "bloqueo (deadlock)")
        self.assertEqual(errors, [])
        self.assertGreater(len(bodies), 20)
        for b in bodies:
            pairs = [p["pair"] for p in b["openPositions"]]
            self.assertEqual(len(pairs), len(set(pairs)), "posición duplicada")
        self.assertEqual(len(self.fake.posts("/api/v1/trade/order")), 1, "entrada duplicada")
        self.assertTrue(server.TRADER.safe, server.TRADER.status())
        # final: kill switch with close-all leaves nothing open on either side
        server.kill_switch(True, close_all=True)
        server.trading_cycle(force_reconcile=True)
        self.assertEqual(server.STATE["openPositions"], [])
        self.assertEqual([p for p in self.fake.positions.values() if p["positions"]], [])


if __name__ == "__main__":
    unittest.main()
