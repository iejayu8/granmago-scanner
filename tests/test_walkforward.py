"""The walk-forward preset is chosen on train expectancy, not on the test window."""

import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="granmago-wf-")
os.environ.setdefault("LOG_LEVEL", "error")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import walkforward  # noqa: E402


class SelectTests(unittest.TestCase):
    def test_min_trades_beats_a_smaller_higher_expectancy(self):
        chosen, relaxed = walkforward.select_preset([
            {"name": "wider_tp", "trades": 5, "expectancy": 1.0, "pf": 3.0},
            {"name": "atr_partial", "trades": 20, "expectancy": 0.05, "pf": 1.05},
            {"name": "current_thr95_nofee", "trades": 50, "expectancy": 0.4, "pf": 1.5},
        ])
        self.assertEqual(chosen, "atr_partial")
        self.assertFalse(relaxed)

    def test_relaxed_when_nobody_has_enough_trades(self):
        chosen, relaxed = walkforward.select_preset([
            {"name": "wider_tp", "trades": 4, "expectancy": -0.2, "pf": 0.7},
            {"name": "atr_partial", "trades": 3, "expectancy": 0.3, "pf": 1.2},
        ])
        self.assertEqual(chosen, "atr_partial")
        self.assertTrue(relaxed)

    def test_negative_expectancy_can_still_be_the_least_bad(self):
        chosen, relaxed = walkforward.select_preset([
            {"name": "current_thr95", "trades": 40, "expectancy": -0.2, "pf": 0.7},
            {"name": "atr_partial", "trades": 30, "expectancy": -0.05, "pf": 0.9},
        ])
        self.assertEqual(chosen, "atr_partial")
        self.assertFalse(relaxed)

    def test_slices_do_not_share_a_boundary_minute(self):
        start, slices = walkforward.slice_bounds(int(
            __import__("datetime").datetime(2026, 10, 23, 12, 0, tzinfo=walkforward.MADRID).timestamp() * 1000
        ))
        self.assertEqual(len(slices), 4)
        for left, right in zip(slices, slices[1:]):
            self.assertEqual(left[2], right[1])
        # Half-open: a decision at the cut belongs to the later slice only.
        cut = slices[0][2]
        self.assertFalse(slices[0][1] <= cut < slices[0][2])
        self.assertTrue(slices[1][1] <= cut < slices[1][2])


if __name__ == "__main__":
    unittest.main()
