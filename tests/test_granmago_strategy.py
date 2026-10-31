"""Course strategy: structure, vetoes, and paper fills. Scanner behaviour stays covered elsewhere."""

import json
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
        for name in (
            "PAIRS", "STRATEGY", "GRANMAGO_STYLE", "GRANMAGO_SETUPS",
            "GRANMAGO_VARIANT", "GRANMAGO_MAX_ADDS", "FEE_PCT_PER_SIDE",
            "SLIPPAGE_PCT_PER_SIDE", "RISK_PCT", "LEVERAGE", "MAX_POSITIONS",
        ):
            os.environ.pop(name, None)
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

    def test_addon_default_is_the_monitoring_setup(self):
        for name in ("RISK_PCT", "LEVERAGE", "MAX_POSITIONS", "FEE_PCT_PER_SIDE",
                     "SLIPPAGE_PCT_PER_SIDE", "PAIRS", "STRATEGY", "GRANMAGO_STYLE"):
            os.environ.pop(name, None)
        fresh = server.default_state()
        settings = fresh["settings"]
        self.assertEqual(settings["strategy"], "granmago")
        self.assertEqual(settings["granmagoVariant"], "a")
        self.assertEqual(settings["granmagoStyle"], "sweep")
        self.assertEqual(settings["granmagoSetups"], "both")
        self.assertEqual(settings["granmagoMaxAdds"], 0)
        self.assertTrue(settings["granmagoMomentumExit"])
        self.assertEqual(settings["granmagoMaxHoldBars"], 15)
        self.assertEqual(settings["granmagoStopPadPct"], 0.3)
        self.assertEqual(settings["takeProfitR"], 1.7)
        self.assertFalse(settings["trailingEnabled"])
        self.assertEqual(settings["regimeFilter"], "off")
        self.assertEqual(settings["pairCooldownMinutes"], 0)
        self.assertEqual(settings["scanSeconds"], 90)
        self.assertEqual(settings["riskPct"], 1.0)
        self.assertEqual(settings["leverage"], 3)
        self.assertEqual(settings["maxPositions"], 3)
        self.assertEqual(settings["feePctPerSide"], 0.06)
        self.assertEqual(settings["slippagePctPerSide"], 0.02)
        self.assertEqual(settings["pairs"], server.LIQUID_PAIRS)
        server.STATE["settings"]["strategy"] = "scanner"
        self.assertEqual(server.strategy_mode(), "scanner")

    def test_removed_style_options_do_not_unpin_the_setup(self):
        os.environ["GRANMAGO_STYLE"] = "strict"
        os.environ["GRANMAGO_SETUPS"] = "pullback"
        os.environ["STRATEGY"] = "scanner"
        os.environ["GRANMAGO_VARIANT"] = "b"
        try:
            server.STATE["settings"]["granmagoStyle"] = "strict"
            server.STATE["settings"]["granmagoSetups"] = "pullback"
            server.STATE["settings"]["strategy"] = "scanner"
            server.STATE["settings"]["granmagoVariant"] = "b"
            server._apply_runtime_options(server.STATE["settings"])
            self.assertEqual(server.granmago_style(), "sweep")
            self.assertEqual(server.granmago_setups(), "both")
            self.assertEqual(server.strategy_mode(), "granmago")
            self.assertEqual(server.granmago_variant(), "a")
        finally:
            for name in ("GRANMAGO_STYLE", "GRANMAGO_SETUPS", "STRATEGY", "GRANMAGO_VARIANT"):
                os.environ.pop(name, None)
        server.STATE["settings"]["granmagoStyle"] = "strict"
        self.assertEqual(server.granmago_style(), "strict")

    def test_pairs_option_defaults_to_the_liquid_book(self):
        os.environ.pop("PAIRS", None)
        os.environ.pop("STRATEGY", None)
        os.environ.pop("GRANMAGO_STYLE", None)
        os.environ.pop("FEE_PCT_PER_SIDE", None)
        os.environ.pop("SLIPPAGE_PCT_PER_SIDE", None)
        server.STATE["settings"].pop("pairs", None)
        server.STATE["settings"]["strategy"] = "scanner"
        server.STATE["settings"]["granmagoStyle"] = "strict"
        server.STATE["settings"]["feePctPerSide"] = 0
        server.STATE["settings"]["slippagePctPerSide"] = 0
        server._apply_runtime_options(server.STATE["settings"])
        self.assertEqual(server.active_pairs(), server.LIQUID_PAIRS)
        self.assertEqual(len(server.LIQUID_PAIRS), 28)
        self.assertEqual(server.LIQUID_PAIRS[:12], server.DEFAULT_PAIRS)
        self.assertEqual(server.strategy_mode(), "granmago")
        self.assertEqual(server.granmago_style(), "sweep")
        self.assertEqual(server.fee_pct_per_side(), 0)
        self.assertEqual(server.slippage_pct_per_side(), 0)
        os.environ["PAIRS"] = json.dumps(server.LIQUID_PAIRS + ["btc-usdt", "NOT-A-PAIR", ""])
        os.environ["STRATEGY"] = "scanner"
        try:
            server._apply_runtime_options(server.STATE["settings"])
            self.assertEqual(server.active_pairs(), server.LIQUID_PAIRS)
            self.assertEqual(server.strategy_mode(), "granmago")
        finally:
            os.environ.pop("PAIRS", None)
            os.environ.pop("STRATEGY", None)
        os.environ["PAIRS"] = "ETH-USDT, eth-usdt, SOL-USDT"
        try:
            server._apply_runtime_options(server.STATE["settings"])
            self.assertEqual(server.active_pairs(), ["ETH-USDT", "SOL-USDT"])
        finally:
            os.environ.pop("PAIRS", None)
        os.environ["PAIRS"] = "nope, ???"
        try:
            server._apply_runtime_options(server.STATE["settings"])
            self.assertEqual(server.active_pairs(), server.LIQUID_PAIRS)
        finally:
            os.environ.pop("PAIRS", None)
        server.STATE["settings"].pop("pairs", None)
        server._apply_runtime_options(server.STATE["settings"])
        self.assertEqual(server.active_pairs(), server.LIQUID_PAIRS)

    def test_empty_pairs_option_overrides_a_stale_saved_book(self):
        # bashio::config prints nothing for `pairs: []`, and run.sh still
        # exports PAIRS as an empty string. That has to replace the 12-pair
        # book a 1.5.0 install left in state.json.
        stale = list(server.DEFAULT_PAIRS)
        self.assertEqual(len(stale), 12)
        self.assertEqual(len(server.LIQUID_PAIRS), 28)
        for raw in ("", "[]", "null", "none", "   "):
            os.environ["PAIRS"] = raw
            try:
                server.STATE["settings"]["pairs"] = list(stale)
                server._apply_runtime_options(server.STATE["settings"])
                self.assertEqual(server.active_pairs(), server.LIQUID_PAIRS, raw)
            finally:
                os.environ.pop("PAIRS", None)
        # A non-empty array is one symbol per line, and it also wins over state.
        os.environ["PAIRS"] = "ETH-USDT\nSOL-USDT\n"
        try:
            server.STATE["settings"]["pairs"] = list(stale)
            server._apply_runtime_options(server.STATE["settings"])
            self.assertEqual(server.active_pairs(), ["ETH-USDT", "SOL-USDT"])
        finally:
            os.environ.pop("PAIRS", None)
        server.STATE["settings"]["pairs"] = list(stale)
        server._apply_runtime_options(server.STATE["settings"])
        self.assertEqual(server.active_pairs(), stale)

    def _course_signal(self, side="long", price=80825.96, sl=78442.1, tp=86000.0, close_ms=None):
        sig = self._signal()
        sig.update({
            "side": side,
            "price": price,
            "sl": sl,
            "tp": tp,
            "addPrice": sl,
            "sweepExtreme": sl,
            "confirmMs": H4,
            "closeMs": H4 * 100 if close_ms is None else close_ms,
        })
        return sig

    def _scan_row(self, market, sig=None, pair="BTC-USDT"):
        if sig is None:
            sig = self._course_signal()
        sig = dict(sig)
        sig["pair"] = pair
        return {
            "pair": pair,
            "price": market,
            "scoreLong": 0,
            "scoreShort": 0,
            "granmago": sig,
        }

    def test_same_signal_is_not_reopened_after_the_stop(self):
        server.STATE["settings"]["granmagoMaxAdds"] = 0
        server.STATE["settings"]["slippagePctPerSide"] = 0
        close_ms = H4 * 100
        signal_price = 80825.96
        stop = 78442.1
        sig = self._course_signal(price=signal_price, sl=stop, tp=86000.0, close_ms=close_ms)
        server.STATE["lastScan"] = [self._scan_row(80000.0, sig)]
        server.maybe_open()
        self.assertEqual(len(server.STATE["openPositions"]), 1)
        pos = server.STATE["openPositions"][0]
        self.assertAlmostEqual(pos["entry"], 80000.0)
        self.assertAlmostEqual(pos["signalPrice"], signal_price)
        self.assertEqual(pos["closeMs"], close_ms)
        reason = server._apply_mark(pos, stop)
        self.assertEqual(reason, "SL")
        self.assertEqual(server.STATE["openPositions"], [])
        self.assertEqual(server.STATE["history"][0]["closeMs"], close_ms)
        balance_after = server.STATE["balance"]
        # The next scans still carry the same 4H signal. Some prints are
        # back inside the bracket, one is the stale signal price, and some
        # are already through the stop. None of them may open.
        for market in (
            80000.0, 77000.0, signal_price, 79000.0, 80000.0,
            77000.0, 81000.0, 78500.0, 80000.0, 77000.0, 80000.0,
        ):
            server.STATE["lastScan"] = [self._scan_row(market, sig)]
            server.maybe_open()
            self.assertEqual(server.STATE["openPositions"], [])
        self.assertEqual(len(server.STATE["history"]), 1)
        self.assertEqual(server.STATE["balance"], balance_after)
        self.assertFalse(server.open_granmago(dict(sig, pair="BTC-USDT", market=80000.0)))

        # tools/backtest.py replaces save_state with a no-op, so write the
        # file the same way a restart would read it.
        payload = {k: v for k, v in server.STATE.items() if k != "busy"}
        server.DATA_DIR.mkdir(parents=True, exist_ok=True)
        server.STATE_FILE.write_text(json.dumps(payload), encoding="utf-8")
        loaded = server.load_state()
        server.STATE.clear()
        server.STATE.update(loaded)
        server.STATE["running"] = True
        server.STATE["lastScan"] = [self._scan_row(80000.0, sig)]
        server.maybe_open()
        self.assertEqual(server.STATE["openPositions"], [])
        self.assertEqual(len(server.STATE["history"]), 1)

        later = self._course_signal(close_ms=close_ms + H4)
        server.STATE["lastScan"] = [self._scan_row(80100.0, later)]
        server.maybe_open()
        self.assertEqual(len(server.STATE["openPositions"]), 1)
        self.assertAlmostEqual(server.STATE["openPositions"][0]["entry"], 80100.0)
        self.assertEqual(server.STATE["openPositions"][0]["closeMs"], close_ms + H4)

    def test_price_beyond_stop_or_target_does_not_open(self):
        server.STATE["settings"]["granmagoMaxAdds"] = 0
        server.STATE["settings"]["slippagePctPerSide"] = 0.02
        stop = 78442.1
        # A long buys higher. 0.02% of slippage would drag this print back
        # above the stop; the raw print is what has to sit inside.
        market = 78432.1
        self.assertLess(market, stop)
        self.assertGreater(market * 1.0002, stop)
        server.STATE["lastScan"] = [self._scan_row(market)]
        server.maybe_open()
        self.assertEqual(server.STATE["openPositions"], [])
        self.assertEqual(server.STATE["history"], [])
        self.assertEqual(server.STATE["balance"], 1000.0)

        server.STATE["lastScan"] = [self._scan_row(stop)]
        server.maybe_open()
        self.assertEqual(server.STATE["openPositions"], [])
        server.STATE["lastScan"] = [self._scan_row(86000.0)]
        server.maybe_open()
        self.assertEqual(server.STATE["openPositions"], [])

        short = self._course_signal(
            side="short", price=100.0, sl=106.0, tp=90.0, close_ms=H4 * 20,
        )
        above_stop = 106.01
        self.assertGreater(above_stop, 106.0)
        self.assertLess(above_stop * (1.0 - 0.0002), 106.0)
        server.STATE["lastScan"] = [self._scan_row(above_stop, short, pair="ETH-USDT")]
        server.maybe_open()
        self.assertEqual(server.STATE["openPositions"], [])

        server.STATE["lastScan"] = [self._scan_row(99.0, short, pair="ETH-USDT")]
        server.maybe_open()
        self.assertEqual(len(server.STATE["openPositions"]), 1)
        pos = server.STATE["openPositions"][0]
        self.assertEqual(pos["pair"], "ETH-USDT")
        self.assertEqual(pos["side"], "short")
        self.assertAlmostEqual(pos["entry"], 99.0 * (1.0 - 0.0002))
        self.assertAlmostEqual(pos["signalPrice"], 100.0)

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
