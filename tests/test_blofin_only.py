"""Live execution gateways: public HTTP is mocked, no HA or exchange orders."""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import requests

os.environ['DATA_DIR'] = tempfile.mkdtemp(prefix='granmago-blofin-test-')
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'granmago'))
import server

NOW = 1794132000.0
PAIR = 'XMR-USDT'


def response(body=None, status=200, headers=None):
    r = requests.Response()
    r.status_code = status
    r._content = json.dumps(body).encode()
    r._content_consumed = True
    r.headers.update(headers or {})
    return r


def instrument(pair):
    return dict(instId=pair, instType='SWAP', contractType='linear',
                quoteCurrency='USDT', settleCurrency='USDT', state='live', apiPermission='full')


def ticker(pair, price=613.76):
    return dict(instId=pair, last=str(price), bidPrice=str(price - .01),
                askPrice=str(price + .01), ts=str(int(NOW * 1000)))


def quote(price=613.76, pair=PAIR, age=0):
    return server.MarketQuote(pair, price, int((NOW - age) * 1000), time.monotonic())


def position(pair=PAIR, strategy='granmago'):
    return dict(pair=pair, side='long', entry=613.882752, sl=598.14, initialSl=598.14,
                tp=621.29, notional=398.93, initialRiskAmt=10.23, riskAmt=10.23,
                entryFee=.239358, price=613.882752, unrealized=0, strategy=strategy,
                tradeId=10, time='2026-11-07T13:01:46+01:00')


def scan_row(pair=PAIR):
    return dict(pair=pair, price=613.76, analyzedAtMs=int(NOW * 1000), scoreLong=100,
                scoreShort=0, bias='ALCISTA', recentLow=600.0, recentHigh=620.0,
                granmago=dict(pair=pair, side='long', price=613.0, sl=598.14,
                              tp=621.29, setup='A', closeMs=int(NOW * 1000), confirmMs=14400000))


class BlofinOnlyTest(unittest.TestCase):
    def setUp(self):
        self.state = server.STATE
        self.clock = server._clock
        self.state_file = server.STATE_FILE
        server.STATE = server.default_state()
        server.STATE.update(running=True, balance=1023.05, history=[], openPositions=[])
        server.STATE_FILE = Path(tempfile.mkdtemp()) / 'state.json'
        server._clock = lambda: NOW
        server._RATE_LIMIT_UNTIL = 0
        server._NEXT_REQUEST_AT = 0
        self.spacing = patch.object(server, 'HTTP_MIN_INTERVAL', 0)
        self.spacing.start()
        self.addCleanup(self.spacing.stop)
        self.urls = []
        self.price = 613.76
        self.edit_tick = lambda t: t
        self.edit_meta = lambda m: m
        self.http_status = 200
        self.fake = patch.object(requests.sessions.Session, 'request',
                                 new=lambda session, method, url, **kw: self.http(session, method, url, **kw))
        self.fake.start()
        self.addCleanup(self.fake.stop)

    def tearDown(self):
        server.STATE = self.state
        server.STATE_FILE = self.state_file
        server._clock = self.clock
        server._RATE_LIMIT_UNTIL = 0
        server._NEXT_REQUEST_AT = 0

    def http(self, session, method, url, **kw):
        self.assertEqual(method.upper(), 'GET')
        self.assertTrue(url.startswith(server.BLOFIN_MARKET), url)
        self.assertFalse(kw.get('allow_redirects', True))
        self.urls.append(url)
        pair = parse_qs(urlsplit(url).query)['instId'][0]
        if self.http_status != 200:
            return response(status=self.http_status)
        if '/instruments?' in url:
            body = self.edit_meta(instrument(pair))
        elif '/tickers?' in url:
            body = self.edit_tick(ticker(pair, self.price))
        else:
            self.fail('Unexpected endpoint ' + url)
        return response(dict(code='0', data=[body]))

    def arm(self):
        pos = position()
        server.STATE['openPositions'] = [pos]
        return pos

    def enter(self):
        server.STATE['lastScan'] = [scan_row()]
        server.maybe_open()

    def test_xmr_entry_monitor_pnl_and_manual_close_only_use_blofin(self):
        self.enter()
        pos = server.STATE['openPositions'][0]
        self.assertAlmostEqual(pos['entry'], 613.882752)
        server.monitor_position()
        self.assertEqual(len(server.STATE['history']), 0)
        self.assertAlmostEqual(pos['unrealized'], -.08, places=2)
        self.price = 615
        result = server.app.test_client().post('/api/close_position', json={'pair': PAIR})
        self.assertEqual(result.status_code, 200)
        hist = server.STATE['history'][0]
        self.assertAlmostEqual(hist['exit'], 615 * .9998)
        self.assertEqual(hist['exitMarket']['source'], 'Blofin')
        self.assertEqual(hist['exitMarket']['ts'], int(NOW * 1000))
        self.assertNotAlmostEqual(hist['exit'], 118.7)
        self.assertTrue(all('binance' not in u.lower() for u in self.urls))
        self.assertEqual(len([u for u in self.urls if '/tickers?' in u]), 3)

    def test_fresh_ticker_not_4h_candle_is_entry_price(self):
        server.STATE['lastScan'] = [scan_row()]
        self.price = 614.5
        server.maybe_open()
        self.assertAlmostEqual(server.STATE['openPositions'][0]['entry'], 614.5 * 1.0002)

    def test_bad_ticks_fail_closed_for_all_live_paths(self):
        bad = [dict(ts=str(int((NOW - 31) * 1000))), dict(ts=str(int((NOW + 3) * 1000))),
               dict(ts='nan'), dict(ts='1700000000'), dict(ts=None), dict(instId='BTC-USDT'),
               dict(last='nan'), dict(last='inf'), dict(last='-1'), dict(last='0'),
               dict(last=True), dict(bidPrice='0'), dict(askPrice='0'),
               dict(bidPrice='900', askPrice='800'), dict(last='118.7')]
        for changes in bad:
            for path in ('entry', 'monitor', 'manual', 'signal'):
                with self.subTest(changes=changes, path=path):
                    server.STATE['history'] = []
                    server.STATE['openPositions'] = []
                    server.STATE['usedSignals'] = {}
                    self.edit_tick = lambda t, c=changes: dict(t, **c)
                    if path == 'entry':
                        self.enter()
                        self.assertFalse(server.STATE['openPositions'])
                    else:
                        pos = self.arm()
                        before = server.STATE['balance']
                        if path == 'monitor':
                            server.monitor_position()
                        elif path == 'manual':
                            r = server.app.test_client().post('/api/close_position', json={'pair': PAIR})
                            self.assertEqual(r.status_code, 503)
                        else:
                            server._GRANMAGO_BARS[PAIR] = {int(NOW * 1000): {}}
                            with patch.object(server.granmago_strategy, 'exit_decision', return_value=('time', 118.7)):
                                server._close_granmago_signals({PAIR})
                        self.assertIs(server.STATE['openPositions'][0], pos)
                        self.assertEqual(server.STATE['balance'], before)
                        self.assertFalse(server.STATE['history'])

    def test_bad_instruments_and_unavailable_symbols_fail_closed(self):
        bad = [dict(instId='XMR-USDC'), dict(instType='SPOT'), dict(contractType='inverse'),
               dict(quoteCurrency='USDC'), dict(settleCurrency='BTC'), dict(state='suspend'),
               dict(apiPermission='unknown')]
        for changes in bad:
            with self.subTest(changes=changes):
                self.edit_meta = lambda m, c=changes: dict(m, **c)
                self.assertIsNone(server.fetch_quote(PAIR))
        for pair in ('XMR/USDT', 'XMR-USDC', 'XMRUSDT', 'XMR-USDT&evil=1'):
            self.assertIsNone(server.fetch_quote(pair))

    def test_reduce_only_blocks_entry_not_closing(self):
        self.edit_meta = lambda m: dict(m, apiPermission='reduce_only')
        self.enter()
        self.assertFalse(server.STATE['openPositions'])
        self.arm()
        r = server.app.test_client().post('/api/close_position', json={'pair': PAIR})
        self.assertEqual(r.status_code, 200)

    def test_no_quote_cache_or_candle_fallback_on_error(self):
        self.assertIsNotNone(server.fetch_quote(PAIR))
        self.http_status = 403  # non-transient: no retries
        self.assertIsNone(server.fetch_quote(PAIR))
        self.assertTrue(all('/candles?' not in u for u in self.urls))
        self.assertEqual(len(self.urls), 3)
        pos = self.arm()
        server.monitor_position()
        self.assertTrue(pos['priceStale'])
        self.assertFalse(server.STATE['history'])

    def test_scan_expiry_and_price_divergence_block_entries(self):
        for change in (dict(analyzedAtMs=int((NOW - 181) * 1000)), dict(price=118.7),
                       dict(analyzedAtMs=None)):
            with self.subTest(change=change):
                server.STATE['lastScan'] = [dict(scan_row(), **change)]
                server.maybe_open()
                self.assertFalse(server.STATE['openPositions'])

    def test_scanner_live_entry_also_uses_ticker(self):
        server.STATE['settings']['strategy'] = 'scanner'
        self.price = 614.5
        self.enter()
        self.assertAlmostEqual(server.STATE['openPositions'][0]['entry'], 614.5 * 1.0002)

    def test_all_stop_target_partial_and_trailing_fills_use_quote(self):
        for reason, price in [('SL', 590), ('TP', 630)]:
            for strategy in ('granmago', 'scanner'):
                with self.subTest(reason=reason, strategy=strategy):
                    server.STATE['history'] = []
                    server.STATE['openPositions'] = [position(strategy=strategy)]
                    self.price = price
                    server.monitor_position()
                    h = server.STATE['history'][0]
                    self.assertEqual(h['reason'], reason)
                    self.assertAlmostEqual(h['exit'], price * .9998)
        server.STATE['history'] = []
        p = position(strategy='scanner')
        p['trailingActive'] = True
        server.STATE['openPositions'] = [p]
        self.price = 590
        server.monitor_position()
        self.assertEqual(server.STATE['history'][0]['reason'], 'trailing')
        server.STATE['history'] = []
        p = position(strategy='scanner')
        p['tp'] = 650
        server.STATE['openPositions'] = [p]
        server.STATE['settings']['partialTpR'] = .5
        self.price = 625
        server.monitor_position()
        self.assertEqual(server.STATE['history'][0]['reason'], 'partial')
        self.assertEqual(server.STATE['history'][0]['exitMarket']['price'], 625)
        self.assertAlmostEqual(server.STATE['history'][0]['exit'], 625 * .9998)

    def test_signal_time_and_momentum_exit_use_current_quote_not_bar_price(self):
        for reason in ('time', 'momentum'):
            server.STATE['history'] = []
            self.arm()
            server._GRANMAGO_BARS[PAIR] = {int(NOW * 1000): {}}
            with patch.object(server.granmago_strategy, 'exit_decision', return_value=(reason, 118.7)):
                server._close_granmago_signals({PAIR})
            self.assertEqual(server.STATE['history'][0]['reason'], reason)
            self.assertAlmostEqual(server.STATE['history'][0]['exit'], self.price * .9998)

    def test_failed_scan_does_not_reuse_old_signal_bar_exit(self):
        self.arm()
        server._GRANMAGO_BARS[PAIR] = {1: {}}
        with patch.object(server.granmago_strategy, 'exit_decision', return_value=('time', 118.7)):
            server._close_granmago_signals(set())
        self.assertFalse(self.urls)
        self.assertFalse(server.STATE['history'])

    def test_quote_expiry_and_out_of_order_guard_before_fill(self):
        p = self.arm()
        before = copy.deepcopy(p)
        self.assertIsNone(server._apply_mark(p, 118.7, quote=quote(118.7, age=31)))
        self.assertEqual(p, before)
        p['lastMarket'] = dict(source='Blofin', ts=int(NOW * 1000))
        self.assertIsNone(server.close_position('manual', 118.7, PAIR, quote=quote(118.7, age=1)))
        self.assertFalse(server.STATE['history'])
        self.assertIsNone(server.close_position('manual', 118.7, PAIR, quote=quote(118.7, pair='BTC-USDT')))

    def test_no_replacement_position_closed_after_network_race(self):
        original = self.arm()
        replacement = dict(original, tradeId=11)
        def changed(pair):
            with server.LOCK:
                server.STATE['openPositions'] = [replacement]
            return quote(590)
        with patch.object(server, 'fetch_quote', side_effect=changed):
            server.monitor_position()
            self.assertFalse(server.STATE['history'])
            server.STATE['openPositions'] = [original]
            r = server.app.test_client().post('/api/close_position', json={'pair': PAIR})
            self.assertEqual(r.status_code, 409)
        self.assertIs(server.STATE['openPositions'][0], replacement)

    def test_scan_changed_or_stopped_during_fetch_cannot_enter(self):
        for mode in ('replace_scan', 'stop'):
            server.STATE['running'] = True
            server.STATE['lastScan'] = [scan_row()]
            def changed(pair):
                with server.LOCK:
                    if mode == 'replace_scan':
                        server.STATE['lastScan'] = [scan_row()]
                    else:
                        server.STATE['running'] = False
                return quote()
            with patch.object(server, 'fetch_quote', side_effect=changed):
                server.maybe_open()
            self.assertFalse(server.STATE['openPositions'])

    def test_parallel_entries_and_close_attempts_do_not_duplicate(self):
        server.STATE['lastScan'] = [scan_row()]
        for phase in ('entry', 'close'):
            barrier = threading.Barrier(2)
            errors = []
            def synchronized(pair):
                barrier.wait(timeout=2)
                return quote(613.76 if phase == 'entry' else 630)
            def run():
                try:
                    if phase == 'entry':
                        server.maybe_open()
                    else:
                        server._monitor_one(PAIR)
                except BaseException as exc:
                    errors.append(exc)
            with patch.object(server, 'fetch_quote', side_effect=synchronized):
                threads = [threading.Thread(target=run) for _ in range(2)]
                for t in threads: t.start()
                for t in threads: t.join(3)
                self.assertFalse(any(t.is_alive() for t in threads))
            self.assertFalse(errors)
            if phase == 'entry':
                self.assertEqual(len(server.STATE['openPositions']), 1)
                self.assertEqual(server.STATE['balance'], round(1023.05 - server.STATE['openPositions'][0]['entryFee'], 2))
            else:
                self.assertEqual(len(server.STATE['history']), 1)

    def test_blocked_pair_does_not_block_other_position(self):
        server.STATE['openPositions'] = [position(), position('BTC-USDT')]
        slow_started = threading.Event()
        other_fetched = threading.Event()
        def fetch(pair):
            if pair == PAIR:
                slow_started.set()
                self.assertTrue(other_fetched.wait(2), 'other position blocked by slow retry')
                return None
            self.assertTrue(slow_started.wait(2))
            other_fetched.set()
            return quote(630, pair)
        with patch.object(server, 'fetch_quote', side_effect=fetch):
            server.monitor_position()
        self.assertEqual(server.STATE['history'][0]['pair'], 'BTC-USDT')
        self.assertEqual(server.STATE['openPositions'][0]['pair'], PAIR)

    def test_public_state_hides_stale_pnl_without_rewriting_ledger(self):
        p = self.arm()
        hist = dict(tradeId=1, pnl=-322.29, pair=PAIR, exit=118.67626)
        server.STATE['history'] = [hist]
        before = copy.deepcopy(server.STATE)
        public = server.public_state()
        self.assertIsNone(public['openPositions'][0]['unrealized'])
        self.assertIsNone(public['openPositions'][0]['price'])
        self.assertEqual(server.STATE, before)
        self.assertEqual(public['history'], before['history'])

    def test_candles_only_blofin_strict_ohlc_and_window_freshness(self):
        good = [str(int((NOW - 100) * 1000)), '610', '620', '600', '613.76']
        for changes in (None, ['nan'], ['0']):
            row = list(good)
            if changes is not None: row[4] = changes[0]
            with patch.object(server, 'fetch_json', return_value=dict(code='0', data=[row])) as get:
                result = server.fetch_candles(PAIR, '4H', 300)
            self.assertEqual(bool(result), changes is None)
            self.assertIn('openapi.blofin.com', get.call_args.args[0])
        for row in ([str(int((NOW - 20000) * 1000)), *good[1:]],
                    [str(int((NOW + 100) * 1000)), *good[1:]],
                    [good[0], '610', '605', '600', '613.76'], good[:3]):
            with patch.object(server, 'fetch_json', return_value=dict(code='0', data=[row])):
                self.assertEqual(server.fetch_candles(PAIR, '4H'), [])
        with patch.object(server, 'fetch_json', return_value=None) as get:
            self.assertEqual(server.fetch_candles(PAIR, '4H'), [])
            self.assertEqual(get.call_count, 1)

    def test_retry_transient_recovers_once_no_duplicate_entry_or_close(self):
        original_http = self.http
        failures = [requests.Timeout(), 503]
        def flaky(session, method, url, **kw):
            if failures:
                failure = failures.pop(0)
                if isinstance(failure, Exception): raise failure
                return response(status=failure)
            return original_http(session, method, url, **kw)
        with patch.object(requests.sessions.Session, 'request', new=flaky), patch.object(server.time, 'sleep'):
            self.enter()
            self.assertEqual(len(server.STATE['openPositions']), 1)
            self.price = 590
            failures.extend([502, requests.ConnectionError()])
            server.monitor_position()
        self.assertEqual(len(server.STATE['history']), 1)
        self.assertFalse(server.STATE['openPositions'])

    def test_retry_persistent_max_three_gets_and_no_fill(self):
        with patch.object(requests.sessions.Session, 'request', side_effect=requests.Timeout()) as request_mock, patch.object(server.time, 'sleep') as sleep:
            self.enter()
        self.assertEqual(request_mock.call_count, 3)
        self.assertEqual([x.args[0] for x in sleep.call_args_list], [.25, .5])
        self.assertFalse(server.STATE['openPositions'])
        self.assertFalse(server.STATE['history'])

    def test_retry_after_long_is_respected_without_long_sleep(self):
        with patch.object(requests.sessions.Session, 'request', return_value=response(status=429, headers={'Retry-After': '120'})) as get, patch.object(server.time, 'sleep') as sleep:
            self.assertIsNone(server.fetch_quote(PAIR))
            self.assertIsNone(server.fetch_quote('BTC-USDT'))
        self.assertEqual(get.call_count, 1)
        sleep.assert_not_called()
        self.assertGreater(server._RATE_LIMIT_UNTIL, time.monotonic() + 100)

    def test_retry_after_short_and_http_date_and_deadline(self):
        self.assertEqual(server._retry_after_seconds('Sun, 08 Nov 2026 15:20:00 GMT'),
                         max(0, 1794151200 - NOW))
        self.assertIsNone(server._retry_after_seconds('invalid'))
        clock = [100.0]
        server._NEXT_REQUEST_AT = 0
        replies = [response(status=429, headers={'Retry-After': '2'}), response(dict(code='0'))]
        def pause(n): clock[0] += n
        with patch.object(server.time, 'monotonic', side_effect=lambda: clock[0]), patch.object(server.time, 'sleep', side_effect=pause) as sleep, patch.object(requests.sessions.Session, 'request', side_effect=replies) as get:
            self.assertEqual(server.fetch_json(server.BLOFIN_MARKET + 'tickers?instId=' + PAIR), dict(code='0'))
            self.assertEqual(get.call_count, 2)
            self.assertEqual(sleep.call_args.args[0], 2)
            self.assertIsNone(server.fetch_json(server.BLOFIN_MARKET + 'tickers?instId=' + PAIR, deadline=99))
            self.assertEqual(get.call_count, 2)

    def test_trickling_or_late_body_exhausts_budget_without_fill(self):
        clock = [100.0]
        server._NEXT_REQUEST_AT = 0
        reply = response(dict(code='0'))
        def trickle(chunk_size):
            for byte in b'{"code":"0"}':
                clock[0] += 3
                yield bytes([byte])
        reply.iter_content = trickle
        with patch.object(server.time, 'monotonic', side_effect=lambda: clock[0]), patch.object(requests.sessions.Session, 'request', return_value=reply):
            self.assertIsNone(server.fetch_json(server.BLOFIN_MARKET + 'tickers?instId=' + PAIR))
        self.assertLessEqual(clock[0], 111)

    def test_global_rate_spacing_is_below_documented_limit(self):
        clock = [100.0]
        server._NEXT_REQUEST_AT = 0
        with patch.object(server, 'HTTP_MIN_INTERVAL', .25), patch.object(server.time, 'monotonic', side_effect=lambda: clock[0]), patch.object(server.time, 'sleep', side_effect=lambda n: clock.__setitem__(0, clock[0] + n)) as sleep:
            self.assertTrue(server._wait_market_turn(108))
            self.assertTrue(server._wait_market_turn(108))
            self.assertTrue(server._wait_market_turn(108))
        self.assertAlmostEqual(clock[0], 100.50)
        self.assertEqual(sleep.call_count, 2)

    def test_add_uses_quote_not_historical_add_level(self):
        p = self.arm()
        p.update(maxAdds=1, addsDone=0, addPrice=608, addBudget=5)
        old_n, old_e = p['notional'], p['entry']
        self.price = 607
        server.monitor_position()
        self.assertEqual(p['addsDone'], 1)
        leg_fill = self.price * 1.0002
        leg_n = round(5 / ((leg_fill - p['sl']) / leg_fill), 2)
        self.assertAlmostEqual(p['entry'], (old_n * old_e + leg_n * leg_fill) / (old_n + leg_n))

    def test_api_ignores_caller_price_and_transient_failure_does_not_duplicate_close(self):
        self.arm()
        r = server.app.test_client().post('/api/close_position', json={'pair': PAIR, 'price': 118.7, 'source': 'Binance'})
        self.assertEqual(r.status_code, 200)
        r = server.app.test_client().post('/api/close_position', json={'pair': PAIR})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(len(server.STATE['history']), 1)
        self.assertAlmostEqual(server.STATE['history'][0]['exit'], 613.76 * .9998)

    def test_bad_http_json_semantics_and_no_redirect_external_source(self):
        for reply in (response(status=403), response(status=302, headers={'Location': 'https://example.com'}), response(dict(code='500', data=[])), response(dict(code='0', data=['bad']))):
            with patch.object(requests.sessions.Session, 'request', return_value=reply):
                self.assertIsNone(server.fetch_quote(PAIR))
        with self.assertRaises(ValueError):
            server.fetch_json('https://api.binance.com/api/v3/ticker/price?symbol=XMRUSDT')


if __name__ == '__main__':
    unittest.main()
