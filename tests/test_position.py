"""Close, timeout, and one-position rules for GranMago Scanner."""

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="granmago-test-")
os.environ.pop("MAX_HOLD_HOURS", None)
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "granmago"))

import server  # noqa: E402


def _book(entry=180.0, side="short"):
    return {
        "pair": "SOL-USDT",
        "side": side,
        "entry": entry,
        "sl": 200.0 if side == "short" else 170.0,
        "tp": 150.0 if side == "short" else 210.0,
        "notional": 400.0,
        "leverage": 3,
        "riskAmt": 8.0,
        "time": "2026-10-15T12:00:00+00:00",
        "unrealized": 0,
        "price": entry,
    }


def _pnl(pos, price):
    if pos["side"] == "long":
        pct = (price - pos["entry"]) / pos["entry"]
    else:
        pct = (pos["entry"] - price) / pos["entry"]
    return round(pct * pos["notional"], 2)


class PositionTests(unittest.TestCase):
    def setUp(self):
        server.STATE.clear()
        server.STATE.update(server.default_state())
        server.STATE["balance"] = 950.56
        server.STATE["initialBalance"] = 10000
        server.STATE["running"] = True
        server.STATE["history"] = []
        server.STATE["log"] = []
        server.STATE["settings"]["maxHoldHours"] = 48

    def test_manual_close_matches_sl_balance_math(self):
        price = 170.0
        server.STATE["openPosition"] = _book()
        server.close_position("SL", price)
        sl_balance = server.STATE["balance"]
        sl_pnl = server.STATE["history"][0]["pnl"]

        self.setUp()
        server.STATE["openPosition"] = _book()
        server.close_position("manual", price)
        self.assertIsNone(server.STATE["openPosition"])
        self.assertEqual(server.STATE["history"][0]["reason"], "manual")
        self.assertEqual(server.STATE["history"][0]["exit"], price)
        self.assertEqual(server.STATE["history"][0]["pair"], "SOL-USDT")
        self.assertEqual(server.STATE["history"][0]["pnl"], sl_pnl)
        self.assertEqual(server.STATE["balance"], sl_balance)
        self.assertEqual(server.STATE["balance"], round(950.56 + _pnl(_book(), price), 2))

    def test_close_without_position_is_noop(self):
        server.STATE["openPosition"] = None
        server.close_position("manual", 100)
        self.assertEqual(server.STATE["balance"], 950.56)
        self.assertEqual(server.STATE["history"], [])

    def test_api_close_position_records_manual(self):
        server.STATE["openPosition"] = _book()
        original = server.fetch_price
        server.fetch_price = lambda pair: 175.0
        try:
            resp = server.app.test_client().post("/api/close_position")
        finally:
            server.fetch_price = original
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertIsNone(data["openPosition"])
        self.assertEqual(data["history"][0]["reason"], "manual")
        self.assertEqual(data["history"][0]["exit"], 175.0)
        self.assertEqual(data["balance"], round(950.56 + _pnl(_book(), 175.0), 2))
        self.assertIsNone(server.STATE["openPosition"])

    def test_api_close_position_missing(self):
        server.STATE["openPosition"] = None
        resp = server.app.test_client().post("/api/close_position")
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(server.STATE["balance"], 950.56)

    def test_api_close_position_without_price_keeps_position(self):
        server.STATE["openPosition"] = _book()
        original = server.fetch_price
        server.fetch_price = lambda pair: None
        try:
            resp = server.app.test_client().post("/api/close_position")
        finally:
            server.fetch_price = original
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(server.STATE["openPosition"]["pair"], "SOL-USDT")
        self.assertEqual(server.STATE["balance"], 950.56)
        self.assertEqual(server.STATE["history"], [])

    def test_hold_boundary_and_disabled(self):
        now = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)
        pos = {"time": (now - timedelta(hours=48)).isoformat()}
        self.assertFalse(server.position_hold_expired(pos, now=now))
        pos["time"] = (now - timedelta(hours=48, seconds=1)).isoformat()
        self.assertTrue(server.position_hold_expired(pos, now=now))
        server.STATE["settings"]["maxHoldHours"] = 0
        self.assertFalse(server.position_hold_expired(pos, now=now))
        server.STATE["settings"]["maxHoldHours"] = 48
        self.assertFalse(server.position_hold_expired({}, now=now))
        self.assertFalse(server.position_hold_expired({"time": "nope"}, now=now))

    def test_timeout_closes_at_market_like_sl(self):
        opened = (datetime.now(timezone.utc) - timedelta(hours=49)).astimezone().isoformat(timespec="seconds")
        pos = _book()
        pos["time"] = opened
        server.STATE["openPosition"] = pos
        price = 175.0
        original = server.fetch_price
        server.fetch_price = lambda pair: price
        try:
            server.monitor_position()
        finally:
            server.fetch_price = original
        self.assertIsNone(server.STATE["openPosition"])
        self.assertEqual(server.STATE["history"][0]["reason"], "timeout")
        self.assertEqual(server.STATE["history"][0]["exit"], price)
        self.assertEqual(server.STATE["balance"], round(950.56 + _pnl(_book(), price), 2))

    def test_sl_wins_over_timeout(self):
        opened = (datetime.now(timezone.utc) - timedelta(hours=80)).astimezone().isoformat(timespec="seconds")
        pos = _book()
        pos["time"] = opened
        server.STATE["openPosition"] = pos
        # Short SL is 200. A price through the stop must stay an SL exit.
        original = server.fetch_price
        server.fetch_price = lambda pair: 205.0
        try:
            server.monitor_position()
        finally:
            server.fetch_price = original
        self.assertEqual(server.STATE["history"][0]["reason"], "SL")
        self.assertIsNone(server.STATE["openPosition"])

    def test_young_position_is_not_timed_out(self):
        opened = (datetime.now(timezone.utc) - timedelta(hours=10)).astimezone().isoformat(timespec="seconds")
        pos = _book()
        pos["time"] = opened
        server.STATE["openPosition"] = pos
        original = server.fetch_price
        server.fetch_price = lambda pair: 175.0
        try:
            server.monitor_position()
        finally:
            server.fetch_price = original
        self.assertEqual(server.STATE["openPosition"]["pair"], "SOL-USDT")
        self.assertEqual(server.STATE["history"], [])
        self.assertEqual(server.STATE["balance"], 950.56)
        self.assertEqual(server.STATE["openPosition"]["price"], 175.0)

    def test_zero_max_hold_does_not_close_old_position(self):
        opened = (datetime.now(timezone.utc) - timedelta(days=9)).astimezone().isoformat(timespec="seconds")
        pos = _book()
        pos["time"] = opened
        server.STATE["openPosition"] = pos
        server.STATE["settings"]["maxHoldHours"] = 0
        original = server.fetch_price
        server.fetch_price = lambda pair: 175.0
        try:
            server.monitor_position()
        finally:
            server.fetch_price = original
        self.assertIsNotNone(server.STATE["openPosition"])
        self.assertEqual(server.STATE["history"], [])

    def test_maybe_open_does_not_stack_a_second_position(self):
        pos = _book()
        server.STATE["openPosition"] = pos
        server.STATE["bestLong"] = {
            "pair": "BTC-USDT",
            "price": 100.0,
            "scoreLong": 95,
            "recentLow": 98.0,
            "scoreShort": 0,
        }
        server.maybe_open()
        self.assertIs(server.STATE["openPosition"], pos)
        self.assertEqual(server.STATE["balance"], 950.56)
        self.assertEqual(server.STATE["history"], [])

    def test_maybe_open_still_uses_threshold_when_flat(self):
        server.STATE["openPosition"] = None
        server.STATE["bestLong"] = {
            "pair": "ETH-USDT",
            "price": 100.0,
            "scoreLong": 74,
            "recentLow": 98.0,
            "scoreShort": 0,
        }
        server.STATE["bestShort"] = None
        server.maybe_open()
        self.assertIsNone(server.STATE["openPosition"])

        server.STATE["bestLong"]["scoreLong"] = 75
        server.maybe_open()
        self.assertEqual(server.STATE["openPosition"]["pair"], "ETH-USDT")
        self.assertEqual(server.STATE["openPosition"]["side"], "long")
        self.assertEqual(server.STATE["balance"], 950.56)

    def test_next_actions_scan_while_position_open(self):
        self.assertEqual(
            server.next_actions(True, True),
            {"scan": True, "monitor": True},
        )
        self.assertEqual(
            server.next_actions(True, False),
            {"scan": True, "monitor": False},
        )
        self.assertEqual(
            server.next_actions(False, True),
            {"scan": False, "monitor": False},
        )

    def test_load_state_keeps_book_when_new_fields_missing(self):
        os.environ.pop("MAX_HOLD_HOURS", None)
        payload = {
            "running": True,
            "balance": 950.56,
            "initialBalance": 10000,
            "openPosition": {
                "pair": "SOL-USDT",
                "side": "short",
                "entry": 180.25,
                "sl": 190.0,
                "tp": 160.0,
                "notional": 512.4,
                "time": "2026-10-15T12:00:00+00:00",
                "note": "keep-me",
            },
            "history": [
                {
                    "time": "2026-10-01 10:00",
                    "pair": "BTC-USDT",
                    "side": "long",
                    "entry": 60000,
                    "exit": 59000,
                    "pnl": -12.34,
                    "reason": "SL",
                }
            ],
            "lastScan": [{"pair": "ETH-USDT", "scoreLong": 40}],
            "settings": {"riskPct": 2, "leverage": 3, "scanSeconds": 90},
            "log": ["[12:00:00] anterior"],
        }
        server.STATE_FILE.write_text(json.dumps(payload), encoding="utf-8")
        loaded = server.load_state()
        self.assertEqual(loaded["balance"], 950.56)
        self.assertEqual(loaded["initialBalance"], 10000)
        self.assertTrue(loaded["running"])
        self.assertEqual(loaded["openPosition"]["pair"], "SOL-USDT")
        self.assertEqual(loaded["openPosition"]["side"], "short")
        self.assertEqual(loaded["openPosition"]["entry"], 180.25)
        self.assertEqual(loaded["openPosition"]["notional"], 512.4)
        self.assertEqual(loaded["openPosition"]["note"], "keep-me")
        self.assertEqual(loaded["history"][0]["pnl"], -12.34)
        self.assertEqual(loaded["history"][0]["reason"], "SL")
        self.assertEqual(loaded["settings"]["scanSeconds"], 90)
        self.assertEqual(loaded["settings"]["riskPct"], 2)
        self.assertEqual(loaded["settings"]["maxHoldHours"], 48)
        self.assertEqual(loaded["lastScan"][0]["pair"], "ETH-USDT")

    def test_addon_option_overrides_missing_or_stale_hold(self):
        os.environ["MAX_HOLD_HOURS"] = "12"
        try:
            payload = {
                "balance": 950.56,
                "openPosition": {"pair": "SOL-USDT", "side": "short"},
                "history": [{"pnl": 1, "reason": "TP"}],
                "settings": {"maxHoldHours": 3, "scanSeconds": 90},
            }
            server.STATE_FILE.write_text(json.dumps(payload), encoding="utf-8")
            loaded = server.load_state()
        finally:
            os.environ.pop("MAX_HOLD_HOURS", None)
        self.assertEqual(loaded["balance"], 950.56)
        self.assertEqual(loaded["openPosition"]["pair"], "SOL-USDT")
        self.assertEqual(len(loaded["history"]), 1)
        self.assertEqual(loaded["settings"]["maxHoldHours"], 12)
        self.assertEqual(loaded["settings"]["scanSeconds"], 90)

    def test_saved_zero_hold_survives_when_option_unset(self):
        os.environ.pop("MAX_HOLD_HOURS", None)
        payload = {
            "balance": 950.56,
            "history": [],
            "settings": {"maxHoldHours": 0, "riskPct": 2, "leverage": 3, "scanSeconds": 90},
        }
        server.STATE_FILE.write_text(json.dumps(payload), encoding="utf-8")
        loaded = server.load_state()
        self.assertEqual(loaded["balance"], 950.56)
        self.assertEqual(loaded["settings"]["maxHoldHours"], 0)


if __name__ == "__main__":
    unittest.main()
