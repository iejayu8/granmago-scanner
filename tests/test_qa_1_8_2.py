"""1.8.2 (pasada de calidad 21/11/2026).

1. A NAS copy that timed out and resumed after a newer local export mixed two
   builds on the NAS: sqlite of the old build, CSV + manifest of the new one
   (the manifest SHA-256 did not match the sqlite next to it).
2. Blofin funding history malformed (data an object instead of a list, or a bill
   without a readable amount) was booked as a known 0 USDT (funding_status
   'exchange') instead of 'unavailable'.
3. A copy OSError was logged as "copia a X fallida: copia a X fallida: ...".
"""
import json
import os
import sqlite3
import threading
from pathlib import Path
from unittest.mock import patch

import test_blofin_trading as tbt
import test_fiscal_export_181 as t181

server = t181.server
fiscal_export = t181.fiscal_export
sha = t181.sha


class ResumedCopyTest(t181.t180.Base):
    def setUp(self):
        super().setUp()
        self.close_one()
        server.FISCAL_EXPORT_WAKE.clear()

    def book_another_trade(self):
        db = sqlite3.connect(str(Path(self.tmp.name) / "fiscal_journal_demo.sqlite"))
        cols = [c[1] for c in db.execute("PRAGMA table_info(trades)") if c[1] != "seq"]
        db.execute("INSERT INTO trades (%s) SELECT %s FROM trades LIMIT 1" % (
            ",".join(cols), ",".join("trade_key||'-2'" if c == "trade_key" else c for c in cols)))
        db.commit()
        db.close()

    def test_resumed_hung_copy_never_mixes_two_builds(self):
        gate, stuck = threading.Event(), threading.Event()
        real = fiscal_export._copy_one

        def hang_after_sqlite(src, dest):  # SMB hangs right after the sqlite reached the NAS
            out = real(src, dest)
            if out.endswith(".sqlite"):
                stuck.set()
                gate.wait(10)
            return out
        server.FISCAL_EXPORT["timeout"] = 0.3
        with patch.object(fiscal_export, "_copy_one", hang_after_sqlite):
            self.assertFalse(server.fiscal_export_now("t1"))
            self.assertTrue(stuck.wait(5))
            self.book_another_trade()
            self.assertFalse(server.fiscal_export_now("t2"))  # new local build; copy still stuck
            gate.set()
            fiscal_export._COPY_STATE["thread"].join(5)
        man = json.loads((self.dest / "manifest.json").read_text())
        for f in man["files"]:
            self.assertEqual(f["sha256"], sha(self.dest / f["file"]), f["file"])
        self.assertEqual(sorted(p.name for p in self.dest.iterdir()), t181.FILES)  # no .tmp left
        # The staging has the new build and the next copy brings the NAS up to date.
        self.assertTrue(server.fiscal_export_now("t3"))
        for name in t181.FILES:
            self.assertEqual(sha(self.staging / name), sha(self.dest / name), name)
        con = sqlite3.connect("file:%s?mode=ro" % (self.dest / "fiscal_journal_demo.sqlite"), uri=True)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM trades").fetchone()[0], 2)
        con.close()

    def test_copy_oserror_is_reported_once_with_its_cause(self):
        real = fiscal_export._copy_files

        def full(staging, dest, names):
            with patch.object(fiscal_export.os, "fsync", side_effect=OSError(28, "No space left on device")):
                return real(staging, dest, names)
        (self.dest / "fiscal_journal_demo.csv").write_text("OLD")
        with patch.object(fiscal_export, "_copy_files", full):
            self.assertFalse(server.fiscal_export_now("t"))
        msg = server.FISCAL_EXPORT["lastError"]
        self.assertEqual(msg.count("fallida"), 1, msg)
        self.assertIn("No space left on device", msg)
        self.assertEqual((self.dest / "fiscal_journal_demo.csv").read_text(), "OLD")  # never a partial file
        self.assertEqual(sorted(p.name for p in self.dest.iterdir()), ["fiscal_journal_demo.csv"])


class FundingParsingTest(tbt.TraderCase):
    def answer(self, data):
        self.fake.h_get_account_funding_fees = lambda p, b: data

    def test_well_formed_answers(self):
        self.assertAlmostEqual(self.trader.funding("XMR-USDT", 0, 1), -0.05)
        self.answer({"code": "0", "msg": "success", "data": []})
        self.assertEqual(self.trader.funding("XMR-USDT", 0, 1), 0.0)  # no bill: really 0
        self.answer({"code": "0", "data": [{"fundingFee": "-0.1"}, {"fundingFee": "0.04"}]})
        self.assertAlmostEqual(self.trader.funding("XMR-USDT", 0, 1), -0.06)

    def test_malformed_answers_are_unavailable_not_zero(self):
        self.answer({"code": "0", "msg": "success"})                       # no data: no bills (1.7.2)
        self.assertEqual(self.trader.funding("XMR-USDT", 0, 1), 0.0)
        for data in ({"code": "0", "data": {"fundingFee": "-0.1"}},        # object, not a list
                     {"code": "0", "data": [{"fundingFee": "abc"}]},       # unreadable amount
                     {"code": "0", "data": [{"fundingFee": "-0.1"}, {"fundingFee": "NaN"}]},
                     {"code": "0", "data": [{"fundingFee": "-0.1"}, "junk"]}):
            self.answer(data)
            self.assertIsNone(self.trader.funding("XMR-USDT", 0, 1), data)
