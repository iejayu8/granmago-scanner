"""1.8.0: fiscal trace per trade and export of the journal to /share.

Simulated with FakeBlofin (no network, no real account): a demo close and the
same close booked into a 'real' journal, the export (sqlite backup + CSV +
manifest), interval and after-close triggers, a missing/unmounted path and
the absence of secrets in every exported byte.
"""
import csv
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_blofin_only as base  # noqa: E402
import test_demo_server as tds  # noqa: E402
from fake_blofin import KEY, SECRET, PASSPHRASE  # noqa: E402

server = base.server
import fiscal_journal  # noqa: E402
import fiscal_export  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PAIR = base.PAIR
TOKEN = "control-token-777abc"
FISCAL = ("entry_order_id", "entry_trade_ids", "exit_order_id", "exchange_trade_ids", "entry_fee_usdt",
          "exit_fee_usdt", "fee_source", "funding_usdt", "funding_status", "open_time_utc", "close_time_utc",
          "open_time_source", "close_time_source", "missing_fields")


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def journal_rows(path):
    con = sqlite3.connect(str(path))
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute("SELECT * FROM trades ORDER BY seq")]
    finally:
        con.close()


class Base(unittest.TestCase):
    http = base.BlofinOnlyTest.http
    demo = tds.DemoServerTest.demo
    enter = tds.DemoServerTest.enter

    def setUp(self):
        tds.DemoServerTest.setUp(self)
        self.share = tempfile.TemporaryDirectory()
        self.dest = Path(self.share.name) / "granmago-fiscal"
        self.dest.mkdir()
        self.saved = dict(server.FISCAL_EXPORT)
        server.FISCAL_EXPORT_WAKE.clear()
        server.configure_fiscal_export({"FISCAL_EXPORT_DIR": str(self.dest), "FISCAL_EXPORT_MINUTES": "15"})
        self.env = patch.dict(os.environ, {"CONTROL_TOKEN": TOKEN})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        server.FISCAL_EXPORT.clear()
        server.FISCAL_EXPORT.update(self.saved)
        server.FISCAL_EXPORT_WAKE.clear()
        server._clear_alert(server.FISCAL_EXPORT_ALERT_KEY)
        tds.DemoServerTest.tearDown(self)
        self.share.cleanup()

    def close_one(self, real=False):
        self.demo()
        if real:  # live is blocked: same Blofin flow booked into the 'real' journal
            server.JOURNAL = fiscal_journal.FiscalJournal(fiscal_journal.journal_path(self.tmp.name, "real"),
                                                          mode="real", engine_version=server.VERSION)
        self.enter()
        pos = dict(server.STATE["openPositions"][0])
        self.fake.trigger_stop(PAIR)
        server.trading_cycle(force_reconcile=True)
        self.assertEqual(server.STATE["openPositions"], [])
        return pos


class FiscalFieldsTest(Base):
    def check_row(self, mode, pos):
        rows = journal_rows(Path(self.tmp.name) / ("fiscal_journal_%s.sqlite" % mode))
        self.assertEqual(len(rows), 1)
        r = rows[0]
        entry = [f for f in self.fake.fills if f["orderId"] == pos["entryOrderId"]]
        exits = [f for f in self.fake.fills if f["orderId"] != pos["entryOrderId"]]
        self.assertEqual((r["mode"], r["fiscal"]), (mode, 1 if mode == "real" else 0))
        self.assertEqual(r["entry_order_id"], pos["entryOrderId"])
        self.assertEqual(r["entry_trade_ids"], ",".join(sorted(f["tradeId"] for f in entry)))
        self.assertEqual(r["exit_order_id"], ",".join(sorted({f["orderId"] for f in exits})))
        self.assertEqual(r["exchange_trade_ids"], ",".join(sorted(f["tradeId"] for f in exits)))
        self.assertAlmostEqual(r["entry_fee_usdt"], sum(abs(float(f["fee"])) for f in entry), places=8)
        self.assertAlmostEqual(r["exit_fee_usdt"], sum(abs(float(f["fee"])) for f in exits), places=8)
        self.assertAlmostEqual(r["fees_usdt"], r["entry_fee_usdt"] + r["exit_fee_usdt"], places=5)
        self.assertEqual(r["fee_source"], "blofin_fills")
        self.assertEqual((r["funding_usdt"], r["funding_status"]), (-0.05, "exchange"))
        open_ms = min(int(f["ts"]) for f in entry)
        close_ms = max(int(f["ts"]) for f in exits)
        self.assertEqual(r["open_time_utc"], fiscal_journal._utc_iso(fiscal_journal._parse_time(open_ms)))
        self.assertEqual(r["close_time_utc"], fiscal_journal._utc_iso(fiscal_journal._parse_time(close_ms)))
        self.assertEqual((r["open_time_source"], r["close_time_source"]), ("blofin_fill", "blofin_fill"))
        self.assertEqual(r["missing_fields"], "")
        self.assertEqual(r["row_hash_version"], 2)
        return r

    def test_demo_trade_has_full_blofin_trace(self):
        pos = self.close_one()
        self.check_row("demo", pos)
        self.assertTrue(server.FISCAL_EXPORT_WAKE.is_set())  # a close asks for an export

    def test_real_trade_has_full_blofin_trace(self):
        pos = self.close_one(real=True)
        self.check_row("real", pos)

    def test_missing_blofin_data_is_null_and_flagged(self):
        self.demo()
        self.enter()
        server.TRADER.funding = lambda *a, **k: None
        server.TRADER.entry_fills = lambda *a, **k: None
        self.fake.trigger_stop(PAIR)
        server.trading_cycle(force_reconcile=True)
        r = journal_rows(Path(self.tmp.name) / "fiscal_journal_demo.sqlite")[0]
        self.assertEqual(r["funding_status"], "unavailable")
        self.assertEqual(r["entry_trade_ids"], "")
        self.assertEqual(r["fee_source"], "blofin_order_detail+fills")
        for f in ("funding_usdt", "entry_trade_ids", "fee_source", "open_time_fill"):
            self.assertIn(f, r["missing_fields"].split(","))
        server.fiscal_export_now("test")
        with open(self.dest / "fiscal_journal_demo.csv", newline="", encoding="utf-8") as fh:
            row = next(csv.DictReader(fh))
        self.assertEqual(row["funding_usdt"], "")  # not a made-up 0
        self.assertEqual(row["funding_status"], "unavailable")

    def test_funding_beyond_7_days_is_flagged_partial(self):
        self.demo()
        self.enter()
        server.STATE["openPositions"][0]["openedMs"] -= 8 * 86400 * 1000
        self.fake.trigger_stop(PAIR)
        server.trading_cycle(force_reconcile=True)
        r = journal_rows(Path(self.tmp.name) / "fiscal_journal_demo.sqlite")[0]
        self.assertEqual(r["funding_status"], "exchange_partial_7d")
        self.assertIn("funding_window", r["missing_fields"])


class SchemaMigrationTest(unittest.TestCase):
    def test_v1_rows_untouched_and_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "fiscal_journal_demo.sqlite"
            con = sqlite3.connect(str(path))
            con.executescript(fiscal_journal.DDL)
            v1 = fiscal_journal.HASH_V1_COLUMNS
            vals = {c: None for c in v1}
            vals.update(trade_key="k1", recorded_at_utc="2026-11-01T00:00:00Z", mode="demo", fiscal=0,
                        engine_version="1.7.7", instance="ha", leg=1, partial=0, close_time_utc="x",
                        close_time_madrid="x", close_date_madrid="2026-11-01", pair="SOL-USDT", side="short",
                        notional_usdt=1.0, entry_price=1.0, exit_price=1.0, fees_usdt=0.1, funding_usdt=0.0,
                        funding_status="pending", pnl_usdt=1.0, entry_order_id="", exit_order_id="",
                        client_order_id="", tpsl_id="", exchange_trade_ids="", raw_json="{}", row_sha256="abc")
            con.execute("INSERT INTO trades (%s) VALUES (%s)" % (",".join(v1), ",".join("?" * len(v1))),
                        [vals[c] for c in v1])
            con.execute("INSERT INTO meta VALUES ('schema_version','1')")
            con.commit()
            con.close()
            for _ in range(2):
                fiscal_journal.FiscalJournal(path, mode="demo")
            rows = journal_rows(path)
            self.assertEqual(len(rows), 1)
            self.assertEqual({c: rows[0][c] for c in v1}, vals)
            self.assertTrue(all(rows[0][c] is None for c, _ in fiscal_journal.V2_COLUMNS))
            con = sqlite3.connect(str(path))
            self.assertEqual(con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0], "2")
            with self.assertRaises(sqlite3.DatabaseError):  # still append-only
                con.execute("UPDATE trades SET pnl_usdt = 0")
            con.close()


class ExportTest(Base):
    def test_close_then_export_sqlite_csv_manifest(self):
        self.close_one()
        src = Path(self.tmp.name) / "fiscal_journal_demo.sqlite"
        before = sha(src), journal_rows(src)
        self.assertTrue(server.fiscal_export_step())  # woken by the close
        self.assertFalse(server.FISCAL_EXPORT_WAKE.is_set())
        self.assertEqual(sorted(p.name for p in self.dest.iterdir()),
                         ["fiscal_journal_demo.csv", "fiscal_journal_demo.sqlite", "manifest.json"])  # no .tmp left
        self.assertEqual((sha(src), journal_rows(src)), before)  # original never modified
        copy = self.dest / "fiscal_journal_demo.sqlite"
        self.assertEqual(journal_rows(copy), before[1])
        con = sqlite3.connect("file:%s?mode=ro" % copy, uri=True)
        self.assertEqual(con.execute("PRAGMA journal_mode").fetchone()[0], "delete")
        trig = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
        self.assertIn("trades_no_update", trig)
        con.close()
        with open(self.dest / "fiscal_journal_demo.csv", newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["mode"], "demo")
        for f in FISCAL:
            self.assertIn(f, rows[0])
        man = json.loads((self.dest / "manifest.json").read_text())
        self.assertEqual(man["engineVersion"], server.VERSION)
        self.assertEqual({(f["file"], f["mode"], f["rows"]) for f in man["files"]},
                         {("fiscal_journal_demo.sqlite", "demo", 1), ("fiscal_journal_demo.csv", "demo", 1)})
        for f in man["files"]:
            self.assertEqual(f["sha256"], sha(self.dest / f["file"]))
        self.assertEqual(oct((self.dest / "fiscal_journal_demo.csv").stat().st_mode & 0o777), "0o644")
        health = self.client.get("/api/health").get_json()["fiscalExport"]
        self.assertEqual((health["dir"], health["lastError"]), (str(self.dest), None))

    def test_demo_and_real_journals_both_exported_with_mode(self):
        self.close_one(real=True)
        fiscal_journal.FiscalJournal(fiscal_journal.journal_path(self.tmp.name, "demo"), mode="demo")
        self.assertTrue(server.fiscal_export_now())
        with open(self.dest / "fiscal_journal_real.csv", newline="", encoding="utf-8") as fh:
            self.assertEqual([r["mode"] for r in csv.DictReader(fh)], ["real"])
        self.assertTrue((self.dest / "fiscal_journal_demo.sqlite").exists())

    def test_interval(self):
        self.close_one()
        server.FISCAL_EXPORT_WAKE.clear()
        t = time.monotonic()
        server.FISCAL_EXPORT["lastAttemptMono"] = t
        self.assertIsNone(server.fiscal_export_step(t + 60))
        self.assertFalse((self.dest / "manifest.json").exists())
        self.assertTrue(server.fiscal_export_step(t + 15 * 60))
        self.assertEqual(server.FISCAL_EXPORT["reason"], "intervalo")
        server.FISCAL_EXPORT.update(minutes=0, lastAttemptMono=None)  # 0 = only after closes
        self.assertFalse(server.fiscal_export_due(t + 10 ** 6))
        server.request_fiscal_export("cierre X")
        self.assertTrue(server.fiscal_export_due())

    def test_missing_path_is_one_deduplicated_alert_and_trading_goes_on(self):
        self.close_one()
        nas = Path(self.share.name) / "nas_ugreen" / "granmago-fiscal"  # NAS not mounted
        server.configure_fiscal_export({"FISCAL_EXPORT_DIR": str(nas), "FISCAL_EXPORT_MINUTES": "15"})
        with self.assertLogs("granmago", "WARNING"):
            for _ in range(3):
                self.assertFalse(server.fiscal_export_now("test"))
        self.assertFalse(nas.parent.exists())  # never created locally
        alerts = [a for a in server.ALERTS if a.get("key") == server.FISCAL_EXPORT_ALERT_KEY]
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["count"], 3)
        self.assertIn("no existe", alerts[0]["msg"])
        server.trading_cycle(force_reconcile=True)  # trading unaffected
        self.assertTrue(server.TRADER.safe)
        server.configure_fiscal_export({"FISCAL_EXPORT_DIR": str(self.dest)})
        self.assertTrue(server.fiscal_export_now("test"))
        self.assertFalse([a for a in server.ALERTS if a.get("key") == server.FISCAL_EXPORT_ALERT_KEY])

    def test_default_dir_created_only_under_existing_share(self):
        self.close_one()
        default = Path(self.share.name) / "granmago-fiscal-default"
        with patch.object(fiscal_export, "DEFAULT_DIR", str(default)):
            self.assertEqual(fiscal_export.resolve_dest(str(default), self.tmp.name), default)
            self.assertTrue(default.is_dir())
        with self.assertRaises(fiscal_export.ExportError):  # never inside /data
            fiscal_export.resolve_dest(str(Path(self.tmp.name) / "x"), self.tmp.name)
        with self.assertRaises(fiscal_export.ExportError):
            fiscal_export.resolve_dest("relative/path", self.tmp.name)

    def test_no_secrets_in_export(self):
        self.close_one()
        self.assertTrue(server.fiscal_export_now("test"))
        for p in self.dest.iterdir():
            data = p.read_bytes()
            for s in (KEY, SECRET, PASSPHRASE, TOKEN):
                self.assertNotIn(s.encode(), data, (p.name, s))

    def test_export_refuses_a_secret_that_leaked_into_a_row(self):
        self.demo()
        j = fiscal_journal.FiscalJournal(Path(self.tmp.name) / "fiscal_journal_demo.sqlite", mode="demo")
        j.record_close({"pair": PAIR, "side": "long", "entry": 1, "exit": 1, "pnl": 0, "reason": SECRET,
                        "tradeId": 9}, {"time": "2026-11-20T08:00:00+01:00"})
        self.assertFalse(server.fiscal_export_now("test"))
        self.assertEqual(list(self.dest.iterdir()), [])  # nothing published, no temp left
        self.assertIn("secreto detectado", server.FISCAL_EXPORT["lastError"])
        self.assertNotIn(SECRET, server.FISCAL_EXPORT["lastError"])


class OptionsTest(unittest.TestCase):
    def test_options_and_defaults(self):
        cfg = (ROOT / "granmago" / "config.yaml").read_text(encoding="utf-8")
        for line in ("map:\n  - share:rw", "fiscal_export_dir: /share/granmago-fiscal", "fiscal_export_minutes: 15",
                     "fiscal_export_dir: str?", "fiscal_export_minutes: int(0,1440)"):
            self.assertIn(line, cfg)
        run = (ROOT / "granmago" / "run.sh").read_text(encoding="utf-8")
        self.assertIn('FISCAL_EXPORT_DIR="$(bashio::config \'fiscal_export_dir\')"', run)
        self.assertIn('FISCAL_EXPORT_MINUTES="$(bashio::config \'fiscal_export_minutes\')"', run)
        saved = dict(server.FISCAL_EXPORT)
        try:
            self.assertEqual(server.configure_fiscal_export({})["dir"], "/share/granmago-fiscal")
            self.assertEqual(server.FISCAL_EXPORT["minutes"], 15)
            c = server.configure_fiscal_export({"FISCAL_EXPORT_DIR": "/share/nas_ugreen/granmago",
                                                "FISCAL_EXPORT_MINUTES": "99999"})
            self.assertEqual((c["dir"], c["minutes"]), ("/share/nas_ugreen/granmago", 1440))
        finally:
            server.FISCAL_EXPORT.update(saved)


if __name__ == "__main__":
    unittest.main()
