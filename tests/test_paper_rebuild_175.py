"""1.7.5: paper book still carrying the 1.7.0 demo balance (999.282 instead of 10382.24).

Reproduces the add-on state seen on 19/11/2026 (1.7.4, demo): migrations =
["1.7.1-ledgers"], paperSnapshot balance 999.282 / initial 10000 / 8 paper
trades, demo book active with 3 closed Blofin trades and an open SOL-USDT short.
"""
import copy
import json
import shutil
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_blofin_only as base  # noqa: E402
import test_demo_server as tds  # noqa: E402

server = base.server
DEMO_ENV, FIXTURE, persist, paper_trades = tds.DEMO_ENV, tds.FIXTURE, tds.persist, tds.paper_trades
DEMO_KEYS = ("balance", "initialBalance", "history", "openPositions", "tradeSeq", "usedSignals", "cooldowns",
             "demoLedger", "exchangeAccount", "killSwitch", "ledgerMode", "balanceSource")


def demo_row(pair, side, entry, exit_, pnl, reason, trade_id):
    return dict(pair=pair, side=side, entry=entry, exit=exit_, pnl=pnl, reason=reason, tradeId=trade_id,
                exchange="blofin-demo", time="2026-11-18 21:01")


def live_state_19_11():
    """The 19/11 state: 1.7.1 flag set, corrupt paper snapshot, active demo book."""
    st = server.default_state()
    st.update(
        running=True, ledgerMode="demo", balanceSource="blofin-demo", migrations=["1.7.1-ledgers"],
        balance=1012.7891, initialBalance=999.416, tradeSeq=4,
        history=[demo_row("ADA-USDT", "short", 0.1527, 0.1514, 0.87, "TP (exchange)", 1),
                 demo_row("SOL-USDT", "short", 71.11, 75.05, -10.41, "SL", 2),
                 demo_row("ENA-USDT", "short", 0.1177, 0.1106, 8.42, "time", 3)],
        openPositions=[dict(base.position(pair="SOL-USDT"), side="short", entry=75.12, sl=76.08, tp=71.98,
                            contracts=10.2, notional=766.22, exchange="blofin-demo", tradeId=4,
                            clientOrderId="gmeSOLabc", addBlocked=True)],
        paperSnapshot={"balance": 999.282, "initialBalance": 10000.0, "history": paper_trades(),
                       "openPositions": [], "tradeSeq": 8, "usedSignals": {}, "cooldowns": {},
                       "at": "2026-11-14T20:24:35+01:00"},
        exchangeAccount={"totalEquity": 1012.7891, "available": 742.54, "positionsMargin": 255.41},
    )
    return st


class PaperRebuildTest(unittest.TestCase):
    http = base.BlofinOnlyTest.http
    setUp = tds.DemoServerTest.setUp
    tearDown = tds.DemoServerTest.tearDown
    restart = tds.DemoServerTest.restart

    def boot(self, state, env=DEMO_ENV):
        server.STATE_FILE.write_text(json.dumps(state), encoding="utf-8")
        server.STATE = server.load_state()
        self.loaded = copy.deepcopy(server.STATE)  # as normalised by load_state, before migrations
        return server.init_trading(env, transport=self.fake, sleep=lambda s: None)

    def backups(self):
        return sorted(server.DATA_DIR.glob("paper_snapshot_backup_*.json"))

    def test_paper_trades_add_up_to_10382_24(self):
        self.assertAlmostEqual(10000 + sum(h["pnl"] for h in paper_trades()), 10382.24, places=6)

    def test_live_19_11_state_is_repaired_without_touching_demo(self):
        state = live_state_19_11()
        with self.assertLogs("granmago", level="WARNING") as logs:
            self.assertEqual(self.boot(state), "demo")
        self.assertTrue(any("Migración 1.7.5" in l and "10382.24" in l and "paper_snapshot_backup_" in l
                            for l in logs.output), logs.output)
        demo_before = {k: self.loaded.get(k) for k in DEMO_KEYS}
        snap = server.STATE["paperSnapshot"]
        self.assertAlmostEqual(snap["balance"], 10382.24, places=2)
        self.assertEqual(snap["initialBalance"], 10000.0)
        self.assertEqual(snap["history"], paper_trades())
        self.assertEqual(snap["tradeSeq"], 8)
        self.assertEqual(snap["previousBalance"], 999.282)
        self.assertEqual(snap["rebuiltBy"], server.PAPER_REBUILD_MIGRATION)
        # demo book, the open SOL-USDT and Blofin untouched
        self.assertEqual({k: server.STATE.get(k) for k in DEMO_KEYS}, demo_before)
        self.assertEqual(self.fake.calls, [])
        # backup of the old snapshot in the data dir + log line
        (backup,) = self.backups()
        self.assertEqual(json.loads(backup.read_text(encoding="utf-8")), state["paperSnapshot"])
        self.assertEqual(server.STATE["lastPaperRebuild"]["old"], 999.282)
        # back to paper: 10382.24 with its 8 trades, never 999.282
        persist()
        self.assertEqual(self.restart({}), "paper")
        self.assertAlmostEqual(server.STATE["balance"], 10382.24, places=2)
        self.assertEqual(len(server.STATE["history"]), 8)
        self.assertEqual(server.STATE["demoLedger"]["openPositions"][0]["pair"], "SOL-USDT")

    def test_idempotent(self):
        self.boot(live_state_19_11())
        persist()
        snap, demo = copy.deepcopy(server.STATE["paperSnapshot"]), {k: copy.deepcopy(server.STATE.get(k)) for k in DEMO_KEYS}
        for _ in range(2):
            server.STATE = server.load_state()
            server.init_trading(DEMO_ENV, transport=self.fake, sleep=lambda s: None)
            persist()
            self.assertEqual(server.STATE["paperSnapshot"], snap)
            self.assertEqual({k: server.STATE.get(k) for k in DEMO_KEYS}, demo)
            self.assertEqual(server.STATE["migrations"].count(server.PAPER_REBUILD_MIGRATION), 1)
        # even forced to run again (flag removed) a repaired book is left alone
        server.STATE["migrations"].remove(server.PAPER_REBUILD_MIGRATION)
        with server.LOCK:
            self.assertIsNone(server._migrate_paper_rebuild_1_7_5())
        self.assertEqual(server.STATE["paperSnapshot"], snap)
        self.assertEqual(len(self.backups()), 1)

    def test_root_cause_1_7_1_kept_snapshot_when_blofin_balance_moved(self):
        # Pre-1.7.1 state where trading_cycle had already refreshed the balance
        # from Blofin (999.416 != snapshot 999.282): 1.7.1 kept the snapshot.
        state = json.loads(FIXTURE.read_text(encoding="utf-8"))
        state["balance"] = 999.416
        self.boot(state)
        self.assertIn(server.LEDGER_MIGRATION, server.STATE["migrations"])
        self.assertAlmostEqual(server.STATE["paperSnapshot"]["balance"], 10382.24, places=2)

    def test_contaminated_active_paper_book_is_repaired(self):
        st = live_state_19_11()
        snap = st.pop("paperSnapshot")
        st.update(ledgerMode="paper", balanceSource="paper", balance=snap["balance"], initialBalance=10000.0,
                  history=snap["history"], openPositions=[], tradeSeq=8,
                  demoLedger={"balance": 1012.79, "initialBalance": 999.416, "history": [], "openPositions": []})
        self.assertEqual(self.boot(st, env={}), "paper")
        demo = self.loaded["demoLedger"]
        self.assertAlmostEqual(server.STATE["balance"], 10382.24, places=2)
        self.assertEqual(len(server.STATE["history"]), 8)
        self.assertEqual(server.STATE["demoLedger"], demo)
        self.assertEqual(len(self.backups()), 1)

    def test_healthy_books_are_left_alone(self):
        # a real paper book with an open position (entry fee already paid) is consistent
        st = live_state_19_11()
        st["paperSnapshot"].update(balance=10382.24 - 0.24, openPositions=[dict(base.position(), entryFee=0.24)])
        self.boot(st)
        good = self.loaded["paperSnapshot"]
        self.assertEqual({k: v for k, v in server.STATE["paperSnapshot"].items()}, good)
        self.assertEqual(self.backups(), [])
        self.assertIn(server.PAPER_REBUILD_MIGRATION, server.STATE["migrations"])
        # without initialBalance nothing is guessed (the active initialBalance is the demo one)
        st = live_state_19_11()
        st["paperSnapshot"]["initialBalance"] = None
        with self.assertLogs("granmago", level="WARNING") as logs:
            self.boot(st)
        bad = self.loaded["paperSnapshot"]
        self.assertEqual(server.STATE["paperSnapshot"], bad)
        self.assertTrue(any("sin comprobar" in l for l in logs.output))
        # inconsistent but nowhere near a demo balance (e.g. hand-edited): not touched
        st = live_state_19_11()
        st["paperSnapshot"]["balance"] = 1234.5
        with self.assertLogs("granmago", level="WARNING") as logs:
            self.boot(st)
        self.assertEqual(server.STATE["paperSnapshot"]["balance"], 1234.5)
        self.assertTrue(any("no parece un saldo demo" in l for l in logs.output))
        self.assertEqual(self.backups(), [])

    def test_fresh_and_1_7_0_states_unchanged_behaviour(self):
        self.assertEqual(self.boot(server.default_state(), env={}), "paper")
        self.assertEqual(self.backups(), [])
        shutil.copy(FIXTURE, server.STATE_FILE)  # 1.7.1 migration rebuilds; 1.7.5 then finds it correct
        server.STATE = server.load_state()
        server.init_trading(DEMO_ENV, transport=self.fake, sleep=lambda s: None)
        self.assertAlmostEqual(server.STATE["paperSnapshot"]["balance"], 10382.24, places=2)
        self.assertEqual(self.backups(), [])


if __name__ == "__main__":
    unittest.main()
