"""1.8.5: weekly optimisation of 23/11/2026 applied (approved by Xavi).

Only two defaults change: hold 15 -> 24 bars and stop pad 0.3 % -> 0.2 %.
Momentum exit and minRR stay as they were.

They are not add-on options (config.yaml has no granmago_* key since 1.5.1;
run.sh deletes leftovers). The engine pins them on every start from
server.FIXED_BEHAVIOUR over whatever state.json saved, so an existing install
picks them up on the first start of 1.8.5 without touching its options.
"""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_blofin_only as base  # noqa: E402
import test_demo_server as tds  # noqa: E402

server = base.server
import granmago_strategy as g  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
ADDON = ROOT / "granmago"


class Defaults185Test(unittest.TestCase):
    def test_pinned_values(self):
        fb = server.FIXED_BEHAVIOUR
        self.assertEqual((fb["granmagoMaxHoldBars"], fb["granmagoStopPadPct"]), (24, 0.2))
        self.assertEqual((fb["granmagoMomentumExit"], fb["granmagoMinRr"]), (True, 0.0))  # unchanged
        self.assertEqual(g.DEFAULT_STOP_PAD_PCT, 0.2)
        s = server.default_state()["settings"]
        self.assertEqual((s["granmagoMaxHoldBars"], s["granmagoStopPadPct"]), (24, 0.2))

    def test_fallbacks_follow_the_pins(self):
        saved = server.STATE
        try:
            server.STATE = {"settings": {}}
            self.assertEqual((server.granmago_max_hold_bars(), server.granmago_stop_pad_pct()), (24, 0.2))
            server.STATE = {"settings": {"granmagoMaxHoldBars": "x", "granmagoStopPadPct": "x"}}
            self.assertEqual((server.granmago_max_hold_bars(), server.granmago_stop_pad_pct()), (24, 0.2))
        finally:
            server.STATE = saved

    def test_not_an_addon_option(self):
        cfg = (ADDON / "config.yaml").read_text(encoding="utf-8")
        self.assertNotIn("granmago_", cfg)  # Supervisor's options.json cannot hold them
        self.assertIn('version: "1.8.6"', cfg)
        run = (ADDON / "run.sh").read_text(encoding="utf-8")
        for key in ("granmago_max_hold_bars", "granmago_stop_pad_pct"):
            self.assertIn("  %s\n" % key, run)  # obsolete list: removed, never exported
        self.assertNotIn("GRANMAGO_MAX_HOLD_BARS", run)
        self.assertNotIn("GRANMAGO_STOP_PAD_PCT", run)

    def test_docs_coherent(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("máximo 24 velas, margen de stop 0,2 %", readme)
        self.assertNotIn("15 velas", readme)
        self.assertNotIn("0,3 % del precio", readme)


class ExistingInstallTest(unittest.TestCase):
    """state.json written by 1.8.4 (explicit 15 / 0.3) or with other values."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_file = server.STATE_FILE
        server.STATE_FILE = Path(self.tmp.name) / "state.json"
        self.addCleanup(setattr, server, "STATE_FILE", self.state_file)
        self.env = patch.dict("os.environ", {"GRANMAGO_MAX_HOLD_BARS": "15", "GRANMAGO_STOP_PAD_PCT": "0.3"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def load(self, settings, positions=()):
        payload = {"running": True, "balance": 1000.0, "openPositions": list(positions), "history": [],
                   "settings": settings}
        server.STATE_FILE.write_text(json.dumps(payload), encoding="utf-8")
        return server.load_state()

    def test_old_defaults_are_replaced(self):
        loaded = self.load({"granmagoMaxHoldBars": 15, "granmagoStopPadPct": 0.3, "granmagoMomentumExit": True,
                            "granmagoMinRr": 0.0, "riskPct": 1, "maxPositions": 3})
        s = loaded["settings"]
        self.assertEqual((s["granmagoMaxHoldBars"], s["granmagoStopPadPct"]), (24, 0.2))
        self.assertEqual((s["granmagoMomentumExit"], s["granmagoMinRr"]), (True, 0.0))
        self.assertEqual((s["riskPct"], s["maxPositions"]), (1.0, 3))

    def test_demo_fixture_from_1_7_0(self):
        shutil.copy(str(tds.FIXTURE), str(server.STATE_FILE))
        before = json.loads(server.STATE_FILE.read_text(encoding="utf-8"))["settings"]
        self.assertEqual((before["granmagoMaxHoldBars"], before["granmagoStopPadPct"]), (15, 0.3))
        s = server.load_state()["settings"]
        self.assertEqual((s["granmagoMaxHoldBars"], s["granmagoStopPadPct"]), (24, 0.2))

    def test_any_saved_value_is_pinned_too(self):
        s = self.load({"granmagoMaxHoldBars": 8, "granmagoStopPadPct": 0.5})["settings"]
        self.assertEqual((s["granmagoMaxHoldBars"], s["granmagoStopPadPct"]), (24, 0.2))

    def test_open_position_keeps_its_own_hold(self):
        pos = dict(base.position(), maxHoldBars=15)
        loaded = self.load({"granmagoMaxHoldBars": 15}, [pos])
        self.assertEqual(loaded["openPositions"][0]["maxHoldBars"], 15)
        self.assertEqual(loaded["openPositions"][0]["sl"], pos["sl"])  # stop not recomputed


class NewEntriesTest(unittest.TestCase):
    http = base.BlofinOnlyTest.http
    tearDown = base.BlofinOnlyTest.tearDown

    def setUp(self):
        base.BlofinOnlyTest.setUp(self)
        server.STATE["settings"].update(server.FIXED_BEHAVIOUR)

    def test_new_position_uses_24_bars(self):
        base.BlofinOnlyTest.enter(self)
        self.assertEqual(server.STATE["openPositions"][0]["maxHoldBars"], 24)

    def test_scan_passes_the_new_pad(self):
        seen = {}

        def spy(*a, **k):
            seen.update(k)
            return [], []
        with patch.object(server.granmago_strategy, "find_signals", spy), \
                patch.object(server, "fetch_candles", lambda *a, **k: [dict(ts=i * 14400000, o=1, h=1, l=1, c=1)
                                                                      for i in range(300)]):
            server.analyze_pair("BTC-USDT", 100)
        self.assertEqual((seen.get("stop_pad_pct"), seen.get("min_rr")), (0.2, 0.0))

    def test_time_exit_at_24_bars(self):
        c = 14400000
        pos = {"side": "long", "closeMs": 0, "confirmMs": c, "maxHoldBars": server.granmago_max_hold_bars(),
               "momentumExit": False, "sweepExtreme": 0.0}

        def bar(n):
            return {"barTs": (n - 1) * c, "confirmMs": c, "close": 1.0, "k": 50, "hist": 0, "prevHist": 0}
        self.assertIsNone(g.exit_decision(dict(pos), bar(15)))  # 1.8.4 closed here
        self.assertIsNone(g.exit_decision(dict(pos), bar(23)))
        self.assertEqual(g.exit_decision(dict(pos), bar(24)), ("time", 1.0))

if __name__ == "__main__":
    unittest.main()
