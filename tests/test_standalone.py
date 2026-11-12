"""Standalone box runner: env defaults, journal hook, status, and a real start on 127.0.0.1."""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault('DATA_DIR', tempfile.mkdtemp(prefix='granmago-standalone-test-'))
sys.path.insert(0, str(ROOT / 'granmago'))
sys.path.insert(0, str(ROOT / 'standalone'))
import server
import fiscal_journal
import granmago_standalone as gs

gs.server = server


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


class EnvDefaultsTest(unittest.TestCase):
    def test_defaults_match_addon_config(self):
        env = gs.apply_env_defaults({'HA_TOKEN': 'x', 'SUPERVISOR_TOKEN': 'y', 'RISK_PCT': '0.5'})
        self.assertEqual(env['PORT'], '8797')
        self.assertEqual(env['DATA_DIR'], '/workspace/granmago-box/data')
        self.assertEqual(env['RISK_PCT'], '0.5')  # explicit env wins
        self.assertEqual(env['LEVERAGE'], '3')
        self.assertEqual(env['MAX_POSITIONS'], '3')
        self.assertEqual(env['FEE_PCT_PER_SIDE'], '0.06')
        self.assertEqual(env['SLIPPAGE_PCT_PER_SIDE'], '0.02')
        self.assertEqual(env['PAIRS'], '')
        self.assertNotIn('HA_TOKEN', env)
        self.assertNotIn('SUPERVISOR_TOKEN', env)

    def test_bind_defaults_to_loopback(self):
        self.assertEqual(gs.bind_host({}), '127.0.0.1')
        self.assertEqual(gs.bind_host({'GRANMAGO_HOST': ''}), '127.0.0.1')

    def test_addon_files_untouched_by_standalone(self):
        # The standalone layer must not need new imports in the add-on image.
        src = (ROOT / 'granmago' / 'server.py').read_text()
        self.assertNotIn('fiscal_journal', src)
        self.assertNotIn('granmago_standalone', src)


class JournalHookTest(unittest.TestCase):
    def setUp(self):
        self.saved = server.STATE
        server.STATE = server.default_state()
        server.STATE.update(running=True, balance=1000.0, history=[], openPositions=[{
            'pair': 'XMR-USDT', 'side': 'long', 'entry': 600.0, 'notional': 300.0, 'entryFee': 0.18,
            'riskAmt': 10.0, 'initialRiskAmt': 10.0, 'openRiskAmt': 10.0, 'sl': 580.0,
            'time': '2026-11-09T12:00:00+01:00', 'tradeId': 41}])
        self.fj = fiscal_journal.FiscalJournal(Path(tempfile.mkdtemp()) / 'j.sqlite', engine_version=server.VERSION)
        gs.install_journal(server, self.fj)
        self.addCleanup(gs.uninstall_journal, server)

    def tearDown(self):
        server.STATE = self.saved

    def test_close_is_journaled_once(self):
        self.assertTrue(getattr(server.close_position, '_fiscal_wrapped', False))
        with server.LOCK:
            pos = server.close_position('TP', 620.0, 'XMR-USDT')
        self.assertIsNotNone(pos)
        rows = self.fj.rows()
        self.assertEqual(len(rows), 1)
        hist = server.STATE['history'][0]
        self.assertEqual(rows[0]['pnl_usdt'], hist['pnl'])
        self.assertEqual(rows[0]['exit_price'], hist['exit'])
        self.assertEqual(rows[0]['local_trade_id'], 41)
        self.assertEqual(rows[0]['fx_status'], 'pending')
        with server.LOCK:  # nothing open any more: no new row
            self.assertIsNone(server.close_position('TP', 620.0, 'XMR-USDT'))
        self.assertEqual(len(self.fj.rows()), 1)

    def test_journal_failure_does_not_break_close(self):
        def boom(*a, **k):
            raise RuntimeError('disk full')
        self.fj.record_close = boom
        with server.LOCK:
            self.assertIsNotNone(server.close_position('SL', 590.0, 'XMR-USDT'))
        self.assertEqual(server.STATE['openPositions'], [])
        self.assertTrue(gs._JOURNAL_ERRORS)
        gs._JOURNAL_ERRORS.clear()


class StatusTest(unittest.TestCase):
    def test_status_fields_and_atomic_write(self):
        fj = fiscal_journal.FiscalJournal(Path(tempfile.mkdtemp()) / 'j.sqlite')
        st = gs.build_status(server, fj)
        for key in ('health', 'version', 'mode', 'openPositions', 'balance', 'lastCycleAt',
                    'recentErrors', 'uptimeSeconds', 'journal'):
            self.assertIn(key, st)
        self.assertEqual(st['mode'], 'paper')
        self.assertFalse(st['realMoney'])
        self.assertEqual(st['version'], server.VERSION)
        out = Path(tempfile.mkdtemp()) / 'status.json'
        gs.write_status(out, st)
        self.assertEqual(json.loads(out.read_text())['version'], server.VERSION)
        self.assertFalse(out.with_name('status.json.tmp').exists())


class StandaloneStartTest(unittest.TestCase):
    """Starts the real runner in a subprocess (autostart off: no scanning, no network)."""

    def test_start_serves_status_on_loopback_only(self):
        data = Path(tempfile.mkdtemp(prefix='gm-sa-'))
        port = free_port()
        env = {k: v for k, v in os.environ.items() if k not in ('DATA_DIR', 'PORT')}
        env.update(DATA_DIR=str(data), PORT=str(port), GRANMAGO_AUTOSTART='0', GRANMAGO_STATUS_INTERVAL='0.5',
                   GRANMAGO_FX_INTERVAL='3600', HA_TOKEN='must-not-leak')
        proc = subprocess.Popen([sys.executable, str(ROOT / 'standalone' / 'granmago_standalone.py')],
                                env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        try:
            body = None
            for _ in range(60):
                try:
                    with urllib.request.urlopen('http://127.0.0.1:%d/status' % port, timeout=2) as r:
                        body = json.loads(r.read())
                    break
                except OSError:
                    time.sleep(0.25)
            self.assertIsNotNone(body, 'standalone did not answer /status')
            self.assertEqual(body['version'], server.VERSION)
            self.assertEqual(body['mode'], 'paper')
            self.assertEqual(body['bind'], '127.0.0.1:%d' % port)
            self.assertEqual(body['health'], 'stopped')
            self.assertEqual(body['config']['pairs'], 28)
            self.assertEqual(body['config']['maxPositions'], 3)
            self.assertEqual(body['config']['leverage'], 3)
            self.assertEqual(body['config']['takeProfitR'], 1.7)
            self.assertNotIn('must-not-leak', json.dumps(body))
            for _ in range(20):
                if (data / 'status.json').exists():
                    break
                time.sleep(0.25)
            self.assertEqual(json.loads((data / 'status.json').read_text())['mode'], 'paper')
            self.assertTrue((data / 'fiscal_journal.sqlite').exists())
            # Not reachable through a non-loopback address.
            try:
                ip = socket.gethostbyname(socket.gethostname())
            except OSError:
                ip = None
            if ip and not ip.startswith('127.'):
                with self.assertRaises(OSError):
                    socket.create_connection((ip, port), timeout=1).close()
        finally:
            proc.terminate()
            try:
                out, _ = proc.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                out, _ = proc.communicate()
        self.assertNotIn(b'must-not-leak', out)


if __name__ == '__main__':
    unittest.main()
