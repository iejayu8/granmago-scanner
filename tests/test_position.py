"""Manual close, trailing stop, and one-position rules for GranMago Scanner."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="granmago-test-")
os.environ.pop("MAX_HOLD_HOURS", None)
os.environ.pop("TRAILING_ENABLED", None)
os.environ.pop("TRAILING_ACTIVATION_PCT", None)
os.environ.pop("TRAILING_DISTANCE", None)
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "granmago"))

import server  # noqa: E402


def _book(entry=180.0, side="short", notional=400.0):
    if side == "short":
        sl, tp = 200.0, 150.0
    else:
        sl, tp = 170.0, 210.0
    return {
        "pair": "SOL-USDT",
        "side": side,
        "entry": entry,
        "sl": sl,
        "tp": tp,
        "notional": notional,
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


def _tick(price):
    original = server.fetch_price
    server.fetch_price = lambda pair: price
    try:
        server.monitor_position()
    finally:
        server.fetch_price = original


def _arm(entry, side, sl, tp, notional=1000.0):
    pos = {
        "pair": "SOL-USDT",
        "side": side,
        "entry": entry,
        "sl": sl,
        "tp": tp,
        "notional": notional,
        "price": entry,
        "unrealized": 0,
        "time": "2026-10-15T12:00:00+00:00",
    }
    server.STATE["openPosition"] = pos
    server.STATE["history"] = []
    return pos


class PositionTests(unittest.TestCase):
    def setUp(self):
        server.STATE.clear()
        server.STATE.update(server.default_state())
        server.STATE["balance"] = 950.56
        server.STATE["initialBalance"] = 10000
        server.STATE["running"] = True
        server.STATE["history"] = []
        server.STATE["log"] = []
        server.STATE["settings"]["trailingEnabled"] = True
        server.STATE["settings"]["trailingActivationPct"] = 1.0
        server.STATE["settings"]["trailingDistance"] = 0.5

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

    def test_activation_is_percent_of_notional(self):
        # 1% of 1000 notional is 10 USDT. Entry 100 needs a 1.00 move, not a
        # fraction of the distance to the target at 117.
        _arm(100.0, "long", 90.0, 117.0, notional=1000.0)
        _tick(100.5)
        pos = server.STATE["openPosition"]
        self.assertFalse(pos["trailingActive"])
        self.assertEqual(pos["sl"], 90.0)
        self.assertEqual(pos["tp"], 117.0)

        _tick(101.0)
        pos = server.STATE["openPosition"]
        self.assertTrue(pos["trailingActive"])
        self.assertIsNone(pos["tp"])
        self.assertAlmostEqual(pos["sl"], 100.0)
        self.assertIsNotNone(server.STATE["openPosition"])

        # Same 1.00 move on a 100 notional is also exactly 1%. A 0.99 move is not.
        self.setUp()
        _arm(100.0, "long", 90.0, 117.0, notional=100.0)
        _tick(100.99)
        self.assertFalse(server.STATE["openPosition"]["trailingActive"])
        _tick(101.0)
        self.assertTrue(server.STATE["openPosition"]["trailingActive"])

    def test_long_trail_replaces_sl_ignores_tp_and_never_loosens(self):
        _arm(100.0, "long", 90.0, 117.0, notional=1000.0)
        _tick(110.0)
        pos = server.STATE["openPosition"]
        self.assertTrue(pos["trailingActive"])
        self.assertIsNone(pos["tp"])
        self.assertEqual(pos["initialTp"], 117.0)
        self.assertEqual(pos["initialSl"], 90.0)
        # dist = 0.5 * 10 = 5; stop = max(100, 110 - 5) = 105. Original SL is gone.
        self.assertAlmostEqual(pos["sl"], 105.0)
        self.assertGreater(pos["sl"], 90.0)
        self.assertGreaterEqual(pos["sl"], 100.0)

        # Past the old target. The trade stays open and the stop only tightens.
        _tick(130.0)
        pos = server.STATE["openPosition"]
        self.assertIsNotNone(pos)
        self.assertEqual(server.STATE["history"], [])
        self.assertAlmostEqual(pos["bestPrice"], 130.0)
        self.assertAlmostEqual(pos["sl"], 125.0)

        # Pullback above the trail must not drag the stop down.
        _tick(128.0)
        pos = server.STATE["openPosition"]
        self.assertIsNotNone(pos)
        self.assertAlmostEqual(pos["bestPrice"], 130.0)
        self.assertAlmostEqual(pos["sl"], 125.0)

        _tick(125.0)
        self.assertIsNone(server.STATE["openPosition"])
        self.assertEqual(server.STATE["history"][0]["reason"], "trailing")
        self.assertEqual(server.STATE["history"][0]["exit"], 125.0)

    def test_short_breakeven_floor_and_tightening(self):
        _arm(100.0, "short", 110.0, 83.0, notional=2000.0)
        # 1% of 2000 is 20 USDT, which is a move to 99. Just under stays fixed.
        _tick(99.1)
        pos = server.STATE["openPosition"]
        self.assertFalse(pos["trailingActive"])
        self.assertEqual(pos["sl"], 110.0)
        self.assertEqual(pos["tp"], 83.0)

        _tick(99.0)
        pos = server.STATE["openPosition"]
        self.assertTrue(pos["trailingActive"])
        self.assertIsNone(pos["tp"])
        # dist = 5; best + dist = 104, floored to breakeven 100. Not the old SL 110.
        self.assertAlmostEqual(pos["sl"], 100.0)
        self.assertLess(pos["sl"], 110.0)
        self.assertIsNotNone(server.STATE["openPosition"])

        _tick(90.0)
        pos = server.STATE["openPosition"]
        self.assertAlmostEqual(pos["bestPrice"], 90.0)
        self.assertAlmostEqual(pos["sl"], 95.0)

        # Bounce must not loosen the stop back up.
        _tick(92.0)
        pos = server.STATE["openPosition"]
        self.assertIsNotNone(pos)
        self.assertAlmostEqual(pos["bestPrice"], 90.0)
        self.assertAlmostEqual(pos["sl"], 95.0)

        # Through the old target (83) but still beyond the trail: no TP exit.
        _tick(80.0)
        pos = server.STATE["openPosition"]
        self.assertIsNotNone(pos)
        self.assertEqual(server.STATE["history"], [])
        self.assertAlmostEqual(pos["sl"], 85.0)

        _tick(85.0)
        self.assertIsNone(server.STATE["openPosition"])
        self.assertEqual(server.STATE["history"][0]["reason"], "trailing")

    def test_fixed_sl_and_tp_before_activation(self):
        _arm(102.48, "short", 109.07, 91.27, notional=1000.0)
        _tick(109.07)
        self.assertIsNone(server.STATE["openPosition"])
        self.assertEqual(server.STATE["history"][0]["reason"], "SL")

        self.setUp()
        _arm(102.48, "short", 109.07, 91.27, notional=1000.0)
        _tick(91.27)
        self.assertIsNone(server.STATE["openPosition"])
        self.assertEqual(server.STATE["history"][0]["reason"], "TP")

    def test_trailing_disabled_matches_fixed_stops(self):
        server.STATE["settings"]["trailingEnabled"] = False
        _arm(100.0, "long", 90.0, 117.0, notional=1000.0)
        _tick(110.0)
        pos = server.STATE["openPosition"]
        self.assertIsNotNone(pos)
        self.assertFalse(pos["trailingActive"])
        self.assertEqual(pos["sl"], 90.0)
        self.assertEqual(pos["tp"], 117.0)
        self.assertEqual(server.STATE["history"], [])

        _tick(117.0)
        self.assertIsNone(server.STATE["openPosition"])
        self.assertEqual(server.STATE["history"][0]["reason"], "TP")

        self.setUp()
        server.STATE["settings"]["trailingEnabled"] = False
        _arm(100.0, "long", 90.0, 117.0, notional=1000.0)
        _tick(90.0)
        self.assertEqual(server.STATE["history"][0]["reason"], "SL")

    def test_legacy_sol_short_is_not_closed_by_the_update(self):
        # The 1.0.0 book: no trailing fields, open for days, price still
        # between the original stop and target.
        pos = {
            "pair": "SOL-USDT",
            "side": "short",
            "entry": 102.48,
            "sl": 109.07,
            "tp": 91.27,
            "notional": 1024.8,
            "time": "2026-10-15T12:00:00+00:00",
        }
        server.STATE["openPosition"] = pos
        server.STATE["settings"]["maxHoldHours"] = 48
        loaded_fields = dict(pos)
        server.ensure_position_fields(server.STATE["openPosition"])
        self.assertIs(server.STATE["openPosition"], pos)
        self.assertEqual(pos["sl"], 109.07)
        self.assertEqual(pos["tp"], 91.27)
        self.assertEqual(pos["initialSl"], 109.07)
        self.assertEqual(pos["initialTp"], 91.27)
        self.assertFalse(pos["trailingActive"])
        self.assertAlmostEqual(pos["bestPrice"], 102.48)
        self.assertEqual(pos["notional"], loaded_fields["notional"])

        _tick(103.04)
        pos = server.STATE["openPosition"]
        self.assertIsNotNone(pos)
        self.assertEqual(server.STATE["history"], [])
        self.assertEqual(server.STATE["balance"], 950.56)
        self.assertFalse(pos["trailingActive"])
        self.assertEqual(pos["sl"], 109.07)
        self.assertEqual(pos["tp"], 91.27)
        self.assertAlmostEqual(pos["bestPrice"], 102.48)

        # Once the short is far enough in profit the old target no longer caps it.
        _tick(101.0)
        pos = server.STATE["openPosition"]
        self.assertTrue(pos["trailingActive"])
        self.assertIsNone(pos["tp"])
        self.assertAlmostEqual(pos["sl"], 102.48)
        _tick(91.27)
        self.assertIsNotNone(server.STATE["openPosition"])
        self.assertEqual(server.STATE["history"], [])
        self.assertLess(server.STATE["openPosition"]["sl"], 102.48)

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
        opened = server.STATE["openPosition"]
        self.assertEqual(opened["pair"], "ETH-USDT")
        self.assertEqual(opened["side"], "long")
        self.assertEqual(opened["initialSl"], opened["sl"])
        self.assertEqual(opened["initialTp"], opened["tp"])
        self.assertFalse(opened["trailingActive"])
        self.assertEqual(opened["bestPrice"], opened["entry"])
        self.assertEqual(server.STATE["balance"], 950.56)

    def test_next_actions_scan_while_position_open(self):
        self.assertEqual(server.next_actions(True, True), {"scan": True, "monitor": True})
        self.assertEqual(server.next_actions(True, False), {"scan": True, "monitor": False})
        self.assertEqual(server.next_actions(False, True), {"scan": False, "monitor": False})

    def test_load_state_keeps_sol_short_and_ignores_max_hold(self):
        os.environ.pop("MAX_HOLD_HOURS", None)
        os.environ["MAX_HOLD_HOURS"] = "1"
        try:
            payload = {
                "running": True,
                "balance": 950.56,
                "initialBalance": 10000,
                "openPosition": {
                    "pair": "SOL-USDT",
                    "side": "short",
                    "entry": 102.48,
                    "sl": 109.07,
                    "tp": 91.27,
                    "notional": 1024.8,
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
                "settings": {
                    "riskPct": 2,
                    "leverage": 3,
                    "scanSeconds": 90,
                    "maxHoldHours": 48,
                },
                "log": ["[12:00:00] anterior"],
            }
            server.STATE_FILE.write_text(json.dumps(payload), encoding="utf-8")
            loaded = server.load_state()
        finally:
            os.environ.pop("MAX_HOLD_HOURS", None)
        self.assertEqual(loaded["balance"], 950.56)
        self.assertTrue(loaded["running"])
        pos = loaded["openPosition"]
        self.assertEqual(pos["pair"], "SOL-USDT")
        self.assertEqual(pos["side"], "short")
        self.assertEqual(pos["entry"], 102.48)
        self.assertEqual(pos["sl"], 109.07)
        self.assertEqual(pos["tp"], 91.27)
        self.assertEqual(pos["notional"], 1024.8)
        self.assertEqual(pos["note"], "keep-me")
        self.assertEqual(pos["initialSl"], 109.07)
        self.assertFalse(pos["trailingActive"])
        self.assertAlmostEqual(pos["bestPrice"], 102.48)
        self.assertEqual(loaded["history"][0]["pnl"], -12.34)
        self.assertNotIn("maxHoldHours", loaded["settings"])
        self.assertEqual(loaded["settings"]["scanSeconds"], 90)
        self.assertTrue(loaded["settings"]["trailingEnabled"])
        self.assertEqual(loaded["settings"]["trailingActivationPct"], 1.0)
        self.assertEqual(loaded["settings"]["trailingDistance"], 0.5)

    def test_trailing_option_from_env_overrides_saved_settings(self):
        os.environ["TRAILING_ENABLED"] = "false"
        os.environ["TRAILING_ACTIVATION_PCT"] = "2.5"
        os.environ["TRAILING_DISTANCE"] = "0.25"
        try:
            payload = {
                "balance": 950.56,
                "openPosition": None,
                "history": [],
                "settings": {
                    "trailingEnabled": True,
                    "trailingActivationPct": 1.0,
                    "trailingDistance": 0.5,
                    "maxHoldHours": 12,
                },
            }
            server.STATE_FILE.write_text(json.dumps(payload), encoding="utf-8")
            loaded = server.load_state()
        finally:
            os.environ.pop("TRAILING_ENABLED", None)
            os.environ.pop("TRAILING_ACTIVATION_PCT", None)
            os.environ.pop("TRAILING_DISTANCE", None)
        self.assertFalse(loaded["settings"]["trailingEnabled"])
        self.assertEqual(loaded["settings"]["trailingActivationPct"], 2.5)
        self.assertEqual(loaded["settings"]["trailingDistance"], 0.25)
        self.assertNotIn("maxHoldHours", loaded["settings"])
        self.assertEqual(loaded["balance"], 950.56)


if __name__ == "__main__":
    unittest.main()
