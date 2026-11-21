"""1.8.3: QA 21/11/2026.

1) Signals left out by max_positions are logged like the other discard causes.
2) A time exit stays "time" when Blofin lists the close fill late (ADA-USDT,
   18/11 21:01: booked as "TP (exchange)" by the reconcile's distance guess).
3) The panel says takeProfitR does not apply in Gran Mago mode.
"""
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_blofin_only as base  # noqa: E402
import test_demo_server as tds  # noqa: E402
import test_panel_contract as tpc  # noqa: E402

server = base.server
PAIR = base.PAIR


def capture_add_log(test):
    """tools/backtest.py (imported by other test modules) turns add_log into a
    no-op; record the messages and log them at INFO like the real add_log."""
    test.msgs = []

    def add_log(msg):
        test.msgs.append(msg)
        server.log.info(msg)
    p = patch.object(server, "add_log", add_log)
    p.start()
    test.addCleanup(p.stop)


def journal_reason(tmp):
    con = sqlite3.connect(str(Path(tmp) / "fiscal_journal_demo.sqlite"))
    try:
        return [r[0] for r in con.execute("SELECT reason FROM trades ORDER BY seq")]
    finally:
        con.close()


class TimeExitReasonTest(unittest.TestCase):
    http = base.BlofinOnlyTest.http
    tearDown = tds.DemoServerTest.tearDown
    demo = tds.DemoServerTest.demo
    enter = tds.DemoServerTest.enter

    def setUp(self):
        tds.DemoServerTest.setUp(self)
        capture_add_log(self)

    def open_near_tp(self):
        self.demo()
        self.enter()
        pos = server.STATE["openPositions"][0]
        # ADA: the time exit filled much closer to the TP than to the SL
        self.fake.prices[PAIR] = pos["tp"] - (pos["tp"] - pos["entry"]) * 0.2
        return pos

    def test_time_exit_with_late_fill_stays_time(self):
        pos = self.open_near_tp()
        with server.LOCK:
            server._request_exchange_close(pos, "time")
        self.fake.fills_hidden_calls = 1  # fills-history does not list the close fill yet
        server.trading_cycle(force_reconcile=True)  # close loop cannot settle; reconcile sees it missing
        self.assertEqual(server.STATE["openPositions"], [])
        hist = server.STATE["history"][0]
        self.assertEqual(hist["reason"], "time")  # 1.8.1: "TP (exchange)"
        self.assertEqual(journal_reason(self.tmp.name), ["time"])
        self.assertIn("CERRADA DEMO %s time" % PAIR, " ".join(self.msgs))

    def test_time_exit_settled_on_a_later_reconcile(self):
        pos = self.open_near_tp()
        with server.LOCK:
            server._request_exchange_close(pos, "time")
        self.fake.fills_hidden_calls = 2  # neither the close loop nor this reconcile see it
        server.trading_cycle(force_reconcile=True)
        self.assertEqual(len(server.STATE["openPositions"]), 1)
        server.trading_cycle(force_reconcile=True)
        self.assertEqual(server.STATE["history"][0]["reason"], "time")
        self.assertEqual(journal_reason(self.tmp.name), ["time"])

    def test_exchange_closed_first_keeps_the_exchange_reason(self):
        pos = self.open_near_tp()
        self.fake.trigger_stop(PAIR)  # SL fires on Blofin before our time close goes out
        with server.LOCK:
            server._request_exchange_close(pos, "time")
        server.trading_cycle(force_reconcile=True)  # close-position answers 102005
        self.assertEqual(server.TRADER.last_close[PAIR], "already_closed")
        self.assertEqual(server.STATE["history"][0]["reason"], "SL (exchange)")
        self.assertEqual(journal_reason(self.tmp.name), ["SL (exchange)"])

    def test_normal_time_exit_unchanged(self):
        pos = self.open_near_tp()
        with server.LOCK:
            server._request_exchange_close(pos, "time")
        server.trading_cycle(force_reconcile=True)
        self.assertEqual(server.STATE["history"][0]["reason"], "time")
        self.assertEqual(server.TRADER.last_close[PAIR], "sent")

    def test_engine_reason_needs_a_fill_after_the_send(self):
        pos = {"closeRequested": {"reason": "time"}, "closeSentMs": 1_000_000}
        self.assertEqual(server._engine_close_reason(pos, {"ts": 1_000_000 - 4000}), "time")  # clock tolerance
        self.assertIsNone(server._engine_close_reason(pos, {"ts": 1_000_000 - 60_000}))
        self.assertIsNone(server._engine_close_reason({"closeRequested": {"reason": "time"}}, {"ts": 2_000_000}))
        self.assertIsNone(server._engine_close_reason({}, {"ts": 2_000_000}))


class MaxPositionsLogTest(unittest.TestCase):
    tearDown = base.BlofinOnlyTest.tearDown

    def setUp(self):
        base.BlofinOnlyTest.setUp(self)
        capture_add_log(self)
        server._MAXPOS_LOGGED.clear()
        server.STATE["settings"]["maxPositions"] = 3
        server.STATE["openPositions"] = [base.position(pair=p) for p in ("BTC-USDT", "ETH-USDT", "SOL-USDT")]

    def lines(self):
        return [m for m in self.msgs if "máximo de posiciones" in m]

    def test_full_book_logs_each_signal_once(self):
        self.assertEqual(server.max_positions(), 3)
        server.STATE["lastScan"] = [base.scan_row(), base.scan_row("BTC-USDT")]  # BTC: already open
        for _ in range(5):  # one scan every ~90 s on the same candle
            server.maybe_open()
        self.assertEqual(len(self.lines()), 1, self.lines())
        self.assertIn("No se abre LONG %s: máximo de posiciones alcanzado (3/3)." % PAIR, self.lines()[0])
        row = base.scan_row()
        row["granmago"]["closeMs"] += 4 * 3600 * 1000  # next candle: logged again
        server.STATE["lastScan"] = [row]
        server.maybe_open()
        self.assertEqual(len(self.lines()), 2)

    def test_same_format_and_level_as_other_causes(self):
        server.STATE["lastScan"] = [base.scan_row()]
        with self.assertLogs("granmago", "INFO") as logs:
            server.maybe_open()
        rec = [r for r in logs.records if "máximo de posiciones" in r.getMessage()]
        self.assertEqual([r.levelname for r in rec], ["INFO"])
        self.assertRegex(rec[0].getMessage(), r"^No se abre (LONG|SHORT) [A-Z0-9]+-USDT: ")

    def test_used_signal_or_stopped_bot_is_not_logged(self):
        sig = base.scan_row()["granmago"]
        server._remember_signal(PAIR, sig["side"], sig["confirmMs"], sig["closeMs"])
        server.STATE["lastScan"] = [base.scan_row()]
        server.maybe_open()
        server.STATE["running"] = False
        server._MAXPOS_LOGGED.clear()
        server.maybe_open()
        self.assertEqual(self.lines(), [])

    def test_dedup_memory_is_bounded(self):
        for i in range(server._MAXPOS_LOGGED_MAX + 50):
            row = base.scan_row()
            row["granmago"]["closeMs"] = i
            server.STATE["lastScan"] = [row]
            server.maybe_open()
        self.assertEqual(len(server._MAXPOS_LOGGED), server._MAXPOS_LOGGED_MAX)


class TakeProfitRPanelTest(unittest.TestCase):
    def test_startup_line_says_not_applied_in_granmago(self):
        src = (tpc.INDEX.parent.parent / "server.py").read_text(encoding="utf-8")
        self.assertIn("takeProfitR %sR no aplica en modo Gran Mago; solo en modo scanner", src)

    def test_panel_marks_take_profit_r(self):
        node, env = tpc._jsdom_env()
        if not node:
            self.skipTest("node/jsdom no disponibles")
        snap = json.loads(json.dumps(server.default_state()))
        snap.update(tradingMode="paper", ledgerMode="paper", display={}, trading={"mode": "paper"}, alerts=[],
                    market={}, killSwitch=False)
        gm = dict(snap, settings=dict(snap["settings"], strategy="granmago", takeProfitR=1.7))
        sc = dict(snap, settings=dict(snap["settings"], strategy="score", takeProfitR=1.7))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "snaps.json"
            path.write_text(json.dumps({"granmago": gm, "scanner": sc}), encoding="utf-8")
            proc = subprocess.run([node, str(tpc.HERE / "panel_render.js"), str(tpc.INDEX), str(path)],
                                  capture_output=True, text=True, env=env, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        out = json.loads(proc.stdout)
        for name in out:
            self.assertEqual(out[name]["errors"], [], name)
        g, s = out["granmago"], out["scanner"]
        self.assertIn("pivote diario", g["texts"]["targetRule"])
        self.assertIn("takeProfitR (1,7R): no aplica en modo Gran Mago", g["texts"]["targetRule"])
        self.assertIn("solo fija el objetivo en modo scanner", g["targetTitle"])
        self.assertIn("Objetivo: takeProfitR 1,7R", s["texts"]["targetRule"])
        self.assertNotIn("no aplica", s["texts"]["targetRule"])


if __name__ == "__main__":
    unittest.main()
