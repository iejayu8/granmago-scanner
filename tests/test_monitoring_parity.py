"""1.5.1's pinned setup trades like 1.5.0 with the monitoring options.

The explicit dict is the 1.5.0 reading of opciones_monitorizacion. The other
side is a start that still has those old keys in the environment and in
state.json. The trades on a short sample have to match.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="granmago-parity-")
os.environ.setdefault("LOG_LEVEL", "error")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "granmago"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import granmago_strategy as g  # noqa: E402
import server  # noqa: E402
import test_granmago_strategy as fx  # noqa: E402

# Same numbers as the 1.5.0 monitoring options file. Not read from
# server.FIXED_BEHAVIOUR, so a wrong pin fails this comparison.
MONITORING_150 = {
    "riskPct": 1.0,
    "leverage": 3,
    "maxPositions": 3,
    "feePctPerSide": 0.06,
    "slippagePctPerSide": 0.02,
    "scanSeconds": 90,
    "trailingEnabled": False,
    "trailingActivationPct": 1.0,
    "trailingDistance": 0.5,
    "entryThreshold": 100,
    "takeProfitR": 1.7,
    "partialTpR": 0.0,
    "partialTpFraction": 0.5,
    "partialMoveBreakeven": True,
    "stopMode": "swing",
    "stopAtrPeriod": 14,
    "stopAtrMult": 2.0,
    "trailingActivationR": 0.0,
    "trailingKeepTp": False,
    "pairCooldownMinutes": 0,
    "maxSameDirectionRiskR": 0.0,
    "regimeFilter": "off",
    "regimeAdxMin": 20.0,
    "regimeErMin": 0.2,
    "strategy": "granmago",
    "granmagoVariant": "a",
    "granmagoMaxAdds": 0,
    "granmagoMomentumExit": True,
    "granmagoMaxHoldBars": 15,
    "granmagoBreakevenR": 0.0,
    "granmagoMinRr": 0.0,
    "granmagoStopPadPct": 0.3,
    "granmagoStyle": "sweep",
    "granmagoSetups": "both",
    "pairs": list(server.LIQUID_PAIRS),
}

HOSTILE_ENV = {
    "STRATEGY": "scanner",
    "GRANMAGO_VARIANT": "b",
    "GRANMAGO_STYLE": "strict",
    "GRANMAGO_SETUPS": "pullback",
    "GRANMAGO_MAX_ADDS": "2",
    "GRANMAGO_MOMENTUM_EXIT": "false",
    "GRANMAGO_MAX_HOLD_BARS": "3",
    "GRANMAGO_STOP_PAD_PCT": "2",
    "GRANMAGO_MIN_RR": "1.5",
    "GRANMAGO_BREAKEVEN_R": "1",
    "TRAILING_ENABLED": "true",
    "TRAILING_ACTIVATION_PCT": "2.5",
    "TRAILING_DISTANCE": "0.25",
    "ENTRY_THRESHOLD": "50",
    "SCAN_SECONDS": "30",
    "TAKE_PROFIT_R": "3",
    "REGIME_FILTER": "adx",
    "REGIME_ADX_MIN": "35",
    "PAIR_COOLDOWN_MINUTES": "60",
    "STOP_MODE": "atr",
    "PARTIAL_TP_R": "1",
    "PAIRS": json.dumps(server.LIQUID_PAIRS),
    "RISK_PCT": "1",
    "LEVERAGE": "3",
    "MAX_POSITIONS": "3",
    "FEE_PCT_PER_SIDE": "0.06",
    "SLIPPAGE_PCT_PER_SIDE": "0.02",
    "INITIAL_BALANCE": "10000",
}


def _sample():
    daily, closes = fx._sweep_daily()
    c4 = fx._with_pdl_sweep(fx._flat_4h(daily), closes, depressed=True)
    return c4, daily


def _signals(settings):
    c4, daily = _sample()
    signals, bars = g.find_signals(
        c4,
        daily,
        [],
        variant=settings["granmagoVariant"],
        min_rr=settings["granmagoMinRr"],
        stop_pad_pct=settings["granmagoStopPadPct"],
        filters=True,
        style=settings["granmagoStyle"],
        setups=settings["granmagoSetups"],
    )
    return signals, bars


def _replay(settings):
    """One signal, then the target. Returns the open shape and the close."""
    signals, _bars = _signals(settings)
    if not signals:
        return {"signals": 0}
    signal = dict(signals[0])
    signal["pair"] = "BTC-USDT"
    base = server.default_state()
    base["settings"] = dict(settings)
    base["running"] = True
    base["balance"] = 10000.0
    base["initialBalance"] = 10000.0
    base["openPositions"] = []
    base["history"] = []
    base["log"] = []
    base["tradeSeq"] = 0
    previous = server.STATE
    previous_clock = server._clock
    server.STATE = base
    server._clock = lambda close_ms=signal["closeMs"]: close_ms / 1000.0
    try:
        opened = server.open_granmago(signal)
        if not opened:
            return {"signals": len(signals), "opened": False}
        pos = server._find_position("BTC-USDT")
        shape = {
            "signals": len(signals),
            "opened": True,
            "side": pos["side"],
            "setup": pos.get("setup"),
            "entry": round(pos["entry"], 6),
            "sl": round(pos["sl"], 6),
            "tp": round(pos["tp"], 6),
            "notional": pos["notional"],
            "maxAdds": pos["maxAdds"],
            "momentumExit": pos["momentumExit"],
            "maxHoldBars": pos["maxHoldBars"],
            "variant": pos["variant"],
            "strategy": pos["strategy"],
        }
        reason = server._apply_mark(pos, pos["tp"])
        shape["exit_reason"] = reason
        if server.STATE["history"]:
            row = server.STATE["history"][0]
            shape["pnl"] = row["pnl"]
            shape["exit"] = row["exit"]
            shape["history_reason"] = row["reason"]
        return shape
    finally:
        server.STATE = previous
        server._clock = previous_clock


class MonitoringParityTest(unittest.TestCase):
    def tearDown(self):
        for name in HOSTILE_ENV:
            os.environ.pop(name, None)

    def test_pinned_start_matches_150_monitoring_trades(self):
        c4, daily = _sample()
        strict, _bars = g.find_signals(
            c4, daily, [], variant="a", style="strict", setups="both",
            stop_pad_pct=0.3, min_rr=0.0, filters=True,
        )
        sweep, _bars = _signals(MONITORING_150)
        self.assertTrue(sweep)
        self.assertNotEqual(
            [(row["closeMs"], row["side"], row["setup"]) for row in strict],
            [(row["closeMs"], row["side"], row["setup"]) for row in sweep],
        )

        os.environ.update(HOSTILE_ENV)
        saved = {
            "running": False,
            "balance": 10000,
            "initialBalance": 10000,
            "openPositions": [],
            "history": [],
            "settings": {
                "strategy": "scanner",
                "granmagoStyle": "strict",
                "granmagoVariant": "b",
                "granmagoMaxAdds": 2,
                "trailingEnabled": True,
                "regimeFilter": "slope",
                "scanSeconds": 300,
                "riskPct": 2,
                "maxPositions": 5,
                "feePctPerSide": 0,
                "slippagePctPerSide": 0,
                "pairs": ["BTC-USDT"],
            },
        }
        server.STATE_FILE.write_text(json.dumps(saved), encoding="utf-8")
        try:
            loaded = server.load_state()
        finally:
            for name in HOSTILE_ENV:
                os.environ.pop(name, None)

        for key, value in MONITORING_150.items():
            self.assertEqual(loaded["settings"][key], value, key)

        legacy = _replay(MONITORING_150)
        pinned = _replay(loaded["settings"])
        self.assertGreaterEqual(legacy["signals"], 1)
        self.assertTrue(legacy["opened"])
        self.assertEqual(legacy["exit_reason"], "TP")
        self.assertEqual(pinned, legacy)


if __name__ == "__main__":
    unittest.main()
