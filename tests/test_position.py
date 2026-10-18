"""Manual close, trailing stop, and multi-position rules for GranMago Scanner."""

import json
import math
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
os.environ.pop("MAX_POSITIONS", None)
os.environ.pop("ENTRY_THRESHOLD", None)
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "granmago"))

import server  # noqa: E402


def _book(entry=180.0, side="short", notional=400.0, pair="SOL-USDT"):
    if side == "short":
        sl, tp = 200.0, 150.0
    else:
        sl, tp = 170.0, 210.0
    return {
        "pair": pair,
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

    def fake(pair):
        if isinstance(price, dict):
            return price.get(pair)
        return price

    server.fetch_price = fake
    try:
        server.monitor_position()
    finally:
        server.fetch_price = original


def _arm(entry, side, sl, tp, notional=1000.0, pair="SOL-USDT"):
    pos = {
        "pair": pair,
        "side": side,
        "entry": entry,
        "sl": sl,
        "tp": tp,
        "notional": notional,
        "price": entry,
        "unrealized": 0,
        "time": "2026-10-15T12:00:00+00:00",
    }
    server.STATE["openPositions"] = [pos]
    server.STATE["history"] = []
    return pos


def _signal(pair, side, score, price=100.0, tight=False):
    # A tight stop sits inside the 0.4% band, so sizing uses the 1.2% floor.
    # recentLow == price is too close to that boundary for a reliable compare.
    if tight:
        recent_low = price * 1.001
        recent_high = price * 0.999
    else:
        recent_low = price * 0.98
        recent_high = price * 1.02
    return {
        "pair": pair,
        "price": price,
        "scoreLong": score if side == "long" else 0,
        "scoreShort": score if side == "short" else 0,
        "recentLow": recent_low,
        "recentHigh": recent_high,
        "bias": "ALCISTA" if side == "long" else "BAJISTA",
    }


def _one():
    positions = server.STATE["openPositions"]
    if len(positions) != 1:
        raise AssertionError("expected one position, got %r" % positions)
    return positions[0]


class PositionTests(unittest.TestCase):
    def setUp(self):
        os.environ.pop("MAX_POSITIONS", None)
        os.environ.pop("ENTRY_THRESHOLD", None)
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
        server.STATE["settings"]["maxPositions"] = 5
        server.STATE["settings"]["entryThreshold"] = 100

    def test_manual_close_matches_sl_balance_math(self):
        price = 170.0
        server.STATE["openPositions"] = [_book()]
        server.close_position("SL", price)
        sl_balance = server.STATE["balance"]
        sl_pnl = server.STATE["history"][0]["pnl"]

        self.setUp()
        server.STATE["openPositions"] = [_book()]
        server.close_position("manual", price, "SOL-USDT")
        self.assertEqual(server.STATE["openPositions"], [])
        self.assertEqual(server.STATE["history"][0]["reason"], "manual")
        self.assertEqual(server.STATE["history"][0]["exit"], price)
        self.assertEqual(server.STATE["history"][0]["pair"], "SOL-USDT")
        self.assertEqual(server.STATE["history"][0]["pnl"], sl_pnl)
        self.assertEqual(server.STATE["balance"], sl_balance)
        self.assertEqual(server.STATE["balance"], round(950.56 + _pnl(_book(), price), 2))

    def test_close_without_position_is_noop(self):
        server.STATE["openPositions"] = []
        server.close_position("manual", 100, "SOL-USDT")
        self.assertEqual(server.STATE["balance"], 950.56)
        self.assertEqual(server.STATE["history"], [])

    def test_api_close_position_records_manual(self):
        server.STATE["openPositions"] = [_book()]
        original = server.fetch_price
        server.fetch_price = lambda pair: 175.0
        try:
            resp = server.app.test_client().post("/api/close_position", json={"pair": "SOL-USDT"})
        finally:
            server.fetch_price = original
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["openPositions"], [])
        self.assertNotIn("openPosition", data)
        self.assertEqual(data["history"][0]["reason"], "manual")
        self.assertEqual(data["history"][0]["exit"], 175.0)
        self.assertEqual(data["balance"], round(950.56 + _pnl(_book(), 175.0), 2))

    def test_api_close_requires_the_pair(self):
        server.STATE["openPositions"] = [_book()]
        resp = server.app.test_client().post("/api/close_position", json={})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["error"], "falta_par")
        self.assertEqual(server.STATE["openPositions"][0]["pair"], "SOL-USDT")
        self.assertEqual(server.STATE["balance"], 950.56)

    def test_api_close_position_missing(self):
        server.STATE["openPositions"] = []
        resp = server.app.test_client().post("/api/close_position", json={"pair": "SOL-USDT"})
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(server.STATE["balance"], 950.56)

    def test_api_close_position_without_price_keeps_position(self):
        server.STATE["openPositions"] = [_book()]
        original = server.fetch_price
        server.fetch_price = lambda pair: None
        try:
            resp = server.app.test_client().post("/api/close_position", json={"pair": "SOL-USDT"})
        finally:
            server.fetch_price = original
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(server.STATE["openPositions"][0]["pair"], "SOL-USDT")
        self.assertEqual(server.STATE["balance"], 950.56)
        self.assertEqual(server.STATE["history"], [])

    def test_manual_close_by_pair_leaves_the_other(self):
        sol = _book()
        eth = _book(entry=2413.52, side="short", notional=800.0, pair="ETH-USDT")
        eth["sl"] = 2580.0
        eth["tp"] = 2130.0
        server.STATE["openPositions"] = [sol, eth]
        original = server.fetch_price

        def fake(pair):
            return {"SOL-USDT": 170.0, "ETH-USDT": 2400.0}.get(pair)

        server.fetch_price = fake
        try:
            wrong = server.app.test_client().post("/api/close_position", json={"pair": "BTC-USDT"})
            closed = server.app.test_client().post("/api/close_position", json={"pair": "SOL-USDT"})
        finally:
            server.fetch_price = original
        self.assertEqual(wrong.status_code, 409)
        self.assertEqual(closed.status_code, 200)
        data = closed.get_json()
        self.assertEqual([p["pair"] for p in data["openPositions"]], ["ETH-USDT"])
        self.assertEqual(data["openPositions"][0]["entry"], 2413.52)
        self.assertEqual(data["openPositions"][0]["notional"], 800.0)
        self.assertEqual(data["history"][0]["pair"], "SOL-USDT")
        self.assertEqual(data["history"][0]["reason"], "manual")
        self.assertEqual(data["balance"], round(950.56 + _pnl(sol, 170.0), 2))

    def test_activation_is_percent_of_notional(self):
        # 1% of 1000 notional is 10 USDT. Entry 100 needs a 1.00 move, not a
        # fraction of the distance to the target at 117.
        _arm(100.0, "long", 90.0, 117.0, notional=1000.0)
        _tick(100.5)
        pos = _one()
        self.assertFalse(pos["trailingActive"])
        self.assertEqual(pos["sl"], 90.0)
        self.assertEqual(pos["tp"], 117.0)

        _tick(101.0)
        pos = _one()
        self.assertTrue(pos["trailingActive"])
        self.assertIsNone(pos["tp"])
        self.assertAlmostEqual(pos["sl"], 100.0)

        # Same 1.00 move on a 100 notional is also exactly 1%. A 0.99 move is not.
        self.setUp()
        _arm(100.0, "long", 90.0, 117.0, notional=100.0)
        _tick(100.99)
        self.assertFalse(_one()["trailingActive"])
        _tick(101.0)
        self.assertTrue(_one()["trailingActive"])

    def test_long_trail_replaces_sl_ignores_tp_and_never_loosens(self):
        _arm(100.0, "long", 90.0, 117.0, notional=1000.0)
        _tick(110.0)
        pos = _one()
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
        pos = _one()
        self.assertEqual(server.STATE["history"], [])
        self.assertAlmostEqual(pos["bestPrice"], 130.0)
        self.assertAlmostEqual(pos["sl"], 125.0)

        # Pullback above the trail must not drag the stop down.
        _tick(128.0)
        pos = _one()
        self.assertAlmostEqual(pos["bestPrice"], 130.0)
        self.assertAlmostEqual(pos["sl"], 125.0)

        _tick(125.0)
        self.assertEqual(server.STATE["openPositions"], [])
        self.assertEqual(server.STATE["history"][0]["reason"], "trailing")
        self.assertEqual(server.STATE["history"][0]["exit"], 125.0)

    def test_short_breakeven_floor_and_tightening(self):
        _arm(100.0, "short", 110.0, 83.0, notional=2000.0)
        # 1% of 2000 is 20 USDT, which is a move to 99. Just under stays fixed.
        _tick(99.1)
        pos = _one()
        self.assertFalse(pos["trailingActive"])
        self.assertEqual(pos["sl"], 110.0)
        self.assertEqual(pos["tp"], 83.0)

        _tick(99.0)
        pos = _one()
        self.assertTrue(pos["trailingActive"])
        self.assertIsNone(pos["tp"])
        # dist = 5; best + dist = 104, floored to breakeven 100. Not the old SL 110.
        self.assertAlmostEqual(pos["sl"], 100.0)
        self.assertLess(pos["sl"], 110.0)

        _tick(90.0)
        pos = _one()
        self.assertAlmostEqual(pos["bestPrice"], 90.0)
        self.assertAlmostEqual(pos["sl"], 95.0)

        # Bounce must not loosen the stop back up.
        _tick(92.0)
        pos = _one()
        self.assertAlmostEqual(pos["bestPrice"], 90.0)
        self.assertAlmostEqual(pos["sl"], 95.0)

        # Through the old target (83) but still beyond the trail: no TP exit.
        _tick(80.0)
        pos = _one()
        self.assertEqual(server.STATE["history"], [])
        self.assertAlmostEqual(pos["sl"], 85.0)

        _tick(85.0)
        self.assertEqual(server.STATE["openPositions"], [])
        self.assertEqual(server.STATE["history"][0]["reason"], "trailing")

    def test_fixed_sl_and_tp_before_activation(self):
        _arm(102.48, "short", 109.07, 91.27, notional=1000.0)
        _tick(109.07)
        self.assertEqual(server.STATE["openPositions"], [])
        self.assertEqual(server.STATE["history"][0]["reason"], "SL")

        self.setUp()
        _arm(102.48, "short", 109.07, 91.27, notional=1000.0)
        _tick(91.27)
        self.assertEqual(server.STATE["openPositions"], [])
        self.assertEqual(server.STATE["history"][0]["reason"], "TP")

    def test_trailing_disabled_matches_fixed_stops(self):
        server.STATE["settings"]["trailingEnabled"] = False
        _arm(100.0, "long", 90.0, 117.0, notional=1000.0)
        _tick(110.0)
        pos = _one()
        self.assertFalse(pos["trailingActive"])
        self.assertEqual(pos["sl"], 90.0)
        self.assertEqual(pos["tp"], 117.0)
        self.assertEqual(server.STATE["history"], [])

        _tick(117.0)
        self.assertEqual(server.STATE["openPositions"], [])
        self.assertEqual(server.STATE["history"][0]["reason"], "TP")

        self.setUp()
        server.STATE["settings"]["trailingEnabled"] = False
        _arm(100.0, "long", 90.0, 117.0, notional=1000.0)
        _tick(90.0)
        self.assertEqual(server.STATE["history"][0]["reason"], "SL")

    def test_positions_manage_sl_tp_and_trailing_independently(self):
        sol = {
            "pair": "SOL-USDT",
            "side": "short",
            "entry": 100.0,
            "sl": 110.0,
            "tp": 83.0,
            "notional": 1000.0,
            "price": 100.0,
            "unrealized": 0,
            "time": "2026-10-15T12:00:00+00:00",
        }
        eth = {
            "pair": "ETH-USDT",
            "side": "long",
            "entry": 200.0,
            "sl": 180.0,
            "tp": 234.0,
            "notional": 1000.0,
            "price": 200.0,
            "unrealized": 0,
            "time": "2026-10-16T08:00:00+00:00",
        }
        btc = {
            "pair": "BTC-USDT",
            "side": "long",
            "entry": 50.0,
            "sl": 40.0,
            "tp": 67.0,
            "notional": 2000.0,
            "price": 50.0,
            "unrealized": 0,
            "time": "2026-10-16T09:00:00+00:00",
        }
        server.STATE["openPositions"] = [sol, eth, btc]

        # SOL hits its own stop. ETH and BTC are not at their activation prices.
        _tick({"SOL-USDT": 110.0, "ETH-USDT": 201.0, "BTC-USDT": 50.2})
        self.assertEqual([p["pair"] for p in server.STATE["openPositions"]], ["ETH-USDT", "BTC-USDT"])
        self.assertEqual(server.STATE["history"][0]["pair"], "SOL-USDT")
        self.assertEqual(server.STATE["history"][0]["reason"], "SL")
        eth_pos, btc_pos = server.STATE["openPositions"]
        self.assertFalse(eth_pos["trailingActive"])
        self.assertEqual(eth_pos["sl"], 180.0)
        self.assertEqual(eth_pos["tp"], 234.0)
        self.assertFalse(btc_pos["trailingActive"])
        self.assertEqual(btc_pos["sl"], 40.0)
        self.assertEqual(btc_pos["tp"], 67.0)

        # ETH trails off its own 20-point risk (distance 10). BTC stays fixed.
        _tick({"ETH-USDT": 212.0, "BTC-USDT": 50.2})
        eth_pos, btc_pos = server.STATE["openPositions"]
        self.assertTrue(eth_pos["trailingActive"])
        self.assertIsNone(eth_pos["tp"])
        self.assertAlmostEqual(eth_pos["sl"], 202.0)
        self.assertFalse(btc_pos["trailingActive"])
        self.assertEqual(btc_pos["tp"], 67.0)
        self.assertEqual(btc_pos["sl"], 40.0)

        # BTC trails off its own 10-point risk (distance 5) and does not move ETH.
        _tick({"ETH-USDT": 212.0, "BTC-USDT": 51.0})
        eth_pos, btc_pos = server.STATE["openPositions"]
        self.assertAlmostEqual(eth_pos["sl"], 202.0)
        self.assertTrue(btc_pos["trailingActive"])
        self.assertIsNone(btc_pos["tp"])
        self.assertAlmostEqual(btc_pos["sl"], 50.0)

        # ETH leaves on its trail. BTC is still open, on its own stop.
        _tick({"ETH-USDT": 202.0, "BTC-USDT": 50.6})
        self.assertEqual([p["pair"] for p in server.STATE["openPositions"]], ["BTC-USDT"])
        self.assertEqual(server.STATE["history"][0]["pair"], "ETH-USDT")
        self.assertEqual(server.STATE["history"][0]["reason"], "trailing")
        self.assertEqual(server.STATE["history"][0]["exit"], 202.0)
        self.assertAlmostEqual(server.STATE["openPositions"][0]["sl"], 50.0)
        self.assertTrue(server.STATE["openPositions"][0]["trailingActive"])

    def test_legacy_sol_short_is_not_closed_by_the_update(self):
        pos = {
            "pair": "SOL-USDT",
            "side": "short",
            "entry": 102.48,
            "sl": 109.07,
            "tp": 91.27,
            "notional": 1024.8,
            "time": "2026-10-15T12:00:00+00:00",
        }
        server.STATE["openPositions"] = [pos]
        server.STATE["settings"]["maxHoldHours"] = 48
        server.ensure_position_fields(server.STATE["openPositions"][0])
        self.assertIs(server.STATE["openPositions"][0], pos)
        self.assertEqual(pos["sl"], 109.07)
        self.assertEqual(pos["tp"], 91.27)
        self.assertEqual(pos["initialSl"], 109.07)
        self.assertEqual(pos["initialTp"], 91.27)
        self.assertFalse(pos["trailingActive"])
        self.assertAlmostEqual(pos["bestPrice"], 102.48)
        self.assertEqual(pos["notional"], 1024.8)

        _tick(103.04)
        pos = _one()
        self.assertEqual(server.STATE["history"], [])
        self.assertEqual(server.STATE["balance"], 950.56)
        self.assertFalse(pos["trailingActive"])
        self.assertEqual(pos["sl"], 109.07)
        self.assertEqual(pos["tp"], 91.27)
        self.assertAlmostEqual(pos["bestPrice"], 102.48)

        _tick(101.0)
        pos = _one()
        self.assertTrue(pos["trailingActive"])
        self.assertIsNone(pos["tp"])
        self.assertAlmostEqual(pos["sl"], 102.48)
        _tick(91.27)
        self.assertEqual(len(server.STATE["openPositions"]), 1)
        self.assertEqual(server.STATE["history"], [])
        self.assertLess(server.STATE["openPositions"][0]["sl"], 102.48)

    def test_no_duplicate_pair_in_either_direction(self):
        sol = _book()
        server.STATE["openPositions"] = [sol]
        server.STATE["lastScan"] = [
            _signal("SOL-USDT", "long", 100),
            _signal("SOL-USDT", "short", 100),
            _signal("BTC-USDT", "short", 100),
        ]
        server.maybe_open()
        self.assertEqual([p["pair"] for p in server.STATE["openPositions"]], ["SOL-USDT", "BTC-USDT"])
        self.assertIs(server.STATE["openPositions"][0], sol)
        self.assertEqual(sol["side"], "short")
        self.assertEqual(server.STATE["history"], [])
        self.assertEqual(server.STATE["balance"], 950.56)

    def test_max_positions_cap(self):
        server.STATE["settings"]["maxPositions"] = 2
        server.STATE["settings"]["entryThreshold"] = 90
        server.STATE["lastScan"] = [
            _signal("ADA-USDT", "long", 96),
            _signal("BTC-USDT", "long", 100),
            _signal("ETH-USDT", "long", 97),
        ]
        server.maybe_open()
        self.assertEqual(
            [p["pair"] for p in server.STATE["openPositions"]],
            ["BTC-USDT", "ETH-USDT"],
        )
        server.maybe_open()
        self.assertEqual(
            [p["pair"] for p in server.STATE["openPositions"]],
            ["BTC-USDT", "ETH-USDT"],
        )

    def test_one_scan_opens_several_pairs_best_score_first(self):
        server.STATE["settings"]["entryThreshold"] = 90
        server.STATE["lastScan"] = [
            _signal("ADA-USDT", "long", 96),
            _signal("BTC-USDT", "long", 100),
            _signal("SOL-USDT", "short", 90),
            _signal("ETH-USDT", "long", 97),
            _signal("XRP-USDT", "long", 89),
        ]
        server.maybe_open()
        self.assertEqual(
            [p["pair"] for p in server.STATE["openPositions"]],
            ["BTC-USDT", "ETH-USDT", "ADA-USDT", "SOL-USDT"],
        )
        self.assertEqual(
            [p["side"] for p in server.STATE["openPositions"]],
            ["long", "long", "long", "short"],
        )
        self.assertEqual(server.STATE["balance"], 950.56)

    def test_equal_scores_keep_scan_order(self):
        server.STATE["lastScan"] = [
            _signal("ETH-USDT", "short", 100),
            _signal("BTC-USDT", "long", 100),
            _signal("SOL-USDT", "long", 100),
        ]
        server.maybe_open()
        self.assertEqual(
            [p["pair"] for p in server.STATE["openPositions"]],
            ["ETH-USDT", "BTC-USDT", "SOL-USDT"],
        )

    def test_stopped_bot_and_manual_scan_do_not_open(self):
        server.STATE["running"] = False
        server.STATE["lastScan"] = [_signal("BTC-USDT", "long", 100)]
        server.maybe_open()
        self.assertEqual(server.STATE["openPositions"], [])

        server.STATE["running"] = True
        started = []

        class FakeThread:
            def __init__(self, target=None, name=None, daemon=None):
                self.target = target

            def start(self):
                started.append(self.target)

        original = server.threading.Thread
        server.threading.Thread = FakeThread
        try:
            resp = server.app.test_client().post("/api/scan")
        finally:
            server.threading.Thread = original
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(started, [server.scan_all])
        self.assertEqual(server.STATE["openPositions"], [])

    def test_notional_cap_shrinks_later_entries_only(self):
        server.STATE["balance"] = 1000
        server.STATE["settings"]["riskPct"] = 2
        server.STATE["settings"]["leverage"] = 2
        server.STATE["settings"]["entryThreshold"] = 90
        server.STATE["lastScan"] = [
            _signal("ETH-USDT", "long", 99, tight=True),
            _signal("BTC-USDT", "long", 100, tight=True),
        ]
        server.maybe_open()
        first, second = server.STATE["openPositions"]
        self.assertEqual(first["pair"], "BTC-USDT")
        self.assertEqual(second["pair"], "ETH-USDT")
        wanted = round(1000 * 0.02 / 0.012, 2)
        self.assertFalse(first["notionalCapped"])
        self.assertEqual(first["notional"], wanted)
        self.assertEqual(first["riskAmt"], 20.0)
        room = 1000 * 2 - wanted
        capped = math.floor(room * 100 + 1e-9) / 100.0
        self.assertTrue(second["notionalCapped"])
        self.assertEqual(second["notional"], capped)
        self.assertLess(second["notional"], wanted)
        self.assertLessEqual(first["notional"] + second["notional"], 2000)

    def test_score_can_reach_100(self):
        perfect_long = server.score_setup("ALCISTA", True, True, True, 50, "long")
        perfect_short = server.score_setup("BAJISTA", True, True, True, 55, "short")
        self.assertEqual(perfect_long, 100)
        self.assertEqual(perfect_short, 100)
        self.assertEqual(server.score_setup("ALCISTA", True, True, True, 70, "long"), 100)
        self.assertEqual(server.score_setup("BAJISTA", True, True, True, 30, "short"), 100)
        self.assertLess(server.score_setup("ALCISTA", False, True, True, 50, "long"), 100)
        self.assertLess(server.score_setup("NEUTRAL", True, True, True, 50, "long"), 100)
        self.assertLess(server.score_setup("BAJISTA", True, False, True, 55, "short"), 100)
        self.assertLess(server.score_setup("BAJISTA", True, True, False, 55, "short"), 100)

    def test_maybe_open_requires_score_100(self):
        self.assertEqual(server.entry_threshold(), 100)
        server.STATE["lastScan"] = [
            _signal("ETH-USDT", "long", 95),
            _signal("SOL-USDT", "short", 99),
        ]
        server.maybe_open()
        self.assertEqual(server.STATE["openPositions"], [])

        server.STATE["lastScan"] = [_signal("ETH-USDT", "long", 100)]
        server.maybe_open()
        opened = _one()
        self.assertEqual(opened["pair"], "ETH-USDT")
        self.assertEqual(opened["side"], "long")
        self.assertEqual(opened["initialSl"], opened["sl"])
        self.assertEqual(opened["initialTp"], opened["tp"])
        self.assertFalse(opened["trailingActive"])
        self.assertEqual(opened["bestPrice"], opened["entry"])
        self.assertFalse(opened["notionalCapped"])
        self.assertEqual(server.STATE["balance"], 950.56)

    def test_entry_threshold_option_is_respected(self):
        server.STATE["settings"]["entryThreshold"] = 90
        self.assertEqual(server.entry_threshold(), 90)
        server.STATE["lastScan"] = [_signal("ETH-USDT", "long", 89)]
        server.maybe_open()
        self.assertEqual(server.STATE["openPositions"], [])
        server.STATE["lastScan"] = [_signal("ETH-USDT", "long", 90)]
        server.maybe_open()
        self.assertEqual(_one()["pair"], "ETH-USDT")
        self.assertEqual(server._sane_threshold(150), 100)
        self.assertEqual(server._sane_threshold(-4), 0)
        self.assertEqual(server._sane_threshold("nope"), 100)

    def test_max_positions_option_range(self):
        self.assertEqual(server.max_positions(), 5)
        self.assertEqual(server._sane_max_positions("nope"), 5)
        self.assertEqual(server._sane_max_positions(0), 1)
        self.assertEqual(server._sane_max_positions(99), 12)
        self.assertEqual(server._sane_max_positions(5), 5)
        os.environ["MAX_POSITIONS"] = "8"
        try:
            server.STATE_FILE.write_text(json.dumps({
                "balance": 950.56,
                "openPositions": [],
                "history": [],
                "settings": {"maxPositions": 2},
            }), encoding="utf-8")
            loaded = server.load_state()
        finally:
            os.environ.pop("MAX_POSITIONS", None)
        self.assertEqual(loaded["settings"]["maxPositions"], 8)

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
        self.assertNotIn("openPosition", loaded)
        pos = loaded["openPositions"][0]
        self.assertEqual(len(loaded["openPositions"]), 1)
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
        self.assertEqual(len(loaded["history"]), 1)
        self.assertNotIn("maxHoldHours", loaded["settings"])
        self.assertEqual(loaded["settings"]["scanSeconds"], 90)
        self.assertTrue(loaded["settings"]["trailingEnabled"])
        self.assertEqual(loaded["settings"]["trailingActivationPct"], 1.0)
        self.assertEqual(loaded["settings"]["trailingDistance"], 0.5)
        self.assertEqual(loaded["settings"]["entryThreshold"], 100)
        self.assertEqual(loaded["settings"]["maxPositions"], 5)

    def test_load_state_migrates_eth_short_without_closing_it(self):
        history = [
            {
                "time": "2026-10-01 10:00",
                "pair": "BTC-USDT",
                "side": "long",
                "entry": 60000,
                "exit": 59000,
                "pnl": -4.5,
                "reason": "SL",
            }
        ]
        payload = {
            "running": True,
            "balance": 950.56,
            "initialBalance": 10000,
            "openPosition": {
                "pair": "ETH-USDT",
                "side": "short",
                "entry": 2413.52,
                "sl": 2580.0,
                "tp": 2130.0,
                "notional": 1500.0,
                "leverage": 5,
                "time": "2026-10-17T09:00:00+02:00",
                "note": "keep-me",
            },
            "history": history,
            "settings": {"riskPct": 2, "leverage": 5, "scanSeconds": 90},
        }
        server.STATE_FILE.write_text(json.dumps(payload), encoding="utf-8")
        loaded = server.load_state()
        self.assertNotIn("openPosition", loaded)
        self.assertEqual(len(loaded["openPositions"]), 1)
        pos = loaded["openPositions"][0]
        self.assertEqual(pos["pair"], "ETH-USDT")
        self.assertEqual(pos["side"], "short")
        self.assertEqual(pos["entry"], 2413.52)
        self.assertEqual(pos["sl"], 2580.0)
        self.assertEqual(pos["tp"], 2130.0)
        self.assertEqual(pos["notional"], 1500.0)
        self.assertEqual(pos["note"], "keep-me")
        self.assertEqual(loaded["balance"], 950.56)
        self.assertEqual(loaded["history"], history)

        server.STATE.clear()
        server.STATE.update(loaded)
        server.STATE["running"] = True
        _tick(2420.0)
        self.assertEqual(len(server.STATE["openPositions"]), 1)
        self.assertEqual(server.STATE["openPositions"][0]["entry"], 2413.52)
        self.assertEqual(server.STATE["openPositions"][0]["notional"], 1500.0)
        self.assertEqual(server.STATE["openPositions"][0]["sl"], 2580.0)
        self.assertEqual(server.STATE["history"], history)
        self.assertEqual(server.STATE["balance"], 950.56)

        # Already larger than balance × leverage. It stays; a new coin does not open.
        server.STATE["balance"] = 100
        server.STATE["settings"]["leverage"] = 1
        server.STATE["lastScan"] = [_signal("BTC-USDT", "long", 100, tight=True)]
        server.maybe_open()
        self.assertEqual([p["pair"] for p in server.STATE["openPositions"]], ["ETH-USDT"])
        self.assertEqual(server.STATE["openPositions"][0]["notional"], 1500.0)
        self.assertEqual(server.STATE["openPositions"][0]["entry"], 2413.52)

    def test_saved_list_is_kept_when_a_legacy_key_is_also_present(self):
        payload = {
            "balance": 100,
            "openPositions": [{
                "pair": "BTC-USDT",
                "side": "long",
                "entry": 1,
                "sl": 0.9,
                "tp": 1.2,
                "notional": 10,
            }],
            "openPosition": {
                "pair": "ETH-USDT",
                "side": "short",
                "entry": 2413.52,
                "sl": 2500,
                "tp": 2000,
                "notional": 10,
            },
            "history": [{"pnl": 1.5, "pair": "XRP-USDT"}],
        }
        server.STATE_FILE.write_text(json.dumps(payload), encoding="utf-8")
        loaded = server.load_state()
        self.assertEqual([p["pair"] for p in loaded["openPositions"]], ["BTC-USDT"])
        self.assertNotIn("openPosition", loaded)
        self.assertEqual(loaded["history"][0]["pnl"], 1.5)
        self.assertEqual(loaded["balance"], 100)

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
        self.assertEqual(loaded["openPositions"], [])


if __name__ == "__main__":
    unittest.main()
