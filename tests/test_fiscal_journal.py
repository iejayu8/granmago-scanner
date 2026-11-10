"""Append-only fiscal journal: immutability, idempotency, FX pending/resolution, CSV."""
import csv
import sqlite3
import sys
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'standalone'))
import fiscal_journal as fjm

CLOSED = {
    'time': '2026-11-09 15:30', 'pair': 'XMR-USDT', 'side': 'long', 'entry': 600.0, 'exit': 612.0,
    'pnl': 5.81, 'reason': 'TP 1.7R', 'rMultiple': 1.62, 'tradePnl': 5.81, 'fees': 0.1812,
    'notional': 300.0, 'tradeId': 7, 'closeMs': 1794132000000,
    'exitMarket': {'source': 'Blofin', 'instId': 'XMR-USDT', 'ts': 1794132000000, 'price': 612.1,
                   'observedAt': '2026-11-09T15:30:05+01:00'},
}
POS = {'pair': 'XMR-USDT', 'side': 'long', 'time': '2026-11-09T12:00:00+01:00', 'tradeId': 7,
       'closedPnls': [5.81], 'entryMarket': {'ts': 1794121200000, 'observedAt': '2026-11-09T12:00:00+01:00'}}
XML = """<?xml version="1.0"?><gesmes:Envelope xmlns:gesmes="http://www.gesmes.org/xml/2002-08-01"
xmlns="http://www.ecb.int/vocabulary/2002-08-01/eurofxref"><Cube>
<Cube time="2026-11-09"><Cube currency="USD" rate="1.1200"/><Cube currency="JPY" rate="177"/></Cube>
<Cube time="2026-11-06"><Cube currency="USD" rate="1.1100"/></Cube></Cube></gesmes:Envelope>"""


class FiscalJournalTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix='gm-fj-'))
        self.fj = fjm.FiscalJournal(self.dir / 'j.sqlite', mode='paper', engine_version='1.6.2')

    def test_row_fields_and_times(self):
        self.assertTrue(self.fj.record_close(CLOSED, POS))
        (row,) = self.fj.rows()
        self.assertEqual(row['close_time_utc'], '2026-11-09T14:30:05Z')
        self.assertEqual(row['close_time_madrid'], '2026-11-09T15:30:05+01:00')
        self.assertEqual(row['open_time_utc'], '2026-11-09T11:00:00Z')
        self.assertEqual(row['mode'], 'paper')
        self.assertEqual(row['fiscal'], 0)
        self.assertEqual(row['entry_order_id'], '')
        self.assertEqual(row['exit_order_id'], '')
        self.assertEqual(row['funding_usdt'], 0.0)
        self.assertEqual(row['funding_status'], 'not_modelled_paper')
        self.assertAlmostEqual(row['quantity_base'], 0.5)
        self.assertEqual(row['pnl_usdt'], 5.81)
        self.assertEqual(row['fx_status'], 'pending')
        self.assertEqual(row['fx_source'], 'pending')
        self.assertIsNone(row['pnl_eur'])
        self.assertEqual(len(row['row_sha256']), 64)

    def test_idempotent(self):
        self.assertTrue(self.fj.record_close(CLOSED, POS))
        self.assertFalse(self.fj.record_close(CLOSED, POS))
        reopened = fjm.FiscalJournal(self.dir / 'j.sqlite', mode='paper', engine_version='1.6.2')
        self.assertFalse(reopened.record_close(CLOSED, POS))
        self.assertEqual(reopened.counts()['rows'], 1)

    def test_partial_legs_are_distinct(self):
        partial = dict(CLOSED, tradePnl=None, pnl=2.0)
        pos1 = dict(POS, closedPnls=[2.0])
        pos2 = dict(POS, closedPnls=[2.0, 5.81])
        self.assertTrue(self.fj.record_close(partial, pos1))
        self.assertTrue(self.fj.record_close(CLOSED, pos2))
        rows = self.fj.rows()
        self.assertEqual([r['leg'] for r in rows], [1, 2])
        self.assertEqual([r['partial'] for r in rows], [1, 0])

    def test_update_and_delete_are_rejected(self):
        self.fj.record_close(CLOSED, POS)
        con = sqlite3.connect(self.dir / 'j.sqlite')
        for sql in ("UPDATE trades SET pnl_usdt = 0", "DELETE FROM trades"):
            with self.assertRaises(sqlite3.DatabaseError, msg=sql):
                con.execute(sql)
        con.execute("INSERT OR REPLACE INTO trades SELECT seq, trade_key, recorded_at_utc, mode, fiscal, "
                    "engine_version, instance, local_trade_id, leg, partial, open_time_utc, open_time_madrid, "
                    "close_time_utc, close_time_madrid, close_date_madrid, pair, side, quantity_base, notional_usdt, "
                    "entry_price, exit_price, fees_usdt, funding_usdt, funding_status, -999, r_multiple, reason, "
                    "entry_order_id, exit_order_id, client_order_id, tpsl_id, exchange_trade_ids, signal_close_ms, "
                    "raw_json, row_sha256 FROM trades")
        con.commit()
        self.fj.resolve_pending(fjm.parse_ecb_xml(XML), today=date(2026, 11, 10))
        for sql in ("UPDATE fx_resolutions SET usd_per_eur = 2", "DELETE FROM fx_resolutions"):
            with self.assertRaises(sqlite3.DatabaseError, msg=sql):
                con.execute(sql)
        con.close()
        self.assertEqual(self.fj.rows()[0]['pnl_usdt'], 5.81)

    def test_fx_resolution_and_eur(self):
        self.fj.record_close(CLOSED, POS)
        self.assertEqual(self.fj.resolve_pending(fjm.parse_ecb_xml(XML), today=date(2026, 11, 9)), 1)
        (row,) = self.fj.rows()
        self.assertEqual(row['fx_status'], 'resolved')
        self.assertEqual(row['fx_rate_date'], '2026-11-09')
        self.assertEqual(row['fx_usd_per_eur'], 1.12)
        self.assertEqual(row['pnl_eur'], round(5.81 / 1.12, 2))
        self.assertIn('ECB', row['fx_source'])
        self.assertEqual(self.fj.resolve_pending(fjm.parse_ecb_xml(XML), today=date(2026, 11, 9)), 0)

    def test_fx_pending_until_today_fixing_and_weekend_fallback(self):
        today_close = dict(CLOSED, exitMarket=dict(CLOSED['exitMarket'], observedAt='2026-11-10T10:00:00+01:00'))
        sat_close = dict(CLOSED, tradeId=8, exitMarket=dict(CLOSED['exitMarket'], observedAt='2026-11-07T10:00:00+01:00'))
        self.fj.record_close(today_close, POS)
        self.fj.record_close(sat_close, dict(POS, tradeId=8))
        rates = fjm.parse_ecb_xml(XML)
        self.assertEqual(self.fj.resolve_pending(rates, today=date(2026, 11, 10)), 1)
        rows = {r['local_trade_id']: r for r in self.fj.rows()}
        self.assertEqual(rows[7]['fx_status'], 'pending')
        self.assertEqual(rows[8]['fx_rate_date'], '2026-11-06')

    def test_source_down_keeps_row_pending(self):
        self.fj.record_close(CLOSED, POS)
        self.assertEqual(self.fj.resolve_pending({}), 0)
        self.assertEqual(self.fj.counts(), {'rows': 1, 'fx_pending': 1})

    def test_csv_export(self):
        self.fj.record_close(CLOSED, POS)
        out = self.dir / 'j.csv'
        self.assertEqual(self.fj.export_csv(out), 1)
        with open(out, newline='') as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual(rows[0]['pair'], 'XMR-USDT')
        self.assertEqual(rows[0]['fx_status'], 'pending')
        self.assertEqual(list(rows[0].keys()), fjm.EXPORT_COLUMNS)

    def test_read_only_reader(self):
        self.fj.record_close(CLOSED, POS)
        con = sqlite3.connect('file:%s?mode=ro' % (self.dir / 'j.sqlite'), uri=True)
        self.assertEqual(con.execute('SELECT COUNT(*) FROM trades_eur').fetchone()[0], 1)
        with self.assertRaises(sqlite3.OperationalError):
            con.execute("INSERT INTO meta VALUES ('x','y')")
        con.close()

    def test_mode_validation(self):
        with self.assertRaises(ValueError):
            fjm.FiscalJournal(self.dir / 'x.sqlite', mode='live')


if __name__ == '__main__':
    unittest.main()
