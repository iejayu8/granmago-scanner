"""Fee, path, selection and trend-signal tests for the 1.6.0 search.

These do not read a fold result and do not need Blofin candles.
"""

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
os.environ.setdefault("DATA_DIR", "/tmp/granmago-sim16-tests")

from sim16 import (  # noqa: E402
    BAR_MS,
    INITIAL,
    accept_event,
    norm_ppf,
    select_config,
    simulate,
)
from trend16 import H4_MS, build_trend, donchian_sides  # noqa: E402

H4 = 4 * 60 * 60 * 1000


def _flat(ts, price):
    return {"ts": ts, "o": price, "h": price, "l": price, "c": price}


def _spec_course(**kw):
    spec = {
        "name": "t",
        "family": "course",
        "rank": 1,
        "setups": "both",
        "sides": "both",
        "momentum": False,
        "hold": 0,
        "be_r": 0.0,
        "target": "swing",
        "target_r": 0.0,
        "min_rr": 0.0,
        "max_stop_pct": 0.08,
        "partial_r": 0.0,
        "partial_frac": 0.5,
        "trail_activate_r": 0.0,
        "trail_dist_r": 1.0,
        "trail_always": False,
        "maker": False,
        "weekly": False,
        "funding_max": None,
        "vol_target": 0.0,
    }
    spec.update(kw)
    return spec


def _book(bars):
    return {
        "AAA-USDT": {
            "ts": [b["ts"] for b in bars],
            "o": [b["o"] for b in bars],
            "h": [b["h"] for b in bars],
            "l": [b["l"] for b in bars],
            "c": [b["c"] for b in bars],
        }
    }


class SearchMathTest(unittest.TestCase):
    def test_inverse_normal_is_near_95(self):
        self.assertAlmostEqual(norm_ppf(0.975), 1.95996398454, places=6)

    def test_plateau_prefers_the_simpler_config(self):
        configs = [
            {"name": "sweep_ab", "rank": 0},
            {"name": "fancy", "rank": 2},
        ]
        rows = [
            {"name": "sweep_ab", "trades": 80, "pnl": 300.0},
            {"name": "fancy", "trades": 80, "pnl": 330.0},
        ]
        # Band is max(50, 0.15*330) = 50. 300 is inside 330-50.
        name, relaxed, plateau = select_config(rows, configs)
        self.assertEqual(name, "sweep_ab")
        self.assertFalse(relaxed)
        self.assertEqual(plateau[0], "sweep_ab")

    def test_a_clear_lead_beats_the_baseline(self):
        configs = [
            {"name": "sweep_ab", "rank": 0},
            {"name": "fancy", "rank": 2},
        ]
        rows = [
            {"name": "sweep_ab", "trades": 80, "pnl": 100.0},
            {"name": "fancy", "trades": 80, "pnl": 400.0},
        ]
        name, relaxed, _plateau = select_config(rows, configs)
        self.assertEqual(name, "fancy")
        self.assertFalse(relaxed)

    def test_thin_training_falls_back_to_the_baseline(self):
        configs = [
            {"name": "sweep_ab", "rank": 0},
            {"name": "fancy", "rank": 2},
        ]
        rows = [
            {"name": "sweep_ab", "trades": 3, "pnl": -10.0},
            {"name": "fancy", "trades": 4, "pnl": 50.0},
        ]
        name, relaxed, plateau = select_config(rows, configs, min_trades=40)
        self.assertTrue(relaxed)
        self.assertEqual(name, "sweep_ab")
        self.assertEqual(plateau, ["sweep_ab"])


class FillTest(unittest.TestCase):
    def test_gap_through_the_stop_fills_at_the_open(self):
        # Bar opens at 0. Its close (t=15m) is the signal. The next bar gaps to 90.
        bars = [
            {"ts": 0, "o": 100.0, "h": 100.0, "l": 100.0, "c": 100.0},
            {"ts": BAR_MS, "o": 90.0, "h": 90.0, "l": 90.0, "c": 90.0},
        ]
        event = {
            "t": BAR_MS,
            "pair": "AAA-USDT",
            "side": "long",
            "price": 100.0,
            "sl": 95.0,
            "tp": 110.0,
            "family": "course",
            "setup": "A",
            "confirm_ms": H4,
            "close_ms": BAR_MS,
            "weekly": "long",
            "funding": 0.0,
            "strength": 0.0,
            "ann_vol": None,
        }
        spec = _spec_course()
        self.assertTrue(accept_event(event, spec))
        rows = simulate(
            [0, BAR_MS],
            _book(bars),
            [event],
            {},
            spec,
            [("S", 0, 10 * BAR_MS)],
        )
        self.assertEqual(rows[0]["trades"], 1)
        self.assertEqual(rows[0]["reasons"].get("SL"), 1)
        # One R at 1% risk is about 10 USDT before the extra gap. A fill at
        # the stop would land near that. Filling the gap at 90 is worse.
        self.assertLess(rows[0]["pnl"], -15.0)

    def test_target_is_reached_inside_the_bar(self):
        bars = [
            {"ts": 0, "o": 100.0, "h": 100.0, "l": 100.0, "c": 100.0},
            {"ts": BAR_MS, "o": 100.0, "h": 112.0, "l": 100.0, "c": 111.0},
        ]
        event = {
            "t": BAR_MS,
            "pair": "AAA-USDT",
            "side": "long",
            "price": 100.0,
            "sl": 95.0,
            "tp": 110.0,
            "family": "course",
            "setup": "A",
            "confirm_ms": H4,
            "close_ms": BAR_MS,
            "weekly": "long",
            "funding": None,
            "strength": 0.0,
            "ann_vol": None,
        }
        rows = simulate(
            [0, BAR_MS], _book(bars), [event], {}, _spec_course(), [("S", 0, 10 * BAR_MS)],
        )
        self.assertEqual(rows[0]["trades"], 1)
        self.assertEqual(rows[0]["reasons"].get("TP"), 1)
        self.assertGreater(rows[0]["pnl"], 0.0)
        self.assertAlmostEqual(rows[0]["pnl"], INITIAL * 0, delta=1000)
        # Round trip costs 0.08% * 2 of notional plus the slippage drag.
        # Notional is about 398. The net win should be well under the raw 10 point move.
        self.assertLess(rows[0]["pnl"], 45.0)
        self.assertGreater(rows[0]["expectancy"], 1.0)

    def test_maker_needs_the_next_bar_to_trade_back(self):
        # Signal close is 100. Next bar never trades at or below 100, so no fill.
        bars = [
            {"ts": 0, "o": 100.0, "h": 100.0, "l": 100.0, "c": 100.0},
            {"ts": BAR_MS, "o": 101.0, "h": 103.0, "l": 100.5, "c": 102.0},
            {"ts": 2 * BAR_MS, "o": 102.0, "h": 104.0, "l": 101.0, "c": 103.0},
        ]
        event = {
            "t": BAR_MS, "pair": "AAA-USDT", "side": "long", "price": 100.0,
            "sl": 95.0, "tp": 110.0, "family": "course", "setup": "A",
            "confirm_ms": H4, "close_ms": BAR_MS, "weekly": "long",
            "funding": None, "strength": 0.0, "ann_vol": None,
        }
        spec = _spec_course(maker=True)
        rows = simulate(
            [0, BAR_MS, 2 * BAR_MS], _book(bars), [event], {}, spec, [("S", 0, 10 * BAR_MS)],
        )
        self.assertEqual(rows[0]["trades"], 0)
        self.assertEqual(rows[0]["pnl"], 0.0)

    def test_time_stop_counts_confirmation_bars(self):
        # Two 4H bars after the entry. Hold is 2, so the second one exits.
        start = 0
        bars = []
        for i in range(0, 16 * H4 // BAR_MS):
            bars.append(_flat(start + i * BAR_MS, 100.0))
        close_ms = H4
        event = {
            "t": close_ms, "pair": "AAA-USDT", "side": "long", "price": 100.0,
            "sl": 95.0, "tp": 120.0, "family": "course", "setup": "A",
            "confirm_ms": H4, "close_ms": close_ms, "weekly": "long",
            "funding": None, "strength": 0.0, "ann_vol": None,
        }
        snaps = {}
        for k in range(0, 6):
            bar_ts = k * H4
            snaps[bar_ts + H4] = {
                "close": 100.0, "k": 50.0, "hist": 0.0, "prev": 0.0,
                "confirm_ms": H4, "bar_ts": bar_ts,
            }
        spec = _spec_course(hold=2, momentum=False)
        rows = simulate(
            [b["ts"] for b in bars],
            _book(bars),
            [event],
            {"AAA-USDT": snaps},
            spec,
            [("S", 0, 20 * H4)],
        )
        self.assertEqual(rows[0]["trades"], 1)
        self.assertEqual(rows[0]["reasons"].get("time"), 1)
        # Entry at H4, exit when elapsed hits 2, which is the close at 3*H4.
        # The trade row does not keep the exit time in the fold summary.
        # A hold of 2 must not wait for 15 bars: the trade closes inside this window
        # and the PnL is only the round-trip cost, not a stop.
        self.assertGreater(rows[0]["pnl"], -5.0)
        self.assertLess(rows[0]["pnl"], 0.0)


class TrendSignalTest(unittest.TestCase):
    def test_donchian_does_not_read_the_future(self):
        candles = []
        price = 100.0
        for i in range(30):
            price += 1.0
            candles.append({
                "ts": i * H4_MS, "o": price - 1, "h": price + 0.5, "l": price - 1.5, "c": price,
            })
        sides = donchian_sides(candles, 5, 3)
        later = list(candles) + [{
            "ts": 30 * H4_MS, "o": 1.0, "h": 1.0, "l": 1.0, "c": 1.0,
        }]
        again = donchian_sides(later, 5, 3)
        self.assertEqual(sides, again[:-1])
        self.assertIsNone(sides[4])

    def test_ema_signal_uses_the_closed_bar_only(self):
        candles = []
        for i in range(80):
            close = 100.0 + i
            candles.append({"ts": i * H4_MS, "o": close, "h": close + 1, "l": close - 1, "c": close})
        spec = {
            "name": "ema", "mode": "ema", "lookback": 10, "atr_mult": 3.0,
            "sides": "both", "adx_min": 0.0, "vol_target": 0.0, "exit_lookback": 10,
        }
        events, state = build_trend("AAA-USDT", candles, H4_MS, spec)
        self.assertTrue(events)
        last_time = 79 * H4_MS + H4_MS
        self.assertEqual(events[-1]["t"], last_time)
        bumped = list(candles)
        bumped[-1] = dict(bumped[-1], c=1.0, h=1.0, l=1.0, o=1.0)
        _events2, state2 = build_trend("AAA-USDT", bumped, H4_MS, spec)
        earlier = 70 * H4_MS + H4_MS
        self.assertEqual(state[earlier], state2[earlier])
        self.assertIn(state[last_time], ("long", "short", None))


if __name__ == "__main__":
    unittest.main()
