"""1.7.7: Blofin balance alert.

20/11/2026 07:18-07:30 (Madrid) GET /api/v1/account/balance answered
code=500 "Server internal error". 1.7.6 did not retry, appended a new
"Balance demo no disponible" alert every cycle and never removed them, so the
panel kept showing 4 copies after Blofin recovered.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_blofin_only as base  # noqa: E402
import test_demo_server as tds  # noqa: E402

server = base.server
bt = server.blofin_trading if hasattr(server, "blofin_trading") else __import__("blofin_trading")
MSG = "Balance demo no disponible"
ROOT = Path(__file__).resolve().parent.parent


class BalanceAlertTest(unittest.TestCase):
    http = base.BlofinOnlyTest.http
    setUp = tds.DemoServerTest.setUp
    tearDown = tds.DemoServerTest.tearDown
    demo = tds.DemoServerTest.demo

    def start(self):
        self.demo()
        self.slept = []
        server.TRADER._sleep = self.slept.append
        # back-to-back cycles here; live they are 60 s apart (limiter window 10 s)
        server.TRADER.client.limiter.acquire = lambda *a, **k: None

    def balance_gets(self):
        return sum(1 for c in self.fake.calls if c[1] == "/api/v1/account/balance")

    def alerts(self):
        st = self.client.get("/api/state").get_json()
        return [a for a in st["trading"]["alerts"] if MSG in a["msg"]], st

    def test_transient_500_is_retried_without_alert(self):
        self.start()
        n = self.balance_gets()
        self.fake.balance_errors = 2  # two 500s, third attempt answers
        server.trading_cycle(force_reconcile=True)
        self.assertEqual(self.balance_gets() - n, 3)
        self.assertEqual(self.slept, [0.5, 1.5])
        self.assertEqual(self.alerts()[0], [])
        self.assertEqual(server.STATE["exchangeAccount"]["equity"], 1000.5)

    def test_outage_gives_one_counted_alert_then_clears(self):
        self.start()
        before = dict(server.STATE["exchangeAccount"])
        balance = server.STATE["balance"]
        self.fake.balance_errors = 10 ** 6  # Blofin down for the whole window
        for _ in range(4):
            server.trading_cycle(force_reconcile=True)
        alerts, st = self.alerts()
        self.assertEqual(len(alerts), 1, alerts)  # 1.7.6: four copies
        self.assertEqual(alerts[0]["count"], 4)
        self.assertIn("code=500", alerts[0]["msg"])
        self.assertLessEqual(alerts[0]["firstAt"], alerts[0]["at"])
        self.assertEqual(sum(self.slept), 4 * 2.0)  # backoff bounded: 2 s per cycle
        # nothing invented: last real Blofin read stays, flagged by its age
        self.assertEqual(server.STATE["exchangeAccount"]["fetchedAtMs"], before["fetchedAtMs"])
        self.assertEqual(server.STATE["balance"], balance)
        health = self.client.get("/api/health").get_json()
        self.assertEqual(len([a for a in health["alerts"] if MSG in a["msg"]]), 1)

        self.fake.balance_errors = 0  # Blofin back
        server.trading_cycle(force_reconcile=True)
        self.assertEqual(self.alerts()[0], [])  # 1.7.6: stayed forever
        self.assertGreater(server.STATE["exchangeAccount"]["fetchedAtMs"], before["fetchedAtMs"] - 1)

    def test_business_errors_are_not_retried(self):
        self.start()
        calls = []

        def boom(method, path, params=None, **kw):
            calls.append(path)
            raise bt.BlofinError("429", "rate limit / firewall", path)
        server.TRADER.client.request = boom
        with self.assertRaises(bt.BlofinError):
            server.TRADER.account()
        self.assertEqual((len(calls), self.slept), (1, []))


class DedupUnitTest(unittest.TestCase):
    def test_is_transient(self):
        self.assertTrue(bt.is_transient(bt.BlofinError("500", "Server internal error")))
        self.assertTrue(bt.is_transient(bt.BlofinError(503, "x")))
        self.assertTrue(bt.is_transient(bt.AmbiguousResult("HTTP 502")))
        for code in ("429", "403", "152409", "0", ""):
            self.assertFalse(bt.is_transient(bt.BlofinError(code, "x")), code)
        self.assertFalse(bt.is_transient(ValueError("x")))

    def test_same_text_is_kept_once(self):
        t = bt.DemoTrader.__new__(bt.DemoTrader)
        from collections import deque
        t.alerts, clock = deque(maxlen=50), iter(range(100, 200))
        t._clock = lambda: next(clock)
        with self.assertLogs("granmago", "WARNING") as logs:
            for _ in range(12):
                t.alert("X falló")
            t.alert("otra")
        self.assertEqual([(a["msg"], a["count"]) for a in t.alerts], [("X falló", 12), ("otra", 1)])
        self.assertEqual(len(logs.records), 3)  # 1st, 10th and "otra"
        self.assertEqual(t.clear_alert("X falló"), 1)
        self.assertEqual([a["msg"] for a in t.alerts], ["otra"])

    def test_panel_shows_count(self):
        html = (ROOT / "granmago" / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn("a.count > 1", html)


if __name__ == "__main__":
    unittest.main()
