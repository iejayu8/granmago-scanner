"""1.8.4: market regime log per closed 4H candle (record only).

Same definitions and basket as the 21/11/2026 demo analysis
(granmago-analisis-demo-20261121/dinamico: PREREGISTRO.md, week_sim.py):
ratio = ATR14 4H / its 30-day mean, vr = max(BTC ratio, basket median),
mv = 12 h move in ATR, tranquilo/movido/brusco. Nothing here may change a
trading decision.
"""
import csv
import gzip
import json
import logging
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_blofin_only as base  # noqa: E402
import test_demo_server as tds  # noqa: E402
import test_fiscal_export_180 as t180  # noqa: E402
import test_panel_contract as tpc  # noqa: E402
from test_qa_183 import capture_add_log  # noqa: E402

server = base.server
fiscal_export = t180.fiscal_export
import market_regime as mr  # noqa: E402

H4 = mr.H4_MS
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "regime_week_20261121.json.gz"
# week_results.txt of the analysis (Madrid time): label, vr, mv rounded as printed there.
STUDY = {1794816000000: ("brusco", 1.51, 0.17),     # 16/11 09:00
         1794945600000: ("movido", 1.35, 0.8),      # 17/11 21:00
         1794988800000: ("movido", 1.29, 0.95),     # 18/11 09:00
         1795003200000: ("movido", 1.26, 0.62),     # 18/11 13:00
         1795118400000: ("movido", 1.18, 1.23)}     # 19/11 21:00


def load_fixture():
    with gzip.open(str(FIXTURE), "rt") as fh:
        raw = json.load(fh)
    candles = {p: [dict(ts=r[0], o=r[1], h=r[2], l=r[3], c=r[4]) for r in rows] for p, rows in raw["candles"].items()}
    return candles, {int(k): v for k, v in raw["expected"].items()}


CANDLES, EXPECTED = load_fixture()
LAST_CLOSE = max(STUDY)


def upto(close_ms, candles=None):
    """What a scan sees right after close_ms: the closed candles plus the one in progress."""
    out = {}
    for p, rows in (candles or CANDLES).items():
        keep = [r for r in rows if r["ts"] <= close_ms - H4]
        if keep and keep[-1]["ts"] == close_ms - H4:
            keep = keep + [dict(keep[-1], ts=close_ms)]  # forming candle, must be ignored
        out[p] = keep
    return out


class CalculationTest(unittest.TestCase):
    def test_basket_is_the_analysis_basket(self):
        self.assertEqual(len(mr.BASKET), 27)
        self.assertEqual(set(mr.BASKET), set(CANDLES))
        self.assertIn("BTC-USDT", mr.BASKET)
        self.assertNotIn("IOST-USDT", mr.BASKET)
        self.assertEqual(set(mr.BASKET), set(server.LIQUID_PAIRS) - {"IOST-USDT"})

    def test_known_values_from_the_analysis(self):
        for close_ms, (label, vr, mv) in STUDY.items():
            row = mr.compute(upto(close_ms), close_ms)
            self.assertEqual(row["regime"], label, close_ms)
            self.assertEqual((round(row["vr"], 2), round(row["mv"], 2)), (vr, mv), close_ms)
            exp = EXPECTED[close_ms]  # week_sim.py's numpy code, unrounded
            self.assertEqual(row["regime"], exp[0])
            self.assertAlmostEqual(row["vr"], exp[1], places=10)
            self.assertAlmostEqual(row["mv"], exp[2], places=10)
            self.assertEqual((row["n_symbols"], row["n_basket"], row["missing"]), (27, 27, ""))
            self.assertEqual(row["vr"], max(row["btc_ratio"], row["basket_median_ratio"]))
            self.assertEqual(row["candle_close_utc"], mr.utc_iso(close_ms))

    def test_classification_thresholds_use_unrounded_values(self):
        cases = [((0.99, 1.49), "tranquilo"), ((1.0, 0.1), "movido"), ((0.5, 1.5), "movido"),
                 ((1.3999, 2.9999), "movido"), ((1.4, 0.0), "brusco"), ((0.5, 3.0), "brusco"),
                 ((0.999999, 1.499999), "tranquilo"), ((None, 1.0), "insuficiente"), ((1.0, None), "insuficiente")]
        for (vr, mv), label in cases:
            self.assertEqual(mr.classify(vr, mv), label, (vr, mv))

    def test_atr14_is_wilder_like_the_analysis(self):
        h = [10.0 + i for i in range(20)]
        low = [8.0 + i for i in range(20)]
        c = [9.0 + i for i in range(20)]
        a = mr.atr14(h, low, c)
        self.assertTrue(all(x is None for x in a[:13]))
        self.assertAlmostEqual(a[13], (2 + 13 * 2.0) / 14)  # TR = max(h-l, |h-c_prev|, |l-c_prev|) = 2
        self.assertAlmostEqual(a[19], 2.0)

    def test_forming_candle_is_ignored(self):
        a = mr.compute(upto(LAST_CLOSE), LAST_CLOSE)
        closed = {p: [r for r in rows if r["ts"] < LAST_CLOSE] for p, rows in upto(LAST_CLOSE).items()}
        self.assertEqual(a, mr.compute(closed, LAST_CLOSE))

    def test_btc_missing_is_insufficient_not_a_guess(self):
        data = upto(LAST_CLOSE)
        del data["BTC-USDT"]
        row = mr.compute(data, LAST_CLOSE)
        self.assertEqual(row["regime"], "insuficiente")
        self.assertIsNone(row["vr"])
        self.assertIsNone(row["mv"])
        self.assertIsNone(row["btc_ratio"])
        self.assertIn("BTC-USDT:sin datos del escaneo", row["missing"])
        self.assertEqual(row["n_symbols"], 26)
        self.assertIsNotNone(row["basket_median_ratio"])

    def test_short_history_and_missing_candle_are_reported(self):
        data = upto(LAST_CLOSE)
        data["BTC-USDT"] = data["BTC-USDT"][-150:]
        row = mr.compute(data, LAST_CLOSE)
        self.assertEqual(row["regime"], "insuficiente")
        self.assertRegex(row["missing"], r"BTC-USDT:historial \d+<194 velas")
        data = upto(LAST_CLOSE)
        data["ETH-USDT"] = [r for r in data["ETH-USDT"] if r["ts"] < LAST_CLOSE - H4]
        row = mr.compute(data, LAST_CLOSE)
        self.assertIn("ETH-USDT:sin vela", row["missing"])
        self.assertEqual(row["n_symbols"], 26)
        self.assertNotEqual(row["regime"], "insuficiente")  # the basket median uses the valid symbols

    def test_no_data_at_all(self):
        row = mr.compute({}, LAST_CLOSE)
        self.assertEqual((row["regime"], row["n_symbols"], row["vr"], row["mv"]), ("insuficiente", 0, None, None))

    def test_candle_close_for(self):
        self.assertEqual(mr.candle_close_for(LAST_CLOSE), LAST_CLOSE)
        self.assertEqual(mr.candle_close_for(LAST_CLOSE + H4 - 1), LAST_CLOSE)
        self.assertEqual(mr.candle_close_for(LAST_CLOSE - 1), LAST_CLOSE - H4)


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_idempotent_across_restarts(self):
        row = mr.compute(upto(LAST_CLOSE), LAST_CLOSE)
        s1 = mr.RegimeStore(self.tmp.name)
        self.assertTrue(s1.record(row, "demo", now_ms=LAST_CLOSE + 60000, engine_version="1.8.4"))
        self.assertFalse(s1.record(row, "demo", now_ms=LAST_CLOSE + 120000, engine_version="1.8.4"))
        s2 = mr.RegimeStore(self.tmp.name)  # restart
        self.assertTrue(s2.has(LAST_CLOSE, "demo"))
        self.assertFalse(s2.record(row, "demo", now_ms=LAST_CLOSE + 999000, engine_version="1.8.4"))
        rows = s2.regime_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["recorded_at_utc"], mr.utc_iso(LAST_CLOSE + 60000))  # first write kept
        self.assertTrue(s2.record(row, "paper", now_ms=LAST_CLOSE, engine_version="1.8.4"))  # per mode
        self.assertEqual(list(rows[0])[:len(mr.REGIME_COLUMNS)], mr.REGIME_COLUMNS)
        self.assertEqual(s2.latest("demo")["regime"], "movido")

    def test_own_file_apart_from_ledger_and_journal(self):
        mr.RegimeStore(self.tmp.name).record(mr.compute({}, LAST_CLOSE), "demo", now_ms=LAST_CLOSE)
        self.assertEqual(sorted(p.name for p in Path(self.tmp.name).iterdir()), [mr.DB_NAME])
        self.assertEqual(mr.DB_NAME, "market_regime.sqlite")

    def test_retention_keeps_at_least_one_year(self):
        s = mr.RegimeStore(self.tmp.name)
        day = 86400 * 1000
        old, kept = LAST_CLOSE - 401 * day, LAST_CLOSE - 366 * day
        for t in (old, kept, LAST_CLOSE):
            s.record(mr.compute({}, t), "demo", now_ms=t)
            s.record_event(now_ms=t + 1000, mode="demo", pair="XMR-USDT", side="long", signal_close_ms=t,
                           outcome="descartada", reason="cooldown")
        self.assertGreaterEqual(mr.RETENTION_DAYS, 366)
        self.assertEqual(s.prune(LAST_CLOSE), 2)
        self.assertEqual([r["candle_close_ms"] for r in s.regime_rows()], [kept, LAST_CLOSE])
        self.assertEqual(len(s.event_rows()), 2)

    def test_events_join_their_regime_candle(self):
        s = mr.RegimeStore(self.tmp.name)
        now = LAST_CLOSE + 10 * 60000
        s.record_event(now_ms=now, mode="demo", pair="XMR-USDT", side="long", signal_close_ms=LAST_CLOSE,
                       outcome="abierta", reason="entrada")
        ev = s.event_rows()[0]
        self.assertEqual((ev["regime_candle_close_ms"], ev["regime"]), (LAST_CLOSE, None))  # regime not yet recorded
        s.record(mr.compute(upto(LAST_CLOSE), LAST_CLOSE), "demo", now_ms=now)
        ev = s.event_rows()[0]
        self.assertEqual((ev["regime"], round(ev["vr"], 2), round(ev["mv"], 2)), ("movido", 1.18, 1.23))
        self.assertEqual(ev["regime_candle_close_utc"], mr.utc_iso(LAST_CLOSE))
        # same event again (next scan, restart): one row
        s.record_event(now_ms=now + 90000, mode="demo", pair="XMR-USDT", side="long", signal_close_ms=LAST_CLOSE,
                       outcome="abierta", reason="entrada")
        self.assertEqual(len(mr.RegimeStore(self.tmp.name).event_rows()), 1)
        with self.assertRaises(ValueError):
            s.record_event(now_ms=now, mode="demo", pair="X", side="long", signal_close_ms=1, outcome="otra", reason="x")


class RegimeEngineMixin:
    def start_regime(self):
        self.rtmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.rtmp.cleanup)
        self.addCleanup(self.stop_regime)
        server._REGIME_CANDLES.clear()
        self.assertIsNotNone(server.init_market_regime(self.rtmp.name))
        return server.MARKET_REGIME["store"]

    def stop_regime(self):
        server.MARKET_REGIME.update(store=None, last=None, lastPruneMs=0)
        server._REGIME_CANDLES.clear()


class EngineStepTest(RegimeEngineMixin, unittest.TestCase):
    http = base.BlofinOnlyTest.http
    tearDown = base.BlofinOnlyTest.tearDown

    def setUp(self):
        base.BlofinOnlyTest.setUp(self)
        self.store = self.start_regime()

    def feed(self, close_ms, candles=None):
        for p, rows in upto(close_ms, candles).items():
            server._regime_observe(p, rows)

    def test_records_once_per_candle_and_after_restart(self):
        self.feed(LAST_CLOSE)
        with self.assertLogs("granmago", "INFO") as logs:
            row = server.market_regime_step(LAST_CLOSE + 2 * 60000)
        self.assertEqual(row["regime"], "movido")
        self.assertTrue(any("Régimen 4H" in r.getMessage() for r in logs.records))
        self.assertIsNone(server.market_regime_step(LAST_CLOSE + 4 * 60000))
        server.init_market_regime(self.rtmp.name)  # restart
        self.assertEqual(server.MARKET_REGIME["last"]["regime"], "movido")
        self.feed(LAST_CLOSE)
        self.assertIsNone(server.market_regime_step(LAST_CLOSE + 6 * 60000))
        rows = self.store.regime_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["mode"], rows[0]["engine_version"]), ("paper", server.VERSION))

    def test_waits_for_late_candles_then_records_what_is_missing(self):
        cut = dict(CANDLES)
        cut["ETH-USDT"] = [r for r in CANDLES["ETH-USDT"] if r["ts"] < LAST_CLOSE - H4]
        self.feed(LAST_CLOSE, cut)
        self.assertIsNone(server.market_regime_step(LAST_CLOSE + 5 * 60000))
        self.assertEqual(self.store.regime_rows(), [])
        row = server.market_regime_step(LAST_CLOSE + 31 * 60000)
        self.assertIn("ETH-USDT:sin vela", row["missing"])
        self.assertEqual(self.store.regime_rows()[0]["n_symbols"], 26)

    def test_no_basket_candles_is_recorded_as_insufficient(self):
        row = server.market_regime_step(LAST_CLOSE + 40 * 60000)
        self.assertEqual(row["regime"], "insuficiente")
        self.assertIn("BTC-USDT:sin datos del escaneo", self.store.regime_rows()[0]["missing"])

    def test_calculation_error_is_a_warning(self):
        self.feed(LAST_CLOSE)
        with patch.object(mr, "compute", side_effect=RuntimeError("boom")), \
                self.assertLogs("granmago", "WARNING") as logs:
            self.assertIsNone(server.market_regime_step(LAST_CLOSE + 60000))
        self.assertTrue(any("boom" in r.getMessage() and r.levelname == "WARNING" for r in logs.records))

    def test_disabled_store_does_nothing(self):
        self.stop_regime()
        server._regime_observe("BTC-USDT", CANDLES["BTC-USDT"])
        self.assertEqual(server._REGIME_CANDLES, {})
        self.assertIsNone(server.market_regime_step(LAST_CLOSE))

    def test_api_state_shows_the_last_regime_read_only(self):
        self.feed(LAST_CLOSE)
        server.market_regime_step(LAST_CLOSE + 60000)
        snap = server.public_state()
        self.assertEqual(snap["marketRegime"]["regime"], "movido")
        self.assertEqual(snap["marketRegime"]["candle_close_utc"], mr.utc_iso(LAST_CLOSE))
        self.stop_regime()
        self.assertIsNone(server.public_state()["marketRegime"])

    def test_scan_uses_the_same_requests_and_survives_errors(self):
        calls = []

        def fetch(pair, bar, limit):
            calls.append((pair, bar, limit))
            return [dict(r) for r in upto(LAST_CLOSE)[pair]] if bar == "4H" and pair in CANDLES else []
        server._clock = lambda: (LAST_CLOSE + 60000) / 1000.0
        pairs = ["BTC-USDT", "ETH-USDT", "XMR-USDT"]
        with patch.object(server, "fetch_candles", fetch), patch.object(server, "active_pairs", lambda: pairs), \
                patch.object(server.time, "sleep", lambda s: None):
            server.scan_all()
            with_store = list(calls)
            self.assertEqual(self.store.regime_rows()[0]["n_symbols"], 3)
            self.assertIn("SOL-USDT:sin datos del escaneo", self.store.regime_rows()[0]["missing"])
            calls.clear()
            self.stop_regime()
            server.scan_all()
            self.assertEqual(calls, with_store)  # no extra Blofin request
            self.start_regime()
            calls.clear()
            server._clock = lambda: (LAST_CLOSE + H4 + 60000) / 1000.0
            with patch.object(mr, "compute", side_effect=RuntimeError("boom")):
                server.scan_all()  # does not raise
            self.assertEqual(calls, with_store)
            self.assertIsNotNone(server.STATE.get("lastScan"))


def snapshot(exchange=None):
    keep = ("pair", "side", "entry", "sl", "tp", "notional", "contracts", "riskAmt", "entryOrderId")
    pos = [{k: p.get(k) for k in keep} for p in server.STATE["openPositions"]]
    orders = [] if exchange is None else sorted(
        (o["instId"], o["side"], o["filledSize"], o["averagePrice"]) for o in exchange.orders.values())
    stops = [] if exchange is None else sorted((t["instId"], t["slTriggerPrice"]) for t in exchange.tpsl)
    return pos, orders, stops


SCENARIO_PAIRS = ("XMR-USDT", "DOGE-USDT", "ETH-USDT", "SOL-USDT", "LINK-USDT")


def scenario_rows():
    rows = [base.scan_row(p) for p in SCENARIO_PAIRS]
    rows[1]["granmago"]["sl"] = 650.0  # stop above a long: refused by _granmago_plan
    return rows


class SameDecisionsMixin(RegimeEngineMixin):
    """Run the same signals with the log off, on, and on but failing."""

    def run_variant(self, variant):
        self.reset_book()
        server._MAXPOS_LOGGED.clear()
        self.msgs.clear()
        if variant != "off":
            self.start_regime()
        patches = []
        if variant == "failing":
            patches = [patch.object(mr.RegimeStore, "record_event", side_effect=sqlite3.OperationalError("disk")),
                       patch.object(mr, "compute", side_effect=RuntimeError("boom"))]
        for p in patches:
            p.start()
        try:
            server.STATE["lastScan"] = scenario_rows()
            server.maybe_open()
            server.maybe_open()  # next poll on the same candle
            server.market_regime_step()
            out = (snapshot(getattr(self, "exchange", None)), [m for m in self.msgs])
            events = [] if variant != "on" else server.MARKET_REGIME["store"].event_rows()
        finally:
            for p in patches:
                p.stop()
            self.stop_regime()
        return out, events

    def check_same(self, expected):
        off, _ = self.run_variant("off")
        on, events = self.run_variant("on")
        with self.assertLogs("granmago", "WARNING") as logs:
            failing, _ = self.run_variant("failing")
        self.assertTrue(off[0][0], "scenario must open something")
        self.assertEqual(on, off)
        self.assertEqual(failing, off)
        self.assertTrue(all(r.levelname == "WARNING" for r in logs.records if "disk" in r.getMessage()))
        by = {(e["pair"], e["outcome"], e["reason"]) for e in events}
        self.assertEqual(by, expected)
        self.assertEqual({p["pair"] for p in off[0][0]}, {e[0] for e in expected if e[1] == "abierta"})
        close = mr.candle_close_for(int(base.NOW * 1000))
        self.assertTrue(all(e["regime_candle_close_ms"] == close for e in events))
        self.assertTrue(all(e["signal_close_ms"] == int(base.NOW * 1000) for e in events))
        self.assertEqual(len(events), len(by))  # one row per outcome despite two polls
        return events


class SameDecisionsPaperTest(SameDecisionsMixin, unittest.TestCase):
    http = base.BlofinOnlyTest.http
    tearDown = base.BlofinOnlyTest.tearDown

    def setUp(self):
        base.BlofinOnlyTest.setUp(self)
        capture_add_log(self)
        server.STATE["settings"]["maxPositions"] = 3

    def reset_book(self):
        server.STATE = server.default_state()
        server.STATE.update(running=True, balance=1023.05, history=[], openPositions=[])
        server.STATE["settings"]["maxPositions"] = 3

    def test_same_decisions_with_and_without_the_log(self):
        self.check_same({("XMR-USDT", "abierta", "entrada"), ("DOGE-USDT", "descartada", "fuera_de_stop_objetivo"),
                         ("ETH-USDT", "abierta", "entrada"), ("SOL-USDT", "abierta", "entrada"),
                         ("LINK-USDT", "descartada", "max_positions"),
                         # second poll with the book full: DOGE is now left out by max_positions too
                         ("DOGE-USDT", "descartada", "max_positions")})


class SameDecisionsDemoTest(SameDecisionsMixin, unittest.TestCase):
    http = base.BlofinOnlyTest.http
    demo = tds.DemoServerTest.demo
    tearDown = tds.DemoServerTest.tearDown

    def setUp(self):
        tds.DemoServerTest.setUp(self)
        capture_add_log(self)

    def reset_book(self):
        from fake_blofin import FakeBlofin
        server.init_trading({})
        self.fake = self.exchange = FakeBlofin()
        server.STATE = server.default_state()
        server.STATE.update(running=True, balance=1023.05, history=[], openPositions=[])
        server.STATE["settings"]["maxPositions"] = 3
        self.demo()

    def test_same_decisions_and_orders_in_demo(self):
        # the fake exchange only lists XMR: the other entries are refused by Blofin
        events = self.check_same({("XMR-USDT", "abierta", "entrada"),
                                  ("DOGE-USDT", "descartada", "fuera_de_stop_objetivo"),
                                  ("ETH-USDT", "descartada", "orden_no_ejecutada"),
                                  ("SOL-USDT", "descartada", "orden_no_ejecutada"),
                                  ("LINK-USDT", "descartada", "orden_no_ejecutada")})
        self.assertTrue(self.exchange.orders)
        self.assertTrue(all(e["mode"] == "demo" for e in events))


class ExportTest(RegimeEngineMixin, t180.Base):
    def setUp(self):
        super().setUp()
        server.FISCAL_EXPORT_WAKE.clear()

    def test_regime_and_signal_files_are_exported_apart(self):
        store = self.start_regime()
        store.record(mr.compute(upto(LAST_CLOSE), LAST_CLOSE), "demo", now_ms=LAST_CLOSE + 60000, engine_version="1.8.4")
        store.record_event(now_ms=LAST_CLOSE + 70000, mode="demo", pair="XMR-USDT", side="long",
                           signal_close_ms=LAST_CLOSE, outcome="descartada", reason="max_positions", detail="3/3")
        self.assertTrue(server.fiscal_export_now("test"))  # no fiscal journal yet: still exported
        for where in (self.staging, self.dest):
            names = sorted(p.name for p in where.iterdir())
            self.assertEqual(names, ["manifest.json", "market_regime.csv", "signal_events.csv"], where)
            with open(str(where / "market_regime.csv"), newline="") as fh:
                rows = list(csv.DictReader(fh))
            self.assertEqual(list(rows[0]), mr.REGIME_COLUMNS)
            self.assertEqual((rows[0]["candle_close_utc"], rows[0]["regime"]), (mr.utc_iso(LAST_CLOSE), "movido"))
            with open(str(where / "signal_events.csv"), newline="") as fh:
                ev = list(csv.DictReader(fh))
            self.assertEqual(list(ev[0]), mr.EVENT_EXPORT_COLUMNS)
            self.assertEqual((ev[0]["reason"], ev[0]["regime"]), ("max_positions", "movido"))
            man = json.loads((where / "manifest.json").read_text())
            self.assertEqual({f["file"]: f["rows"] for f in man["files"]},
                             {"market_regime.csv": 1, "signal_events.csv": 1})

    def test_journal_export_unchanged_and_regime_failure_does_not_block_it(self):
        self.close_one()
        store = self.start_regime()
        with patch.object(mr.RegimeStore, "export_tables", side_effect=sqlite3.OperationalError("locked")), \
                self.assertLogs("granmago", "WARNING"):
            self.assertTrue(server.fiscal_export_now("test"))
        self.assertEqual(sorted(p.name for p in self.dest.iterdir()),
                         ["fiscal_journal_demo.csv", "fiscal_journal_demo.sqlite", "manifest.json"])
        self.assertTrue(server.fiscal_export_now("test2"))
        self.assertEqual(sorted(p.name for p in self.dest.iterdir()),
                         ["fiscal_journal_demo.csv", "fiscal_journal_demo.sqlite", "manifest.json",
                          "market_regime.csv", "signal_events.csv"])
        self.assertEqual(store.event_rows(), [])  # close_one ran before the store existed

    def test_unknown_extra_name_is_refused(self):
        with self.assertRaises(fiscal_export.ExportError):
            fiscal_export.build_local([], self.staging, extra_csv=[("ledger.csv", ["a"], [])])

    def test_orphan_tmp_of_extras_is_cleaned(self):
        self.assertTrue(fiscal_export._is_orphan_tmp(".market_regime.csv.abc.tmp"))
        self.assertTrue(fiscal_export._is_orphan_tmp(".signal_events.csv.abc.tmp"))


class PanelTest(unittest.TestCase):
    def test_panel_shows_the_regime_read_only(self):
        node, env = tpc._jsdom_env()
        if not node:
            self.skipTest("node/jsdom no disponibles")
        snap = json.loads(json.dumps(server.default_state()))
        snap.update(tradingMode="demo", ledgerMode="demo", display={}, trading={"mode": "demo"}, alerts=[],
                    market={}, killSwitch=False, marketRegime=None)
        row = mr.compute(upto(LAST_CLOSE), LAST_CLOSE)
        reg = dict(snap, marketRegime={k: row.get(k) for k in ("candle_close_utc", "regime", "vr", "mv", "n_symbols",
                                                               "n_basket", "missing")})
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "snaps.json"
            path.write_text(json.dumps({"none": snap, "reg": reg}), encoding="utf-8")
            proc = subprocess.run([node, str(tpc.HERE / "panel_render.js"), str(tpc.INDEX), str(path)],
                                  capture_output=True, text=True, env=env, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        out = json.loads(proc.stdout)
        for name in out:
            self.assertEqual(out[name]["errors"], [], name)
        self.assertIn("sin registro todavía", out["none"]["texts"]["regimeRule"])
        text = out["reg"]["texts"]["regimeRule"]
        self.assertIn("movido · vr 1,18 · mv 1,23 · 27/27 símbolos", text)
        self.assertIn("no cambia ninguna decisión", text)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    unittest.main()
