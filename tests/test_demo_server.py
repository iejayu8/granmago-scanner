"""Server integration of the Blofin Demo layer: fake exchange, mocked public HTTP."""
import copy
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_blofin_only as base  # noqa: E402  (sets DATA_DIR, imports server)
from fake_blofin import KEY, PASSPHRASE, SECRET, FakeBlofin  # noqa: E402

server = base.server
PAIR = base.PAIR
DEMO_ENV = {"TRADING_MODE": "demo", "BLOFIN_API_KEY": KEY, "BLOFIN_API_SECRET": SECRET,
            "BLOFIN_API_PASSPHRASE": PASSPHRASE, "LEVERAGE": "3"}
REMOTE = {"REMOTE_ADDR": "192.168.1.50"}
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "state_1_7_0_demo.json"


def persist():
    """Write state.json like save_state does. tools/backtest.py (imported by
    other test modules) replaces server.save_state with a no-op."""
    payload = {k: v for k, v in server.STATE.items() if k != "busy"}
    server.STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    server.STATE_FILE.write_text(json.dumps(payload), encoding="utf-8")


def paper_trades(n=8):
    pnls = [131.34, 20.57, 64.82, 46.57, 3.0, 75.43, 41.52, -1.01][:n]
    return [dict(pair="XLM-USDT", side="long", entry=1.0, exit=1.1, pnl=p, reason="TP", tradeId=8 - i,
                 time="2026-11-1%d 10:00" % (i % 5), closeMs=1794000000000 + i, confirmMs=14400000)
            for i, p in enumerate(pnls)]


def xmr_meta(meta):
    return dict(meta, contractValue="0.01", lotSize="1", minSize="1", tickSize="0.01", maxMarketSize="10000")


class DemoServerTest(unittest.TestCase):
    http = base.BlofinOnlyTest.http

    def setUp(self):
        base.BlofinOnlyTest.setUp(self)
        self.edit_meta = xmr_meta
        self.tmp = tempfile.TemporaryDirectory()
        self.data = patch.object(server, "DATA_DIR", Path(self.tmp.name))
        self.data.start()
        self.fake = FakeBlofin()
        self.client = server.app.test_client()

    def tearDown(self):
        server.init_trading({})  # back to paper
        self.data.stop()
        base.BlofinOnlyTest.tearDown(self)
        self.tmp.cleanup()

    def demo(self):
        self.assertEqual(server.init_trading(DEMO_ENV, transport=self.fake, sleep=lambda s: None), "demo")
        server.start_trading()
        self.assertTrue(server.TRADER.can_open(), server.TRADER.status())

    def enter(self):
        server.STATE["lastScan"] = [base.scan_row()]
        server.maybe_open()

    # ------------------------------------------------------------- entries
    def test_demo_entry_lives_on_exchange_with_sl(self):
        self.demo()
        self.enter()
        pos = server.STATE["openPositions"][0]
        self.assertEqual(pos["exchange"], "blofin-demo")
        self.assertEqual(len(self.fake.orders), 1)
        order = next(iter(self.fake.orders.values()))
        self.assertEqual(pos["entryOrderId"], order["orderId"])
        self.assertEqual(pos["contracts"], float(order["filledSize"]))
        self.assertEqual(pos["sl"], 598.14)
        self.assertTrue(self.fake.tpsl and self.fake.tpsl[0]["slTriggerPrice"] == "598.14")
        self.assertLessEqual(pos["contracts"] * 0.01 * (pos["entry"] - pos["sl"]), 10.23 * 1.02)
        # Same signal again: never a second order.
        server.STATE["openPositions"] = []
        self.enter()
        self.assertEqual(len(self.fake.posts("/api/v1/trade/order")), 1)

    def test_exchange_stop_is_settled_with_real_fills_and_journal(self):
        self.demo()
        self.enter()
        self.fake.trigger_stop(PAIR)
        server.trading_cycle(force_reconcile=True)
        self.assertEqual(server.STATE["openPositions"], [])
        hist = server.STATE["history"][0]
        self.assertEqual(hist["reason"], "SL (exchange)")
        self.assertAlmostEqual(hist["exit"], 598.14)
        self.assertTrue(hist["exitOrderId"] and hist["exchangeTradeIds"])
        self.assertAlmostEqual(hist["fundingUsdt"], -0.05)
        self.assertLess(hist["pnl"], 0)
        self.assertEqual(server.STATE["balanceSource"], "blofin-demo")
        db = sqlite3.connect(str(Path(self.tmp.name) / "fiscal_journal_demo.sqlite"))
        cols = [d[1] for d in db.execute("PRAGMA table_info(trades)")]
        row = dict(zip(cols, db.execute("SELECT * FROM trades").fetchone()))
        db.close()
        self.assertEqual(row["mode"], "demo")
        self.assertTrue(server.TRADER.safe)

    def test_manual_close_is_queued_then_executed_on_exchange(self):
        self.demo()
        self.enter()
        r = self.client.post("/api/close_position", json={"pair": PAIR})
        self.assertEqual(r.status_code, 202)
        self.assertTrue(server.STATE["openPositions"][0]["closeRequested"])
        server.trading_cycle()
        self.assertEqual(len(self.fake.posts("/api/v1/trade/close-position")), 1)
        self.assertEqual(server.STATE["openPositions"], [])
        self.assertEqual(server.STATE["history"][0]["reason"], "manual")
        self.assertFalse([t for t in self.fake.tpsl if t["state"] == "live"])

    def test_orphan_on_exchange_blocks_new_entries(self):
        self.demo()
        self.fake.positions["BTC-USDT"] = {"positions": 1, "averagePrice": 95000}
        server.trading_cycle(force_reconcile=True)
        self.assertFalse(server.TRADER.can_open())
        self.enter()
        self.assertEqual(self.fake.posts("/api/v1/trade/order"), [])
        self.assertFalse(server.health()["ok"])

    def test_missing_sl_is_restored_by_reconcile(self):
        self.demo()
        self.enter()
        server.TRADER.cancel_tpsl(PAIR)
        server.trading_cycle(force_reconcile=True)
        self.assertEqual(len(self.fake.posts("/api/v1/trade/order-tpsl")), 1)
        self.assertTrue([t for t in self.fake.tpsl if t["state"] == "live"])

    # ---------------------------------------------------------- kill switch
    def test_kill_switch_demo_blocks_and_closes(self):
        self.demo()
        self.enter()
        r = self.client.post("/api/kill_switch", json={"enabled": True, "closeAll": True})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["closeRequested"], [PAIR])
        server.trading_cycle()
        self.assertEqual(server.STATE["openPositions"], [])
        server.STATE["lastScan"] = [base.scan_row(pair="BTC-USDT")]
        server.maybe_open()
        self.assertEqual(len(self.fake.posts("/api/v1/trade/order")), 1)
        self.assertTrue(server.public_state()["killSwitch"])

    def test_kill_switch_paper_closes_and_blocks(self):
        server.init_trading({})
        self.enter()
        self.assertEqual(len(server.STATE["openPositions"]), 1)
        r = self.client.post("/api/kill_switch", json={"enabled": True, "closeAll": True})
        self.assertEqual(r.get_json()["closedPaper"], [PAIR])
        self.assertEqual(server.STATE["history"][0]["reason"], "kill_switch")
        server.STATE["lastScan"] = [base.scan_row(pair="BTC-USDT")]
        server.maybe_open()
        self.assertEqual(server.STATE["openPositions"], [])
        self.client.post("/api/kill_switch", json={"enabled": False})
        self.assertFalse(server.STATE["killSwitch"])

    def test_kill_switch_option_at_startup(self):
        server.init_trading({"KILL_SWITCH": "true"})
        self.assertTrue(server.STATE["killSwitch"])

    # ----------------------------------------------------------- live block
    def test_live_mode_is_blocked_in_code(self):
        self.assertEqual(server.init_trading({"TRADING_MODE": "live", **{k: v for k, v in DEMO_ENV.items() if k != "TRADING_MODE"}}), "blocked")
        self.assertFalse(server.STATE["running"])
        self.assertIsNone(server.TRADER)
        r = self.client.post("/api/start")
        self.assertEqual(r.status_code, 409)
        self.assertEqual(server.health()["mode"], "blocked")
        self.assertIn("real", server.health()["tradingError"].lower())

    def test_demo_without_keys_is_blocked(self):
        self.assertEqual(server.init_trading({"TRADING_MODE": "demo"}), "blocked")
        self.assertEqual(self.client.post("/api/start").status_code, 409)

    def test_paper_ledger_snapshotted_before_demo(self):
        server.STATE["balance"] = 1234.5
        self.demo()
        server.trading_cycle(force_reconcile=True)
        self.assertEqual(server.STATE["paperSnapshot"]["balance"], 1234.5)
        self.assertEqual(server.STATE["balance"], 1000.5)  # demo equity from Blofin

    # ---------------------------------------------------- 1.7.1 ledgers
    def restart(self, env=None):
        """Simulate an add-on restart: persist, reload from disk, init again."""
        persist()
        server.STATE = server.load_state()
        server.STATE["running"] = True
        mode = server.init_trading(env if env is not None else DEMO_ENV, transport=self.fake, sleep=lambda s: None)
        if mode == "demo":
            server.start_trading()
        return mode

    def paper_book(self):
        server.STATE.update(balance=10382.24, initialBalance=10000.0, history=paper_trades(), tradeSeq=8)

    def test_persistent_keys_survive_reload(self):
        server.STATE.update(killSwitch=True, paperSnapshot={"balance": 1.0}, demoLedger={"balance": 2.0},
                            balanceSource="blofin-demo", lastReconcileAt="2026-11-14T20:00:00+01:00",
                            ledgerMode="demo")
        persist()
        loaded = server.load_state()
        for key in ("killSwitch", "paperSnapshot", "demoLedger", "balanceSource", "lastReconcileAt", "ledgerMode"):
            self.assertEqual(loaded[key], server.STATE[key], key)

    def test_kill_switch_from_api_survives_restart(self):
        server.init_trading({})
        self.assertEqual(self.client.post("/api/kill_switch", json={"enabled": True}).status_code, 200)
        self.restart({})
        self.assertTrue(server.STATE["killSwitch"])
        self.assertTrue(server.public_state()["killSwitch"])
        self.restart(DEMO_ENV)  # option kill_switch=false does not clear an API kill switch
        self.assertTrue(server.STATE["killSwitch"])

    def test_demo_has_its_own_book_and_snapshot_is_stable(self):
        self.paper_book()
        self.demo()
        self.assertEqual(server.STATE["history"], [])
        self.assertEqual(server.STATE["initialBalance"], 1000.5)  # first Blofin balance
        self.assertEqual(server.STATE["balance"], 1000.5)
        snap = copy.deepcopy(server.STATE["paperSnapshot"])
        self.assertEqual(snap["balance"], 10382.24)
        self.assertEqual(snap["initialBalance"], 10000.0)
        self.assertEqual(len(snap["history"]), 8)
        self.fake.h_get_account_balance = lambda p, b: {"code": "0", "data": {"totalEquity": "999.28"}}
        for _ in range(2):
            self.assertEqual(self.restart(), "demo")
            self.assertEqual(server.STATE["paperSnapshot"], snap)
            self.assertEqual(server.STATE["initialBalance"], 1000.5)
            self.assertEqual(server.STATE["balance"], 999.28)
            self.assertEqual(server.STATE["history"], [])
        state = self.client.get("/api/state").get_json()
        self.assertEqual((state["initialBalance"], state["history"], state["tradingMode"]), (1000.5, [], "demo"))
        self.assertEqual(state["paperSnapshot"]["trades"], 8)
        self.assertNotIn("history", state["paperSnapshot"])
        # Back to paper: the paper book comes back intact.
        self.assertEqual(self.restart({}), "paper")
        self.assertEqual((server.STATE["balance"], server.STATE["initialBalance"]), (10382.24, 10000.0))
        self.assertEqual(len(server.STATE["history"]), 8)
        self.assertEqual(server.STATE["balanceSource"], "paper")
        # And demo again resumes its own book.
        self.restart()
        self.assertEqual(server.STATE["initialBalance"], 1000.5)
        self.assertEqual(server.STATE["history"], [])
        self.assertEqual(server.STATE["paperSnapshot"]["balance"], 10382.24)

    def test_demo_trades_stay_in_demo_book(self):
        self.paper_book()
        self.demo()
        self.enter()
        self.fake.trigger_stop(PAIR)
        server.trading_cycle(force_reconcile=True)
        self.assertEqual(len(server.STATE["history"]), 1)
        self.assertEqual(server.STATE["history"][0]["exchange"], "blofin-demo")
        self.restart({})
        self.assertEqual(len(server.STATE["history"]), 8)
        self.assertTrue(all(not h.get("exchange") for h in server.STATE["history"]))
        self.assertEqual(server.STATE["demoLedger"]["history"][0]["exchange"], "blofin-demo")

    def test_migration_rebuilds_paper_book_from_1_7_0_state(self):
        shutil.copy(FIXTURE, server.STATE_FILE)
        server.STATE = server.load_state()
        self.assertEqual(server.STATE["ledgerMode"], "demo")
        self.assertEqual(server.STATE["paperSnapshot"]["balance"], 999.282)  # the 1.7.0 corruption
        self.assertEqual(server.init_trading(DEMO_ENV, transport=self.fake, sleep=lambda s: None), "demo")
        server.start_trading()
        snap = copy.deepcopy(server.STATE["paperSnapshot"])
        self.assertAlmostEqual(snap["balance"], 10382.24, places=2)
        self.assertEqual(snap["initialBalance"], 10000.0)
        self.assertEqual(len(snap["history"]), 8)
        self.assertEqual(snap["tradeSeq"], 8)
        self.assertEqual(server.STATE["history"], [])
        self.assertEqual(server.STATE["initialBalance"], 1000.5)
        for _ in range(2):
            self.restart()
            self.assertEqual(server.STATE["paperSnapshot"], snap)
            self.assertEqual(server.STATE["migrations"].count(server.LEDGER_MIGRATION), 1)
        self.restart({})
        self.assertAlmostEqual(server.STATE["balance"], 10382.24, places=2)
        self.assertEqual(len(server.STATE["history"]), 8)

    def test_migration_when_upgrading_straight_to_paper(self):
        shutil.copy(FIXTURE, server.STATE_FILE)
        server.STATE = server.load_state()
        self.assertEqual(server.init_trading({}), "paper")
        self.assertAlmostEqual(server.STATE["balance"], 10382.24, places=2)
        self.assertEqual(server.STATE["initialBalance"], 10000.0)
        self.assertEqual(len(server.STATE["history"]), 8)

    def test_demo_start_ignores_initial_balance_and_reset_keeps_kill_switch(self):
        self.demo()
        self.client.post("/api/start", json={"initialBalance": 5000})
        self.assertEqual(server.STATE["initialBalance"], 1000.5)
        server.init_trading({})
        server.STATE["killSwitch"] = True
        self.client.post("/api/reset", json={"initialBalance": 2000})
        self.assertTrue(server.STATE["killSwitch"])
        self.assertIsNotNone(server.STATE["demoLedger"])
        self.assertEqual(server.STATE["balance"], 2000)

    def test_panel_shows_mode_safe_state_and_alerts(self):
        html = (Path(server.__file__).parent / "static" / "index.html").read_text(encoding="utf-8")
        for needle in ('id="modeBadge"', 'id="safeBadge"', 'id="tradingAlerts"', "s.tradingMode",
                       "trading.unsafeReasons", '$("btnReset").disabled = demo', '$("initBalance").disabled = demo',
                       "s.initialBalance == null"):
            self.assertIn(needle, html)

    # ---------------------------------------------- 1.7.2 freshness / account
    def test_panel_not_stale_between_monitor_cycles(self):
        # Regression: 1.7.0 lowered the freshness limit to 10 s while the monitor
        # still refreshed every 20 s, so the panel flickered "Sin precio Blofin fresco".
        self.assertLess(server.MONITOR_SECONDS * 2 + server.HTTP_CONNECT_TIMEOUT + server.HTTP_READ_TIMEOUT,
                        server.DISPLAY_MAX_AGE_SECONDS)
        self.enter()
        pos = server.STATE["openPositions"][0]
        for age, stale in ((0, False), (12, False), (25, False), (server.DISPLAY_MAX_AGE_SECONDS + 1, True)):
            pos["lastMarket"] = dict(pos["lastMarket"], ts=int((base.NOW - age) * 1000))
            pos["priceStale"] = False
            self.assertEqual(server.public_state()["openPositions"][0]["priceStale"], stale, age)

    def test_stopped_bot_keeps_prices_fresh_without_acting(self):
        self.enter()
        server.STATE["running"] = False
        pos = server.STATE["openPositions"][0]
        pos["priceStale"] = True
        self.price = 500.0  # below the SL: must NOT close while stopped
        server.monitor_position(display_only=True)
        self.assertEqual(len(server.STATE["openPositions"]), 1)
        self.assertFalse(pos["priceStale"])
        self.assertLess(pos["unrealized"], 0)
        self.assertFalse(server.public_state()["openPositions"][0]["priceStale"])

    def test_demo_account_and_marks_read_from_blofin(self):
        self.demo()
        self.enter()
        self.fake.prices[PAIR] = 620.0
        server.trading_cycle(force_reconcile=True)
        acc = server.STATE["exchangeAccount"]
        mark = server.STATE["openPositions"][0]["exchangeMark"]
        self.assertEqual(mark["markPrice"], 620.0)
        self.assertGreater(mark["unrealizedPnl"], 0)
        # Isolated margin leaves available/balance (real demo: 838 next to 1000.43).
        self.assertGreater(acc["positionsMargin"], 0)
        self.assertAlmostEqual(acc["available"] + acc["positionsMargin"] + mark["unrealizedPnl"], acc["equity"], places=6)
        self.assertAlmostEqual(acc["available"] + acc["positionsMargin"], 1000.5, places=6)
        state = self.client.get("/api/state").get_json()
        self.assertEqual(state["exchangeAccount"]["totalEquity"], acc["totalEquity"])
        d = state["display"]
        self.assertEqual((d["equity"], d["balance"], d["marginUsed"]),
                         (round(acc["equity"], 2), round(acc["available"], 2), round(acc["positionsMargin"], 2)))
        persist()
        self.assertEqual(server.load_state()["exchangeAccount"]["totalEquity"], acc["totalEquity"])

    def test_reset_and_settings_refused_in_demo(self):
        self.demo()
        self.assertEqual(self.client.post("/api/reset").status_code, 409)
        self.assertEqual(self.client.post("/api/settings", json={"riskPct": 2}).status_code, 409)

    # ------------------------------------------------------------- security
    def test_secrets_never_exposed(self):
        self.demo()
        self.enter()
        with self.assertLogs("granmago", level="WARNING") as logs:
            server.log.warning("leak attempt %s %s %s", KEY, SECRET, PASSPHRASE)
        blobs = [json.dumps(server.public_state()), json.dumps(server.health()),
                 self.client.get("/api/state").get_data(as_text=True),
                 self.client.get("/api/health").get_data(as_text=True),
                 repr(server.TRADER), repr(server.TRADING_CONFIG), "\n".join(logs.output),
                 (Path(self.tmp.name) / "trading_intents.jsonl").read_text()]
        for blob in blobs:
            for secret in (KEY, SECRET, PASSPHRASE):
                self.assertNotIn(secret, blob)
        self.assertIn("trading", server.public_state())
        self.assertIn("market", server.public_state())

    def test_control_endpoints_need_ingress_or_token(self):
        for path in ("/api/start", "/api/stop", "/api/reset", "/api/scan", "/api/kill_switch", "/api/close_position",
                     "/api/settings"):
            self.assertEqual(self.client.post(path, json={}, environ_base=REMOTE).status_code, 403, path)
        self.assertEqual(self.client.get("/api/state", environ_base=REMOTE).status_code, 200)
        self.assertEqual(self.client.get("/api/health", environ_base=REMOTE).status_code, 200)
        ingress = {"REMOTE_ADDR": server.INGRESS_IP}
        self.assertEqual(self.client.post("/api/kill_switch", json={"enabled": False}, environ_base=ingress).status_code, 200)
        with patch.dict(os.environ, {"CONTROL_TOKEN": "tok-123456"}):
            r = self.client.post("/api/kill_switch", json={"enabled": False}, environ_base=REMOTE,
                                 headers={"X-GranMago-Token": "tok-123456"})
            self.assertEqual(r.status_code, 200)
            r = self.client.post("/api/kill_switch", json={"enabled": False}, environ_base=REMOTE,
                                 headers={"X-GranMago-Token": "wrong"})
            self.assertEqual(r.status_code, 403)


if __name__ == "__main__":
    unittest.main()
