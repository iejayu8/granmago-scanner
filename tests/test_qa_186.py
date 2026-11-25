"""1.8.6: QA 25/11/2026.

1) Blofin serves the just-closed candle as provisional (confirm="0") for a few
   seconds after the boundary, with OHLC that still change (probe 25/11 10:15
   Madrid, BTC-USDT 15m: low 59049.6 / close 59060.6 with confirm 0 at +0..+3 s,
   low 59029.8 / close 59051.5 with confirm 1 from +4 s). The strategy and the
   market-regime log used it as a closed candle.
2) Signals discarded in maybe_open before _granmago_plan (kill switch, pair
   degraded, no fresh ticker) were missing from signal_events although 1.8.4
   promises every opened or discarded signal there.
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_blofin_only as base  # noqa: E402
import test_market_regime_184 as t184  # noqa: E402
from test_qa_183 import capture_add_log  # noqa: E402

server = base.server
import granmago_strategy as g  # noqa: E402
import market_regime as mr  # noqa: E402

H4 = mr.H4_MS


class ProvisionalCandleTest(unittest.TestCase):
    http = base.BlofinOnlyTest.http
    tearDown = base.BlofinOnlyTest.tearDown

    def setUp(self):
        base.BlofinOnlyTest.setUp(self)

    def body(self, last_open_s, confirm_last):
        rows = [[str((last_open_s - i * 14400) * 1000), "10", "11", "9", "10.5", "1", "1", "1",
                 confirm_last if i == 0 else "1"] for i in range(5)]
        return {"code": "0", "data": rows}

    def test_fetch_marks_unconfirmed_rows_only(self):
        boundary = (int(base.NOW) // 14400) * 14400
        server._clock = lambda: boundary + 2
        with patch.object(server, "fetch_json", return_value=self.body(boundary - 14400, "0")):
            rows = server.fetch_candles(base.PAIR, "4H", 5)
        self.assertEqual(len(rows), 5)
        self.assertIs(rows[-1].get("confirm"), False)
        self.assertTrue(all("confirm" not in r for r in rows[:-1]))
        with patch.object(server, "fetch_json", return_value=self.body(boundary - 14400, "1")):
            rows = server.fetch_candles(base.PAIR, "4H", 5)
        self.assertTrue(all("confirm" not in r for r in rows))
        old = self.body(boundary - 14400, "1")
        for r in old["data"]:
            del r[8]  # 8-column rows (old format / fixtures): confirmed by time as before
        with patch.object(server, "fetch_json", return_value=old):
            self.assertTrue(all("confirm" not in r for r in server.fetch_candles(base.PAIR, "4H", 5)))

    def test_drop_forming_drops_a_provisional_last_bar(self):
        now = 10 * H4 + 2000
        rows = [dict(ts=i * H4, o=1, h=1, l=1, c=1) for i in range(10)]
        self.assertEqual(len(g.drop_forming(rows, H4, now)), 10)
        rows[-1]["confirm"] = False  # just closed, Blofin still provisional
        out = g.drop_forming(rows + [dict(ts=10 * H4, o=1, h=1, l=1, c=1, confirm=False)], H4, now)
        self.assertEqual([r["ts"] for r in out], [i * H4 for i in range(9)])
        self.assertEqual(len(g.drop_forming(rows, H4, None)), 10)  # replays keep the series


class ProvisionalRegimeTest(t184.RegimeEngineMixin, unittest.TestCase):
    http = base.BlofinOnlyTest.http
    tearDown = base.BlofinOnlyTest.tearDown

    def setUp(self):
        base.BlofinOnlyTest.setUp(self)
        self.store = self.start_regime()

    def feed(self, provisional):
        for p, rows in t184.upto(t184.LAST_CLOSE).items():
            rows = [dict(r) for r in rows[:-1]]  # no new forming candle yet (publication delay)
            if provisional:
                rows[-1]["confirm"] = False
                rows[-1]["c"] = rows[-1]["c"] * 1.01  # provisional close, not the final one
            server._regime_observe(p, rows)

    def test_waits_for_the_confirmed_candle(self):
        self.feed(provisional=True)
        self.assertIsNone(server.market_regime_step(t184.LAST_CLOSE + 3000))
        self.assertEqual(self.store.regime_rows(), [])
        self.feed(provisional=False)
        row = server.market_regime_step(t184.LAST_CLOSE + 95000)
        self.assertEqual(row["regime"], "movido")
        self.assertEqual(row["n_symbols"], 27)

    def test_still_provisional_after_the_wait_is_missing_not_used(self):
        self.feed(provisional=True)
        row = server.market_regime_step(t184.LAST_CLOSE + 31 * 60000)
        self.assertEqual(row["regime"], "insuficiente")
        self.assertIn("BTC-USDT:vela %s sin confirmar" % mr.utc_iso(t184.LAST_CLOSE), row["missing"])


class MaybeOpenDiscardEventsTest(t184.RegimeEngineMixin, unittest.TestCase):
    http = base.BlofinOnlyTest.http
    tearDown = base.BlofinOnlyTest.tearDown

    def setUp(self):
        base.BlofinOnlyTest.setUp(self)
        capture_add_log(self)
        server.STATE["settings"]["maxPositions"] = 3
        server.STATE.update(running=True, balance=1023.05, history=[], openPositions=[])
        self.store = self.start_regime()

    def reasons(self):
        return {(e["pair"], e["outcome"], e["reason"]) for e in self.store.event_rows()}

    def test_kill_switch_discards_are_recorded_once(self):
        server.STATE["killSwitch"] = True
        server.STATE["lastScan"] = t184.scenario_rows()
        server.maybe_open()
        server.maybe_open()
        self.assertEqual(server.STATE["openPositions"], [])
        self.assertEqual(self.reasons(), {(p, "descartada", "kill_switch") for p in t184.SCENARIO_PAIRS})
        self.assertEqual(len(self.store.event_rows()), len(t184.SCENARIO_PAIRS))

    def test_no_fresh_ticker_is_recorded(self):
        server.STATE["lastScan"] = [base.scan_row("XMR-USDT")]
        with patch.object(server, "fetch_quote", lambda pair: None):
            server.maybe_open()
        self.assertEqual(server.STATE["openPositions"], [])
        self.assertEqual(self.reasons(), {("XMR-USDT", "descartada", "sin_ticker_fresco")})

    def test_degraded_pair_is_recorded(self):
        server.STATE["lastScan"] = [base.scan_row("XMR-USDT")]
        with patch.object(server, "pair_degraded", lambda pair: True):
            server.maybe_open()
        self.assertEqual(server.STATE["openPositions"], [])
        self.assertEqual(self.reasons(), {("XMR-USDT", "descartada", "par_degradado")})

    def test_trading_unchanged_without_store(self):
        self.stop_regime()
        server.STATE["killSwitch"] = True
        server.STATE["lastScan"] = t184.scenario_rows()
        server.maybe_open()
        self.assertEqual(server.STATE["openPositions"], [])


if __name__ == "__main__":
    unittest.main()
