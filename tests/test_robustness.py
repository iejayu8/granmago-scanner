"""1.7.0 market-data robustness (Jarvis report P0-P4). Public HTTP mocked."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_blofin_only as base  # noqa: E402

server = base.server
NOW, PAIR = base.NOW, base.PAIR
BOUNDARY = 1794124800  # a 4H boundary (multiple of 14400)


class RobustnessTest(unittest.TestCase):
    http = base.BlofinOnlyTest.http

    def setUp(self):
        base.BlofinOnlyTest.setUp(self)
        server._MARKET_STATS.clear()
        self.kw = []
        orig = self.http

        def recording(session, method, url, **kw):
            self.kw.append(kw)
            return orig(session, method, url, **kw)
        self.http = recording

    def tearDown(self):
        base.BlofinOnlyTest.tearDown(self)

    # --------------------------------------------------------------- quotes
    def test_book_mid_price_and_freshness_from_book_ts(self):
        self.edit_tick = lambda t: dict(t, bidPrice="100.0", askPrice="100.2", ts=str(int((NOW - 9) * 1000)))
        q = server.fetch_quote(PAIR)
        self.assertAlmostEqual(q.price, 100.1)
        self.assertEqual((q.bid, q.ask), (100.0, 100.2))
        self.assertEqual(server._quote_audit(q)["priceBasis"], "mid")
        self.edit_tick = lambda t: dict(t, ts=str(int((NOW - 11) * 1000)))
        self.assertIsNone(server.fetch_quote(PAIR))
        reasons = server.market_stats()["endpoints"]["quote"]["reasons"]
        self.assertIn("libro obsoleto", reasons)
        self.assertTrue(all("/ticker" not in u for u in self.urls))  # ticker 'last' no longer needed

    def test_wide_spread_and_crossed_book_rejected_with_reason(self):
        self.edit_tick = lambda t: dict(t, bidPrice="100", askPrice="103")
        self.assertIsNone(server.fetch_quote(PAIR))
        self.edit_tick = lambda t: dict(t, bidPrice="101", askPrice="100")
        self.assertIsNone(server.fetch_quote(PAIR))
        reasons = server.market_stats()["endpoints"]["quote"]["reasons"]
        self.assertIn("spread excesivo", reasons)
        self.assertIn("libro cruzado", reasons)
        self.assertEqual(server.market_stats()["endpoints"]["quote"]["fail"], 2)

    def test_shared_session_timeouts_and_instrument_cache(self):
        server.fetch_quote(PAIR)
        server.fetch_quote(PAIR)
        self.assertEqual(len([u for u in self.urls if "/instruments?" in u]), 1)
        self.assertTrue(all(kw.get("timeout") == (server.HTTP_CONNECT_TIMEOUT, server.HTTP_READ_TIMEOUT)
                            for kw in self.kw))
        self.assertIs(server._session(), server._session())
        stamp, meta = server._INSTRUMENTS[PAIR]
        server._INSTRUMENTS[PAIR] = (stamp - server.INSTRUMENTS_TTL_SECONDS - 1, meta)  # expire
        server.fetch_quote(PAIR)
        self.assertEqual(len([u for u in self.urls if "/instruments?" in u]), 2)
        server.instrument_meta(PAIR, force=True)  # orders always refetch
        self.assertEqual(len([u for u in self.urls if "/instruments?" in u]), 3)
        ep = server.market_stats()["endpoints"]
        self.assertGreaterEqual(ep["books"]["ok"], 3)
        self.assertIsNotNone(ep["books"]["p50ms"])

    def test_retry_backoff_has_jitter(self):
        self.http_status = 503
        slept = []
        with patch.object(server.time, "sleep", side_effect=slept.append):
            self.assertIsNone(server.fetch_quote(PAIR))
        self.assertTrue(slept)
        self.assertTrue(all(0.25 <= s <= 0.25 * 2 ** 3 * 1.5 for s in slept), slept)
        self.assertTrue(server.market_stats()["endpoints"]["instruments"]["fail"] >= 1)

    # ------------------------------------------------------------- degraded
    def test_pair_degrades_after_repeated_failures_and_needs_probation(self):
        self.edit_tick = lambda t: dict(t, ts=str(int((NOW - 60) * 1000)))
        for _ in range(server.PAIR_DEGRADED_FAILURES):
            server.fetch_quote(PAIR)
        self.assertTrue(server.pair_degraded(PAIR))
        self.assertEqual(server.market_stats()["degraded"], [PAIR])
        self.edit_tick = lambda t: t
        server.STATE["lastScan"] = [base.scan_row()]
        server.maybe_open()  # healthy again, but this cycle is probation
        self.assertEqual(server.STATE["openPositions"], [])
        self.assertFalse(server.pair_degraded(PAIR))
        server.STATE["lastScan"] = [base.scan_row()]
        server.maybe_open()
        self.assertEqual(len(server.STATE["openPositions"]), 1)

    # -------------------------------------------------------------- candles
    def candles(self, last_open):
        rows = [[str((last_open - i * 14400) * 1000), "10", "11", "9", "10.5", "1", "1", "1", "1"]
                for i in range(5)]
        return {"code": "0", "data": rows}

    def test_just_closed_candle_accepted_within_grace(self):
        last_open = BOUNDARY - 14400  # Blofin has not published the new candle yet
        with patch.object(server, "fetch_json", return_value=self.candles(last_open)):
            server._clock = lambda: BOUNDARY + 170
            self.assertEqual(len(server.fetch_candles(PAIR, "4H", 5)), 5)
            server._clock = lambda: BOUNDARY + server.CANDLE_CLOSE_GRACE_SECONDS + 10
            self.assertEqual(server.fetch_candles(PAIR, "4H", 5), [])
        st = server.market_stats()["endpoints"]["candles:4H"]
        self.assertEqual((st["ok"], st["fail"]), (1, 1))
        self.assertIn("velas 4H obsoletas", st["reasons"])

    def test_failed_pairs_are_retried_at_end_of_scan(self):
        calls = []

        def analyze(pair, threshold=None):
            calls.append(pair)
            if pair == "BTC-USDT" and calls.count(pair) == 1:
                return None
            return dict(pair=pair, price=1.0, scoreLong=0, scoreShort=0, validLong=False, validShort=False,
                        granmago=None, bias="NEUTRAL")
        server.STATE["running"] = False
        with patch.object(server, "analyze_pair", side_effect=analyze), \
                patch.object(server, "active_pairs", return_value=["BTC-USDT", "ETH-USDT"]), \
                patch.object(server.time, "sleep"):
            server.scan_all()
        self.assertEqual(calls, ["BTC-USDT", "ETH-USDT", "BTC-USDT"])
        self.assertEqual(server.STATE["lastScanRetry"]["recovered"], ["BTC-USDT"])
        self.assertEqual({r["pair"] for r in server.STATE["lastScan"]}, {"BTC-USDT", "ETH-USDT"})


if __name__ == "__main__":
    unittest.main()
