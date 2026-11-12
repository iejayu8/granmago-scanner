"""Box pauses vs real hangs: suspension detection, run-time heartbeat, watchdog restart,
idempotent start, and explicit HTTP timeouts."""
import ast
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault('DATA_DIR', tempfile.mkdtemp(prefix='granmago-liveness-test-'))
sys.path.insert(0, str(ROOT / 'granmago'))
sys.path.insert(0, str(ROOT / 'standalone'))
import server
import granmago_standalone as gs
import heartbeat_check as hb

gs.server = server


class Clock:
    def __init__(self):
        self.wall, self.mono = 1794500000.0, 1000.0

    def run(self, s):
        self.wall += s
        self.mono += s

    def pause(self, s):  # VM frozen: only the wall clock moves (stepped on resume)
        self.wall += s


class SuspendDetectorTest(unittest.TestCase):
    def test_pause_detected_normal_run_not(self):
        c = Clock()
        d = gs.SuspendDetector(30, wall=lambda: c.wall, mono=lambda: c.mono)
        c.run(15)
        self.assertIsNone(d.tick())
        c.run(5)
        c.pause(50 * 60)
        c.run(5)
        ev = d.tick()
        self.assertIsNotNone(ev)
        self.assertEqual(ev['seconds'], 3000.0)
        self.assertEqual(d.as_dict()['count'], 1)
        c.run(15)
        self.assertIsNone(d.tick())

    def test_cycle_age_is_run_time(self):
        c = Clock()
        t = gs.CycleTracker(mono=lambda: c.mono)
        self.assertEqual(t.observe('a'), 0)
        c.run(60)
        c.pause(3000)
        self.assertEqual(t.observe('a'), 60)  # the 50 min pause does not count
        self.assertEqual(t.observe('b'), 0)

    def test_status_health_ignores_pause(self):
        saved_cycle = gs.CYCLE
        c = Clock()
        gs.CYCLE = gs.CycleTracker(mono=lambda: c.mono)
        saved = server.STATE
        server.STATE = server.default_state()
        server.STATE.update(running=True, lastTickAt='2026-11-12T20:22:17+01:00')
        try:
            gs.build_status(server)
            c.pause(3000)
            st = gs.build_status(server)
            self.assertEqual(st['health'], 'ok')  # wall age is huge, run age is ~0
            self.assertGreater(st['lastCycleAgeSeconds'], 600)
            c.run(gs.CYCLE_MAX_RUN_AGE + 1)
            self.assertEqual(gs.build_status(server)['health'], 'stale')
            for k in ('monotonicAt', 'lastCycleRunAgeSeconds', 'suspensions', 'wallUptimeSeconds'):
                self.assertIn(k, st)
        finally:
            server.STATE = saved
            gs.CYCLE = saved_cycle

    def test_on_resume_logs_and_kicks(self):
        saved = server.STATE
        server.STATE = server.default_state()
        gen = server._kick_gen
        try:
            gs.on_resume(server, {'from': 'a', 'to': 'b', 'seconds': 3000})
            self.assertIn('pausa 50 min', server.STATE['log'][0])
            self.assertEqual(server._kick_gen, gen + 1)
        finally:
            server.STATE = saved


class HeartbeatVerdictTest(unittest.TestCase):
    def st(self, **kw):
        base = dict(pid=10, monotonicAt=1000.0, running=True, lastCycleRunAgeSeconds=30)
        base.update(kw)
        return base

    def test_ok(self):
        self.assertEqual(hb.verdict(self.st(), 10, 900, 1010)[0], 0)

    def test_status_stale_in_run_time(self):
        self.assertEqual(hb.verdict(self.st(), 10, 900, 1000 + 301, status_max=300)[0], 1)

    def test_cycle_stale(self):
        self.assertEqual(hb.verdict(self.st(lastCycleRunAgeSeconds=601), 10, 900, 1005, cycle_max=600)[0], 1)
        self.assertEqual(hb.verdict(self.st(lastCycleRunAgeSeconds=601, running=False), 10, 900, 1005)[0], 0)

    def test_grace_and_foreign_pid(self):
        self.assertEqual(hb.verdict(self.st(pid=9), 10, 1000, 1010, grace=180, status_max=300)[0], 2)
        self.assertEqual(hb.verdict(None, 10, 1000, 1400, grace=180, status_max=300)[0], 1)


STUB = """#!/usr/bin/env bash
# Fake python: real python for the heartbeat check, a silent sleeper as "engine".
case "$*" in *heartbeat_check.py*) exec python3 "$@";; esac
echo "stub engine $$"; exec sleep 600
"""


class WatchdogScriptTest(unittest.TestCase):
    def setUp(self):
        self.box = Path(tempfile.mkdtemp(prefix='gm-wd-'))
        stub = self.box / 'fakepython'
        stub.write_text(STUB)
        stub.chmod(0o755)
        self.env = dict(os.environ, GRANMAGO_BOX=str(self.box), DATA_DIR=str(self.box / 'data'),
                        GRANMAGO_PYTHON=str(stub), GRANMAGO_HEARTBEAT_EVERY='1', GRANMAGO_STARTUP_GRACE='2',
                        GRANMAGO_HEARTBEAT_MAX='2', GRANMAGO_MIN_BACKOFF='1', PORT='1')
        (self.box / 'data').mkdir()
        self.ctl = [str(ROOT / 'standalone' / 'granmagoctl.sh')]
        self.addCleanup(lambda: subprocess.run(self.ctl + ['stop'], env=self.env, capture_output=True, timeout=30))

    def run_ctl(self, *args):
        return subprocess.run(self.ctl + list(args), env=self.env, capture_output=True, text=True, timeout=60)

    def test_start_is_idempotent_and_hung_engine_is_restarted(self):
        self.assertIn('started', self.run_ctl('start').stdout)
        wd = (self.box / 'run' / 'watchdog.pid').read_text().strip()
        self.assertIn('already running', self.run_ctl('ensure').stdout)
        self.assertEqual((self.box / 'run' / 'watchdog.pid').read_text().strip(), wd)
        log = self.box / 'logs' / 'watchdog.log'
        for _ in range(40):
            if log.exists() and 'HEARTBEAT' in log.read_text() and log.read_text().count('engine started') >= 2:
                break
            time.sleep(0.5)
        text = log.read_text()
        self.assertIn('HEARTBEAT', text)
        self.assertGreaterEqual(text.count('engine started'), 2)
        self.assertIn('stopped', self.run_ctl('stop').stdout)
        self.assertFalse((self.box / 'run' / 'watchdog.pid').exists())

    def test_stale_pidfile_from_other_process_is_ignored(self):
        run = self.box / 'run'
        run.mkdir()
        (run / 'watchdog.pid').write_text(str(os.getpid()))  # alive, but not a watchdog
        out = self.run_ctl('ensure').stdout
        self.assertIn('started', out)
        self.assertNotEqual((run / 'watchdog.pid').read_text().strip(), str(os.getpid()))


class ExplicitTimeoutTest(unittest.TestCase):
    def test_every_http_call_has_a_timeout(self):
        for path in (ROOT / 'granmago' / 'server.py', ROOT / 'standalone' / 'fiscal_journal.py',
                     ROOT / 'standalone' / 'granmago_standalone.py'):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                        and node.func.attr in ('get', 'post', 'request', 'urlopen') \
                        and isinstance(node.func.value, ast.Name) \
                        and node.func.value.id in ('requests', 'session', 'urllib', 'request'):
                    names = {k.arg for k in node.keywords}
                    self.assertIn('timeout', names, '%s:%d HTTP call without timeout' % (path.name, node.lineno))
        src = (ROOT / 'granmago' / 'server.py').read_text()
        self.assertNotIn('urlopen(', src)


if __name__ == '__main__':
    unittest.main()
