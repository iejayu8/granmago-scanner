"""1.8.1: fiscal export built in local staging, then copied to the NAS.

1.8.0 ran the SQLite backup straight on the SMB/CIFS destination; it hung and
left a 0-byte .fiscal_journal_demo.sqlite.*.tmp with no CSV, no manifest and
nothing in the log.
"""
import hashlib
import json
import sqlite3
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import test_fiscal_export_180 as t180

server = t180.server
fiscal_export = t180.fiscal_export
fiscal_journal = t180.fiscal_journal
FILES = ["fiscal_journal_demo.csv", "fiscal_journal_demo.sqlite", "manifest.json"]


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


class StagingTest(t180.Base):
    def setUp(self):
        super().setUp()
        self.close_one()
        server.FISCAL_EXPORT_WAKE.clear()

    def staged(self):
        return sorted(p.name for p in self.staging.iterdir())

    def test_staging_and_destination_get_the_same_content(self):
        self.assertTrue(server.fiscal_export_now("test"))
        self.assertEqual(server.FISCAL_EXPORT["staging"], str(self.staging))
        self.assertEqual(self.staged(), FILES)
        self.assertEqual(sorted(p.name for p in self.dest.iterdir()), FILES)
        for name in FILES:
            self.assertEqual(sha(self.staging / name), sha(self.dest / name), name)
        man = json.loads((self.dest / "manifest.json").read_text())
        for f in man["files"]:
            self.assertEqual(f["sha256"], sha(self.dest / f["file"]))

    def test_no_sqlite_write_on_the_destination(self):
        opened = []
        real = sqlite3.connect

        def spy(path, *a, **k):
            opened.append(str(path))
            return real(path, *a, **k)
        with patch.object(fiscal_export.sqlite3, "connect", spy):
            self.assertTrue(server.fiscal_export_now("test"))
        self.assertTrue(opened)
        self.assertFalse([p for p in opened if str(self.dest) in p], opened)

    def test_slow_destination_times_out_and_local_export_stays(self):
        release = threading.Event()
        real_copy = fiscal_export._copy_one

        def hung(src, dest):  # a hung SMB write
            release.wait(10)
            return real_copy(src, dest)
        server.FISCAL_EXPORT["timeout"] = 0.3
        try:
            with patch.object(fiscal_export, "_copy_one", hung), self.assertLogs("granmago", "WARNING") as logs:
                t0 = time.monotonic()
                self.assertFalse(server.fiscal_export_now("test"))
                self.assertLess(time.monotonic() - t0, 3)  # the export thread is not blocked
                self.assertFalse(server.fiscal_export_now("test"))  # still stuck: no second copy
            msg = "\n".join(logs.output)
            self.assertIn("timeout de 0.3s copiando a %s" % self.dest, msg)
            self.assertIn("sigue colgada", msg)
            self.assertIn("queda intacta", msg)
            self.assertEqual(self.staged(), FILES)  # local export complete
            con = sqlite3.connect("file:%s?mode=ro" % (self.staging / "fiscal_journal_demo.sqlite"), uri=True)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM trades").fetchone()[0], 1)
            con.close()
            self.assertFalse(list(self.dest.iterdir()))
            alerts = [a for a in server.ALERTS if a.get("key") == server.FISCAL_EXPORT_ALERT_KEY]
            self.assertEqual(len(alerts), 1)
            server.trading_cycle(force_reconcile=True)  # the engine goes on
            self.assertTrue(server.TRADER.safe)
        finally:
            release.set()
            th = fiscal_export._COPY_STATE["thread"]
            if th is not None:
                th.join(5)
        self.assertTrue(server.fiscal_export_now("test"))  # mount back: copy completes
        self.assertEqual(sorted(p.name for p in self.dest.iterdir()), FILES)

    def test_missing_destination_is_never_created(self):
        for nas in (Path(self.share.name) / "nas_ugreen" / "granmago-fiscal",  # parent missing
                    Path(self.share.name) / "granmago-nas"):                    # folder missing
            server.configure_fiscal_export({"FISCAL_EXPORT_DIR": str(nas)})
            with self.assertLogs("granmago", "WARNING") as logs:
                self.assertFalse(server.fiscal_export_now("test"))
            self.assertIn(str(nas.parent if not nas.parent.exists() else nas), "\n".join(logs.output))
            self.assertIn("no existe", "\n".join(logs.output))
            self.assertFalse(nas.exists())
            self.assertEqual(self.staged(), FILES)

    def test_share_subfolder_must_be_a_mount_point(self):
        share = Path(self.share.name)
        nas = share / "nas_ugreen" / "granmago-fiscal"
        nas.mkdir(parents=True)  # NAS unmounted: empty local mount dir with the folder
        server.configure_fiscal_export({"FISCAL_EXPORT_DIR": str(nas)})
        with patch.object(fiscal_export, "SHARE_ROOT", str(share)):
            with self.assertLogs("granmago", "WARNING") as logs:
                self.assertFalse(server.fiscal_export_now("test"))
            self.assertIn("%s no es un punto de montaje" % (share / "nas_ugreen"), "\n".join(logs.output))
            self.assertFalse(list(nas.iterdir()))
            with patch.object(fiscal_export.os.path, "ismount", lambda p: p == str(share / "nas_ugreen")):
                self.assertTrue(server.fiscal_export_now("test"))
        self.assertEqual(sorted(p.name for p in nas.iterdir()), FILES)

    def test_orphan_tmp_files_are_cleaned(self):
        orphans = [".fiscal_journal_demo.sqlite.k3j2.tmp", ".fiscal_journal_demo.csv.x.tmp", ".manifest.json.ab.tmp"]
        keep = [".other.tmp", "notas.txt"]
        for n in orphans + keep:
            (self.dest / n).write_bytes(b"")  # like the 0-byte tmp 1.8.0 left on the NAS
        (self.staging).mkdir(exist_ok=True)
        (self.staging / ".fiscal_journal_demo.sqlite.old.tmp").write_bytes(b"")
        self.assertTrue(server.fiscal_export_now("test"))
        self.assertEqual(sorted(p.name for p in self.dest.iterdir()), sorted(FILES + keep))
        self.assertEqual(self.staged(), FILES)

    def test_staging_choice(self):
        with patch.object(fiscal_export, "STAGING_DIR", str(Path(self.share.name) / "no" / "share")):
            self.assertEqual(fiscal_export.choose_staging(self.tmp.name),
                             Path(self.tmp.name) / "granmago-fiscal-staging")
        self.assertEqual(fiscal_export.choose_staging(self.tmp.name), self.staging)  # created under /share
        self.assertEqual((fiscal_export.STAGING_DIR, fiscal_export.STAGING_FALLBACK),
                         (str(self.staging), "granmago-fiscal-staging"))

    def test_destination_equal_to_staging_copies_nothing(self):
        server.configure_fiscal_export({"FISCAL_EXPORT_DIR": str(self.staging)})
        with patch.object(fiscal_export, "copy_to_dest", side_effect=AssertionError("no copy")):
            self.assertTrue(server.fiscal_export_now("test"))
        self.assertEqual(self.staged(), FILES)

    def test_secret_in_a_row_publishes_nothing_anywhere(self):
        j = fiscal_journal.FiscalJournal(Path(self.tmp.name) / "fiscal_journal_demo.sqlite", mode="demo")
        j.record_close({"pair": t180.PAIR, "side": "long", "entry": 1, "exit": 1, "pnl": 0, "reason": t180.SECRET,
                        "tradeId": 9}, {"time": "2026-11-20T08:00:00+01:00"})
        self.assertFalse(server.fiscal_export_now("test"))
        self.assertFalse([p for p in self.staging.iterdir() if p.name.endswith(".sqlite")])
        self.assertFalse(list(self.dest.iterdir()))


class OptionsTest(unittest.TestCase):
    def test_timeout_option(self):
        cfg = (t180.ROOT / "granmago" / "config.yaml").read_text(encoding="utf-8")
        self.assertIn('version: "1.8.6"', cfg)
        self.assertIn("fiscal_export_timeout_seconds: 60", cfg)
        self.assertIn("fiscal_export_timeout_seconds: int(5,600)", cfg)
        run = (t180.ROOT / "granmago" / "run.sh").read_text(encoding="utf-8")
        self.assertIn("FISCAL_EXPORT_TIMEOUT_SECONDS=", run)
        saved = dict(server.FISCAL_EXPORT)
        try:
            self.assertEqual(server.configure_fiscal_export({})["timeout"], 60)
            self.assertEqual(server.configure_fiscal_export({"FISCAL_EXPORT_TIMEOUT_SECONDS": "1"})["timeout"], 5)
        finally:
            server.FISCAL_EXPORT.update(saved)


if __name__ == "__main__":
    unittest.main()
