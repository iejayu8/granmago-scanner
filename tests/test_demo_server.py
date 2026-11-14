"""Server integration of the Blofin Demo layer: fake exchange, mocked public HTTP."""
import json
import os
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
