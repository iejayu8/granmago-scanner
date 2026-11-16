"""Panel <-> /api/state contract (1.7.2).

Drives the server into every user-visible state, takes the real /api/state
snapshot, checks the fields index.html reads (presence and type) and renders
the real index.html headless with jsdom: no JS errors, no NaN/undefined/null
text, and real figures wherever data exists.

jsdom: NODE_PATH or GRANMAGO_JSDOM_DIR (a dir with node_modules/jsdom). The
render part is skipped when node/jsdom are missing; the contract part always runs.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_blofin_only as base  # noqa: E402
import test_demo_server as tds  # noqa: E402

server = base.server
NOW, PAIR = base.NOW, base.PAIR
HERE = Path(__file__).resolve().parent
INDEX = HERE.parent / "granmago" / "static" / "index.html"
BAD_TEXT = ("NaN", "undefined", "null", "[object", "Infinity")


def _jsdom_env():
    node = shutil.which("node")
    if not node:
        return None, None
    candidates = [os.environ.get("GRANMAGO_JSDOM_DIR"), "/workspace/granmago-robustez-1.7.2"]
    env = dict(os.environ)
    for c in candidates:
        if c and (Path(c) / "node_modules" / "jsdom").is_dir():
            env["NODE_PATH"] = str(Path(c) / "node_modules")
            return node, env
    if os.environ.get("NODE_PATH") and (Path(os.environ["NODE_PATH"]) / "jsdom").is_dir():
        return node, env
    return None, None


NUM = (int, float)
OPT_NUM = (int, float, type(None))
OPT_STR = (str, type(None))
# Every field index.html reads from /api/state, with its allowed types.
CONTRACT = {
    "running": (bool,), "busy": (bool,), "lastTickAt": OPT_STR, "lastScanAt": OPT_STR,
    "openPositions": (list,), "history": (list,), "lastScan": (list,), "log": (list,),
    "settings": (dict,), "bestLong": (dict, type(None)), "bestShort": (dict, type(None)),
    "initialBalance": OPT_NUM, "balance": NUM, "tradingMode": (str,), "tradingError": OPT_STR,
    "trading": (dict,), "alerts": (list,), "killSwitch": (bool,), "display": (dict,),
    "exchangeAccount": (dict, type(None)),
}
DISPLAY = {"mode": (str,), "balance": OPT_NUM, "equity": OPT_NUM, "equitySource": OPT_STR,
           "unrealized": OPT_NUM, "unrealizedSource": OPT_STR, "pnlTotal": OPT_NUM, "unavailable": OPT_STR,
           "balanceSource": OPT_STR, "marginUsed": OPT_NUM, "marginSource": OPT_STR}
POSITION = {"pair": (str,), "side": (str,), "entry": NUM, "sl": NUM, "tp": NUM, "priceStale": (bool,),
            "price": OPT_NUM, "unrealized": OPT_NUM}


class PanelContractTest(unittest.TestCase):
    http = base.BlofinOnlyTest.http

    def setUp(self):
        tds.DemoServerTest.setUp(self)

    def tearDown(self):
        tds.DemoServerTest.tearDown(self)

    demo = tds.DemoServerTest.demo
    enter = tds.DemoServerTest.enter

    # --------------------------------------------------------------- states
    def snap(self):
        return json.loads(self.client.get("/api/state").get_data(as_text=True))

    def build_states(self):
        out = {}
        server.STATE["lastScan"] = []
        out["paper_empty"] = self.snap()
        self.enter()
        out["paper_position"] = self.snap()
        pos = server.STATE["openPositions"][0]
        pos["lastMarket"] = dict(pos["lastMarket"], ts=int((NOW - 120) * 1000))
        out["paper_position_stale"] = self.snap()
        pos["lastMarket"] = dict(pos["lastMarket"], ts=int(NOW * 1000))
        server.STATE["running"] = False
        out["paper_stopped_position"] = self.snap()
        server.STATE["killSwitch"] = True
        out["paper_kill_switch"] = self.snap()
        server.STATE["killSwitch"] = False
        server.STATE["history"] = [{"pair": PAIR, "side": "long", "pnl": None, "time": None, "reason": None},
                                   {"pair": PAIR, "side": "short", "pnl": 1.5}]
        out["paper_odd_history"] = self.snap()
        server.STATE["running"] = True
        server.STATE["openPositions"] = []
        server.STATE["history"] = []
        server.init_trading({"TRADING_MODE": "live"})
        out["blocked_live"] = self.snap()
        server.init_trading({})
        server.STATE["running"] = True
        # demo, before the first Blofin balance
        server.init_trading(tds.DEMO_ENV, transport=self.fake, sleep=lambda s: None)
        out["demo_waiting_balance"] = self.snap()
        server.start_trading()
        out["demo_empty"] = self.snap()
        self.enter()
        server.trading_cycle(force_reconcile=True)
        out["demo_position"] = self.snap()
        pos = server.STATE["openPositions"][0]
        pos["exchangeMark"]["atMs"] = int(NOW * 1000)
        server.STATE["exchangeAccount"]["fetchedAtMs"] = int(NOW * 1000)
        pos["lastMarket"] = dict(pos["lastMarket"], ts=int((NOW - 120) * 1000))
        out["demo_local_stale_blofin_mark"] = self.snap()
        pos["exchangeMark"]["atMs"] = int((NOW - 600) * 1000)
        server.STATE["exchangeAccount"]["fetchedAtMs"] = int((NOW - 600) * 1000)
        out["demo_account_stale"] = self.snap()
        server.STATE["exchangeAccount"]["fetchedAtMs"] = int(NOW * 1000)
        pos["lastMarket"] = dict(pos["lastMarket"], ts=int(NOW * 1000))  # display monitor keeps refreshing
        server.STATE["running"] = False
        out["demo_stopped_position"] = self.snap()
        server.TRADER.mark_unsafe("discrepancia_reconciliacion")
        out["demo_safe_state"] = self.snap()
        # fresh after the 1.7.0 -> 1.7.1 migration
        server.init_trading({})
        shutil.copy(tds.FIXTURE, server.STATE_FILE)
        server.STATE = server.load_state()
        server.init_trading(tds.DEMO_ENV, transport=self.fake, sleep=lambda s: None)
        out["demo_after_migration_waiting"] = self.snap()
        return out

    # ------------------------------------------------------------- contract
    def check_contract(self, name, snap):
        for key, types in CONTRACT.items():
            self.assertIn(key, snap, "%s: falta %s" % (name, key))
            self.assertIsInstance(snap[key], types, "%s: %s=%r" % (name, key, snap[key]))
        for key, types in DISPLAY.items():
            self.assertIn(key, snap["display"], "%s: display.%s" % (name, key))
            self.assertIsInstance(snap["display"][key], types, "%s: display.%s" % (name, key))
        for pos in snap["openPositions"]:
            for key, types in POSITION.items():
                self.assertIsInstance(pos.get(key), types, "%s: position.%s=%r" % (name, key, pos.get(key)))
        s = snap["settings"]
        for key in ("maxPositions", "riskPct", "leverage"):
            self.assertIsInstance(s.get(key), NUM, "%s: settings.%s" % (name, key))
        d = snap["display"]
        if snap["tradingMode"] == "demo" and d["equity"] is not None:
            # Never balance + unrealized in demo: equity is Blofin's totalEquity.
            self.assertEqual(d["equitySource"], "blofin")
            self.assertEqual(d["equity"], round(snap["exchangeAccount"]["totalEquity"], 2))

    def test_contract_and_headless_render_of_every_state(self):
        states = self.build_states()
        for name, snap in states.items():
            self.check_contract(name, snap)
        d = {k: v["display"] for k, v in states.items()}
        self.assertEqual(d["paper_position"]["equitySource"], "local")
        self.assertIsNone(d["paper_position_stale"]["equity"])
        self.assertIsNotNone(d["paper_stopped_position"]["equity"])
        self.assertEqual(d["demo_waiting_balance"]["unavailable"], "esperando el primer saldo de Blofin")
        self.assertEqual(d["demo_position"]["equitySource"], "blofin")
        self.assertEqual(d["demo_local_stale_blofin_mark"]["unrealizedSource"], "blofin")
        self.assertEqual(d["demo_account_stale"]["unavailable"], "sin saldo Blofin reciente")
        self.assertIsNone(states["demo_account_stale"]["openPositions"][0]["exchangeMark"])
        # 1.7.3: demo shows the free balance (details.available) and the margin in use
        # (sum of the positions' `margin`); equity = free + margin + unrealized.
        for name in ("demo_position", "demo_local_stale_blofin_mark", "demo_stopped_position", "demo_safe_state"):
            dd, acc = d[name], states[name]["exchangeAccount"]
            self.assertEqual(dd["balanceSource"], "available", name)
            self.assertEqual(dd["balance"], round(acc["available"], 2), name)
            self.assertEqual(dd["marginSource"], "positions", name)
            self.assertGreater(dd["marginUsed"], 0, name)
            self.assertAlmostEqual(dd["balance"] + dd["marginUsed"] + acc["isolatedUnrealizedPnl"], dd["equity"],
                                   delta=0.02, msg=name)
        self.assertEqual(d["demo_empty"]["marginUsed"], 0)
        self.assertEqual(d["demo_empty"]["balance"], d["demo_empty"]["equity"])
        for name, snap in states.items():
            if snap["tradingMode"] != "demo":
                self.assertIsNone(d[name]["marginUsed"], name)
                self.assertIsNone(d[name]["balanceSource"], name)

        node, env = _jsdom_env()
        if not node:
            self.skipTest("node/jsdom no disponibles: contrato comprobado, render headless omitido")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "snaps.json"
            path.write_text(json.dumps(states), encoding="utf-8")
            proc = subprocess.run([node, str(HERE / "panel_render.js"), str(INDEX), str(path)],
                                  capture_output=True, text=True, env=env, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        rendered = json.loads(proc.stdout)
        allowed_unavailable = {"paper_position_stale", "demo_account_stale", "demo_waiting_balance",
                               "demo_after_migration_waiting"}
        for name, r in rendered.items():
            self.assertEqual(r["errors"], [], name)
            for el, text in r["texts"].items():
                for bad in BAD_TEXT:
                    self.assertNotIn(bad, text or "", "%s #%s: %r" % (name, el, text))
            self.assertNotIn("Error del panel", r["texts"]["scanStatus"], name)
            if name not in allowed_unavailable:
                for el in ("balance", "equity", "unreal", "pnl", "openTrades"):
                    self.assertNotIn("No disponible", r["texts"][el], "%s #%s" % (name, el))
                    self.assertNotIn("Sin precio", r["texts"][el], "%s #%s" % (name, el))
                self.assertIn("USDT", r["texts"]["equity"], name)
        t = {k: v["texts"] for k, v in rendered.items()}
        self.assertEqual(t["paper_position_stale"]["equity"], "Sin precio Blofin fresco")
        self.assertEqual(t["demo_waiting_balance"]["equity"], "Esperando el primer saldo de Blofin")
        self.assertEqual(t["demo_waiting_balance"]["pnl"], "—")
        self.assertIn("(Blofin)", t["demo_local_stale_blofin_mark"]["unreal"])
        self.assertIn("(Blofin)", t["demo_local_stale_blofin_mark"]["openTrades"])
        self.assertEqual(t["demo_account_stale"]["equity"], "Sin saldo Blofin reciente")
        self.assertEqual(t["demo_position"]["modeBadge"], "DEMO")
        self.assertEqual(t["blocked_live"]["modeBadge"], "BLOQUEADO")
        self.assertIn("Trading bloqueado", t["blocked_live"]["tradingAlerts"])
        self.assertIn("Kill switch activo", t["paper_kill_switch"]["tradingAlerts"])
        self.assertFalse(rendered["demo_safe_state"]["safeBadgeHidden"])
        self.assertTrue(rendered["demo_position"]["resetDisabled"])
        self.assertFalse(rendered["paper_position"]["resetDisabled"])
        self.assertIn("-", t["paper_odd_history"]["historyTrades"])
        for name, r in rendered.items():
            demo = states[name]["tradingMode"] == "demo"
            self.assertEqual(r["texts"]["balanceLabel"], "Saldo libre (fuera de posiciones)" if demo else "Balance", name)
            self.assertEqual(r["marginHidden"], not demo, name)
            self.assertNotIn("Balance (Blofin demo)", json.dumps(r["texts"]), name)
            if not demo:
                self.assertEqual(r["balanceTitle"], "", name)
        self.assertRegex(t["demo_position"]["marginUsed"], r"^\d[\d.,]* USDT$")
        self.assertRegex(t["demo_position"]["balance"], r"^\d[\d.,]*\.\d\d USDT$")
        self.assertIn("Equity = saldo libre + margen en uso", rendered["demo_position"]["balanceTitle"])
        self.assertIn("suma de 'margin'", rendered["demo_position"]["marginTitle"])
        self.assertEqual(t["demo_waiting_balance"]["marginUsed"], "—")
        self.assertEqual(t["demo_empty"]["marginUsed"], "0 USDT")


if __name__ == "__main__":
    unittest.main()
