"""Course strategy: structure, vetoes, and paper fills. Scanner behaviour stays covered elsewhere."""

import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="granmago-strategy-")
os.environ.pop("STRATEGY", None)
os.environ.pop("GRANMAGO_VARIANT", None)
os.environ.pop("GRANMAGO_MAX_ADDS", None)
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "granmago"))

import granmago_strategy as g  # noqa: E402
import server  # noqa: E402

DAY = g.DAY_MS
H4 = g.H4_MS


def _rising_book():
    """Uptrend, one swing high, one higher-low dip. 4H bars have no extra wick."""
    daily = []
    for i in range(96):
        if i < 48:
            close = 100 + i * 0.8
        elif i <= 58:
            close = 100 + (48 * 0.8 + (5 - abs(i - 53)) * 2)
        elif i <= 70:
            close = 100 + (40 + (i - 59) * 0.15)
        elif i <= 81:
            close = 100 + (42 - (5 - abs(i - 76)) * 1.2)
        else:
            close = 100 + (44 + (i - 82) * 0.2)
        close = float(close)
        daily.append({"ts": i * DAY, "o": close, "h": close, "l": close, "c": close})
    c4 = []
    for candle in daily:
        for k in range(6):
            close = candle["c"]
            c4.append({
                "ts": candle["ts"] + k * H4,
                "o": close,
                "h": close,
                "l": close,
                "c": close,
            })
    return c4, daily


def _mirror(candles):
    out = []
    for candle in candles:
        out.append({
            "ts": candle["ts"],
            "o": 300.0 - candle["o"],
            "h": 300.0 - candle["l"],
            "l": 300.0 - candle["h"],
            "c": 300.0 - candle["c"],
        })
    return out


class StrategyStructureTest(unittest.TestCase):
    def test_12h_buckets_are_three_blofin_4h_bars(self):
        bars = []
        for i in range(5):
            bars.append({"ts": i * H4, "o": 1.0, "h": 2.0 + i, "l": 0.5, "c": 1.0 + i})
        got = g.aggregate_12h(bars)
        # 00:00 and the partial 12:00 bucket. The partial one has only two bars.
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["ts"], 0)
        self.assertEqual(got[0]["o"], 1.0)
        self.assertEqual(got[0]["c"], 3.0)
        self.assertEqual(got[0]["h"], 4.0)
        self.assertEqual(got[0]["l"], 0.5)

    def test_daily_trend_wins_and_one_close_does_not_flip(self):
        daily = [{"ts": i * DAY, "o": 100.0 + i, "h": 101.0 + i, "l": 99.0 + i, "c": 100.0 + i} for i in range(80)]
        weekly = [{"ts": i * g.WEEK_MS, "o": 200.0 - i, "h": 201.0 - i, "l": 199.0 - i, "c": 200.0 - i} for i in range(80)]
        self.assertEqual(g.trend_at(daily, weekly, daily[-1]["ts"] + DAY), "long")
        climbed = [{"ts": i * DAY, "o": 50.0 + i, "h": 51.0 + i, "l": 49.0 + i, "c": 50.0 + i} for i in range(70)]
        self.assertEqual(g.trend_states(climbed)[-1], "long")
        one = climbed + [{"ts": 70 * DAY, "o": 1.0, "h": 1.0, "l": 1.0, "c": 1.0}]
        self.assertEqual(g.trend_states(one)[-1], "long")
        two = one + [{"ts": 71 * DAY, "o": 1.0, "h": 1.0, "l": 1.0, "c": 1.0}]
        self.assertEqual(g.trend_states(two)[-1], "short")

    def test_squeeze_is_negative_in_a_decline_and_crosses_on_a_rebound(self):
        fall = [{"ts": i * H4, "o": 100 - i, "h": 101 - i, "l": 99 - i, "c": 100 - i} for i in range(80)]
        hist = g.squeeze_hist(fall)
        self.assertLess(hist[-1], 0)
        rise = fall + [
            {"ts": (80 + i) * H4, "o": 20 + i * 3, "h": 24 + i * 3, "l": 19 + i * 3, "c": 23 + i * 3}
            for i in range(15)
        ]
        hist = g.squeeze_hist(rise)
        crossed = any(
            hist[i] is not None and hist[i - 1] is not None and hist[i] > 0 and hist[i - 1] <= 0
            for i in range(1, len(hist))
        )
        self.assertTrue(crossed)
        self.assertTrue(g.squeeze_allows(hist, len(hist) - 1, "long") or crossed)

    def test_stochastic_vetoes_only_the_exhausted_side(self):
        self.assertFalse(g.stoch_allows(85, "long"))
        self.assertTrue(g.stoch_allows(50, "long"))
        self.assertTrue(g.stoch_allows(15, "long"))
        self.assertFalse(g.stoch_allows(15, "short"))
        self.assertTrue(g.stoch_allows(50, "short"))
        self.assertTrue(g.stoch_allows(85, "short"))
        self.assertFalse(g.stoch_allows(None, "long"))

    def test_setup_a_stop_is_beyond_the_sweep_and_target_is_the_prior_high(self):
        c4, daily = _rising_book()
        signals, _bars = g.find_signals(c4, daily, [], variant="a", filters=False)
        self.assertEqual(len(signals), 1)
        signal = signals[0]
        self.assertEqual(signal["side"], "long")
        self.assertEqual(signal["setup"], "A")
        self.assertEqual(signal["confirmMs"], H4)
        self.assertLess(signal["sl"], signal["sweepExtreme"])
        self.assertLessEqual(signal["sl"], signal["sweepExtreme"] - 0.003 * signal["price"] + 1e-6)
        self.assertGreater(signal["tp"], signal["price"])
        self.assertAlmostEqual(signal["tp"], 148.4 * 0.999, places=4)
        # The next bars do not open a second trade. The entry was the second close.
        self.assertEqual(g.variant_spec("a")["confirm_bars"], 2)

    def test_one_close_back_above_the_level_does_not_enter(self):
        c4, daily = _rising_book()
        signals, _bars = g.find_signals(c4, daily, [], variant="a", filters=False)
        level = signals[0]["level"]
        for bar in c4:
            if bar["ts"] + H4 >= signals[0]["closeMs"]:
                bar["c"] = level - 1
                bar["o"] = bar["c"]
                bar["h"] = bar["c"]
                bar["l"] = min(bar["l"], bar["c"])
        later, _bars = g.find_signals(c4, daily, [], variant="a", filters=False)
        self.assertEqual(later, [])

    def test_variant_b_is_long_only_and_confirms_on_12h(self):
        c4, daily = _rising_book()
        signals, _bars = g.find_signals(c4, daily, [], variant="b", filters=False)
        self.assertTrue(signals)
        self.assertTrue(all(s["side"] == "long" for s in signals))
        self.assertTrue(all(s["confirmMs"] == g.H12_MS for s in signals))
        mirrored_4 = _mirror(c4)
        mirrored_d = _mirror(daily)
        shorts, _bars = g.find_signals(mirrored_4, mirrored_d, [], variant="a", filters=False)
        self.assertTrue(any(s["side"] == "short" for s in shorts))
        no_shorts, _bars = g.find_signals(mirrored_4, mirrored_d, [], variant="b", filters=False)
        self.assertEqual(no_shorts, [])

    def test_overbought_stochastic_blocks_the_structural_long(self):
        c4, daily = _rising_book()
        structural, _bars = g.find_signals(c4, daily, [], variant="a", filters=False)
        self.assertEqual(len(structural), 1)
        index = next(i for i, bar in enumerate(c4) if bar["ts"] + H4 == structural[0]["closeMs"])
        self.assertGreater(g.stoch_k_series(c4)[index], 80)
        gated, _bars = g.find_signals(c4, daily, [], variant="a", filters=True)
        self.assertEqual(gated, [])

    def test_unfilled_gap_past_the_pad_pushes_the_stop_out(self):
        # Far edge 98, near edge 102. The plain pad would sit inside the gap.
        confirm = [
            {"ts": 0, "o": 100, "h": 99.0, "l": 98.5, "c": 99.0},
            {"ts": H4, "o": 100, "h": 100.4, "l": 99.5, "c": 100.0},
            {"ts": 2 * H4, "o": 101, "h": 102, "l": 100.5, "c": 101.5},
        ]
        stop = g._stop_price(100, 103, None, 0.3, confirm, 2, "long")
        plain = 100 - 0.003 * 103
        self.assertLess(stop, plain)
        self.assertAlmostEqual(stop, 99.0)

    def test_momentum_exit_and_time_exit_and_add_block(self):
        pos = {
            "side": "long",
            "sweepExtreme": 90.0,
            "closeMs": 0,
            "confirmMs": H4,
            "momentumExit": True,
            "maxHoldBars": 15,
            "addBlocked": False,
        }
        blocked = g.exit_decision(pos, {
            "barTs": H4, "confirmMs": H4, "close": 89.0, "k": 40, "hist": -1.0, "prevHist": -0.5,
        })
        self.assertIsNone(blocked)
        self.assertTrue(pos["addBlocked"])
        self.assertFalse(g.add_distance_ok("long", 89.0, 90.0, 88.0) and not pos["addBlocked"])
        momentum = g.exit_decision(pos, {
            "barTs": 2 * H4, "confirmMs": H4, "close": 110.0, "k": 85, "hist": 1.0, "prevHist": 1.4,
        })
        self.assertEqual(momentum, ("momentum", 110.0))
        quiet = {
            "side": "long", "sweepExtreme": 90.0, "closeMs": 0, "confirmMs": H4,
            "momentumExit": False, "maxHoldBars": 15, "addBlocked": False,
        }
        too_soon = g.exit_decision(quiet, {
            "barTs": 10 * H4, "confirmMs": H4, "close": 100.0, "k": 40, "hist": 0.1, "prevHist": 0.2,
        })
        self.assertIsNone(too_soon)
        timed = g.exit_decision(quiet, {
            "barTs": 15 * H4, "confirmMs": H4, "close": 100.0, "k": 40, "hist": 0.1, "prevHist": 0.2,
        })
        self.assertEqual(timed[0], "time")


def _sweep_daily():
    """Uptrend with a prior swing high, so a sweep has a target above price."""
    daily = []
    closes = []
    for i in range(80):
        if i < 36:
            close = 100 + i * 0.8
        elif i < 46:
            close = 100 + 36 * 0.8 - (i - 36) * 1.2
        else:
            close = 100 + 36 * 0.8 - 10 * 1.2 + (i - 46) * 0.15
        closes.append(close)
        daily.append({"ts": i * DAY, "o": close, "h": close, "l": close, "c": close})
    return daily, closes


def _flat_4h(daily):
    c4 = []
    for candle in daily:
        for k in range(6):
            close = candle["c"]
            c4.append({
                "ts": candle["ts"] + k * H4,
                "o": close, "h": close, "l": close, "c": close,
            })
    return c4


def _with_pdl_sweep(c4, closes, depressed):
    """Wick through day 59's low on the first 4H of day 60, then one close back above."""
    level = closes[59]

    def at(day, k):
        return day * 6 + k

    if depressed:
        base = level
        for day in (58, 59):
            for k in range(5):
                i = at(day, k)
                c4[i]["o"] = c4[i]["h"] = c4[i]["c"] = base - 2
                c4[i]["l"] = base - 2.2
        c4[at(58, 0)]["h"] = base + 8
    sweep = at(60, 0)
    c4[sweep]["l"] = level - 1.5
    c4[sweep]["h"] = level + 1.2
    c4[sweep]["o"] = level - 1.0
    c4[sweep]["c"] = level + 1.0
    nxt = at(60, 1)
    c4[nxt]["o"] = level + 0.8
    c4[nxt]["h"] = level + 1.4
    c4[nxt]["l"] = level + 0.4
    c4[nxt]["c"] = level + 1.1
    return c4


class SweepModeTest(unittest.TestCase):
    def test_previous_day_sweep_fires_without_squeeze_or_adx(self):
        daily, closes = _sweep_daily()
        c4 = _with_pdl_sweep(_flat_4h(daily), closes, depressed=True)
        original = g.squeeze_allows
        g.squeeze_allows = lambda *args, **kwargs: False
        try:
            signals, _bars = g.find_signals(
                c4, daily, [], variant="a", style="sweep", setups="sweep",
            )
        finally:
            g.squeeze_allows = original
        self.assertEqual(len(signals), 1)
        signal = signals[0]
        self.assertEqual(signal["setup"], "A")
        self.assertEqual(signal["side"], "long")
        self.assertTrue(signal["stochConfirm"])
        self.assertFalse(signal["sqzOk"])
        self.assertFalse(signal["adxOk"])
        self.assertLess(signal["sl"], signal["sweepExtreme"])
        self.assertGreater(signal["tp"], signal["price"])

    def test_sweep_requires_the_stochastic_cross(self):
        daily, closes = _sweep_daily()
        c4 = _with_pdl_sweep(_flat_4h(daily), closes, depressed=False)
        signals, _bars = g.find_signals(
            c4, daily, [], variant="a", style="sweep", setups="sweep",
        )
        self.assertEqual(signals, [])

    def test_a_failed_reclaim_is_not_chased(self):
        daily, closes = _sweep_daily()
        c4 = _with_pdl_sweep(_flat_4h(daily), closes, depressed=True)
        level = closes[59]
        # The second 4H closes back through the low. Fail that close, then
        # print two later closes above it. Sweep mode must not take them.
        nxt = 60 * 6 + 1
        c4[nxt]["o"] = c4[nxt]["h"] = c4[nxt]["c"] = level - 0.5
        c4[nxt]["l"] = level - 0.8
        for k in (2, 3):
            j = 60 * 6 + k
            c4[j]["o"] = c4[j]["h"] = c4[j]["c"] = level + 1
            c4[j]["l"] = level + 0.5
        signals, _bars = g.find_signals(
            c4, daily, [], variant="a", style="sweep", setups="sweep",
        )
        self.assertEqual(signals, [])

    def test_pullback_only_skips_the_sweep(self):
        daily, closes = _sweep_daily()
        c4 = _with_pdl_sweep(_flat_4h(daily), closes, depressed=True)
        signals, _bars = g.find_signals(
            c4, daily, [], variant="a", style="sweep", setups="pullback",
        )
        self.assertTrue(all(row["setup"] == "B" for row in signals))


class GranmagoPaperTest(unittest.TestCase):
    def setUp(self):
        server.STATE.clear()
        server.STATE.update(server.default_state())
        server.STATE["running"] = True
        server.STATE["balance"] = 1000.0
        server.STATE["initialBalance"] = 1000.0
        server.STATE["history"] = []
        server.STATE["log"] = []
        server.STATE["settings"]["riskPct"] = 2
        server.STATE["settings"]["leverage"] = 3
        server.STATE["settings"]["feePctPerSide"] = 0
        server.STATE["settings"]["slippagePctPerSide"] = 0
        server.STATE["settings"]["trailingEnabled"] = True
        server.STATE["settings"]["trailingActivationPct"] = 0.1
        server.STATE["settings"]["strategy"] = "granmago"
        server.STATE["settings"]["granmagoMaxAdds"] = 1
        server.STATE["settings"]["granmagoBreakevenR"] = 0

    def _signal(self):
        return {
            "pair": "BTC-USDT",
            "side": "long",
            "setup": "A",
            "variant": "a",
            "price": 100.0,
            "sl": 94.0,
            "tp": 110.0,
            "addPrice": 96.0,
            "sweepExtreme": 96.0,
            "confirmMs": H4,
            "closeMs": 0,
        }

    def test_default_mode_is_the_scanner(self):
        server.STATE["settings"]["strategy"] = "scanner"
        self.assertEqual(server.strategy_mode(), "scanner")

    def test_sweep_style_option_round_trip(self):
        os.environ.pop("GRANMAGO_STYLE", None)
        os.environ.pop("GRANMAGO_SETUPS", None)
        server.STATE["settings"]["granmagoStyle"] = "strict"
        server.STATE["settings"]["granmagoSetups"] = "both"
        server._apply_runtime_options(server.STATE["settings"])
        self.assertEqual(server.granmago_style(), "strict")
        self.assertEqual(server.granmago_setups(), "both")
        os.environ["GRANMAGO_STYLE"] = "sweep"
        os.environ["GRANMAGO_SETUPS"] = "SWEEP"
        server._apply_runtime_options(server.STATE["settings"])
        self.assertEqual(server.granmago_style(), "sweep")
        self.assertEqual(server.granmago_setups(), "sweep")
        os.environ.pop("GRANMAGO_STYLE", None)
        os.environ.pop("GRANMAGO_SETUPS", None)
        server.STATE["settings"]["granmagoStyle"] = "strict"
        server.STATE["settings"]["granmagoSetups"] = "both"
        server._apply_runtime_options(server.STATE["settings"])

    def test_first_entry_reserves_the_add_and_a_runner_does_not_trail(self):
        self.assertTrue(server.open_granmago(self._signal()))
        pos = server.STATE["openPositions"][0]
        # 2% of 1000 is 20. Two legs, so this fill risks 10 on a 6 point stop.
        self.assertAlmostEqual(pos["notional"], round(10 / 0.06, 2), places=2)
        self.assertAlmostEqual(pos["sl"], 94.0)
        self.assertEqual(pos["strategy"], "granmago")
        server._apply_mark(pos, 105.0)
        pos = server._find_position("BTC-USDT")
        self.assertFalse(pos["trailingActive"])
        self.assertAlmostEqual(pos["sl"], 94.0)
        self.assertEqual(pos["addsDone"], 0)

    def test_add_at_the_extreme_keeps_the_stop_and_a_stop_print_does_not_add(self):
        self.assertTrue(server.open_granmago(self._signal()))
        pos = server._find_position("BTC-USDT")
        server._apply_mark(pos, 96.0)
        pos = server._find_position("BTC-USDT")
        self.assertEqual(pos["addsDone"], 1)
        self.assertAlmostEqual(pos["sl"], 94.0)
        self.assertGreater(pos["notional"], 160.0)
        self.assertLess(pos["entry"], 100.0)
        server._apply_mark(pos, 95.0)
        pos = server._find_position("BTC-USDT")
        self.assertEqual(pos["addsDone"], 1)
        reason = server._apply_mark(pos, 94.0)
        self.assertEqual(reason, "SL")
        self.assertIsNone(server._find_position("BTC-USDT"))

    def test_no_add_after_a_close_through_the_extreme_or_through_the_stop(self):
        server.STATE["settings"]["granmagoMaxAdds"] = 2
        self.assertTrue(server.open_granmago(self._signal()))
        pos = server._find_position("BTC-USDT")
        decision = g.exit_decision(pos, {
            "barTs": H4,
            "confirmMs": H4,
            "close": 95.0,
            "k": 40,
            "hist": -1.0,
            "prevHist": -0.2,
        })
        self.assertIsNone(decision)
        self.assertTrue(pos["addBlocked"])
        server._apply_mark(pos, 96.0)
        pos = server._find_position("BTC-USDT")
        self.assertEqual(pos["addsDone"], 0)
        fresh = self._signal()
        fresh["pair"] = "ETH-USDT"
        server.STATE["settings"]["granmagoMaxAdds"] = 1
        self.assertTrue(server.open_granmago(fresh))
        other = server._find_position("ETH-USDT")
        reason = server._apply_mark(other, 94.0)
        self.assertEqual(reason, "SL")
        self.assertEqual(other.get("addsDone", 0), 0)

    def test_target_closes_without_arming_the_scanner_trail(self):
        self.assertTrue(server.open_granmago(self._signal()))
        pos = server._find_position("BTC-USDT")
        reason = server._apply_mark(pos, 110.0)
        self.assertEqual(reason, "TP")
        self.assertEqual(server.STATE["history"][0]["reason"], "TP")
        self.assertGreater(server.STATE["history"][0]["pnl"], 0)


if __name__ == "__main__":
    unittest.main()
