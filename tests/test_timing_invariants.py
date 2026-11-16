"""Timing relationships between periods, timeouts, freshness limits and caches.

1.7.0 shipped QUOTE_MAX_AGE_SECONDS = 10 with MONITOR_SECONDS = 20: every open
position looked stale about half the time. These invariants fail as soon as one
constant is changed without the ones that depend on it.
"""
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "granmago"))
import blofin_trading as bt  # noqa: E402
import server  # noqa: E402

INDEX = Path(server.HTML_PATH).read_text(encoding="utf-8")
BOOK_AGE_MARGIN = 2.0        # measured Blofin books ts age: p100 0.6 s prod (16/11/2026)
BLOFIN_CANDLE_DELAY = 70.0   # measured publication delay of a new 4H candle (14/11/2026)
LATENCY = 0.3                # measured median ~0.18 s per public GET


def panel_poll_seconds():
    m = re.search(r"setInterval\(refresh,\s*(\d+)\)", INDEX)
    return int(m.group(1)) / 1000.0


class TimingInvariantsTest(unittest.TestCase):
    def test_display_freshness_survives_one_failed_monitor_cycle(self):
        # Worst cycle: wait + full HTTP budget. One failed refresh must not blank the panel.
        cycle = server.MONITOR_SECONDS + server.HTTP_BUDGET_SECONDS
        self.assertLessEqual(2 * cycle + BOOK_AGE_MARGIN, server.DISPLAY_MAX_AGE_SECONDS)
        self.assertGreaterEqual(server.DISPLAY_MAX_AGE_SECONDS, server.QUOTE_MAX_AGE_SECONDS)

    def test_normal_monitor_cycle_is_well_inside_execution_freshness(self):
        self.assertLess(server.MONITOR_SECONDS + LATENCY + BOOK_AGE_MARGIN, server.QUOTE_MAX_AGE_SECONDS)

    def test_one_http_attempt_cannot_age_a_quote_past_the_limit(self):
        # The book ts comes from the response that succeeded: its age at arrival is
        # bounded by that attempt's connect+read timeout, plus book age and clock skew.
        worst = server.HTTP_CONNECT_TIMEOUT + server.HTTP_READ_TIMEOUT + BOOK_AGE_MARGIN + server.CLOCK_SKEW_ALERT_SECONDS
        self.assertLessEqual(worst, server.QUOTE_MAX_AGE_SECONDS)
        self.assertLessEqual(server.HTTP_CONNECT_TIMEOUT + server.HTTP_READ_TIMEOUT, server.HTTP_BUDGET_SECONDS)
        # Retries with backoff fit in the budget at least twice.
        backoff = sum(0.25 * 2 ** a * 1.5 for a in range(server.HTTP_MAX_ATTEMPTS - 1))
        self.assertLess(backoff + 2 * LATENCY, server.HTTP_BUDGET_SECONDS)

    def test_panel_polls_faster_than_staleness(self):
        poll = panel_poll_seconds()
        self.assertLessEqual(poll * 2, server.DISPLAY_MAX_AGE_SECONDS)
        self.assertLessEqual(poll, server.MONITOR_SECONDS * 2)

    def test_blofin_account_display_covers_two_reconciles(self):
        trading_wake = 5
        worst_reconcile_io = 4 * sum(bt.QUERY_TIMEOUT)
        self.assertLessEqual(2 * server.RECONCILE_SECONDS + trading_wake + worst_reconcile_io,
                             server.ACCOUNT_DISPLAY_MAX_AGE_SECONDS)
        self.assertIn("ageSeconds", Path(server.__file__).read_text(encoding="utf-8"))
        # The panel must not age Blofin data with the browser clock.
        self.assertNotIn("Date.now()", INDEX)

    def test_private_rate_limiter(self):
        self.assertLessEqual(bt.TRADE_WINDOW_MAX, 30)  # docs: 30 requests / 10 s per user
        lim = bt.SlidingWindowLimiter()
        import inspect
        max_wait = inspect.signature(lim.acquire).parameters["max_wait"].default
        # A single caller always gets a slot: the oldest call leaves the window in time.
        self.assertGreaterEqual(max_wait, bt.TRADE_WINDOW_SECONDS)
        self.assertLess(bt.CLOCK_MAX_SKEW_SECONDS + max_wait, 60)  # Blofin rejects signatures > 60 s old

    def test_public_request_rate(self):
        per_min = 60 / server.HTTP_MIN_INTERVAL  # global spacing covers scan + monitor + quotes
        self.assertLessEqual(per_min, 500 * 0.6)  # docs: 500/min per IP

    def test_scan_duration_fits_signal_freshness(self):
        per_pair = 3 * (server.HTTP_MIN_INTERVAL + LATENCY) + 0.3
        normal_scan = server.MAX_CONFIGURED_PAIRS * per_pair + server.SCAN_RETRY_DELAY_SECONDS
        self.assertLessEqual(normal_scan, server.SCAN_MAX_AGE_SECONDS * 0.8)

    def test_candle_grace_covers_blofin_delay(self):
        self.assertGreaterEqual(server.CANDLE_CLOSE_GRACE_SECONDS, BLOFIN_CANDLE_DELAY * 2)
        self.assertLess(server.CANDLE_CLOSE_GRACE_SECONDS, server.BAR_SECONDS["4H"] / 10)

    def test_instrument_cache_and_degradation(self):
        self.assertGreaterEqual(server.INSTRUMENTS_TTL_SECONDS, 5 * 60)
        self.assertLessEqual(server.INSTRUMENTS_TTL_SECONDS, 15 * 60)
        # N failures at monitor pace happen within the time window.
        self.assertLessEqual(server.PAIR_DEGRADED_FAILURES * server.MONITOR_SECONDS, server.PAIR_DEGRADED_SECONDS)

    def test_thread_watchdog_limits(self):
        lim = server.THREAD_STALE_SECONDS
        self.assertGreater(lim["granmago-monitor"], server.MONITOR_SECONDS + server.HTTP_BUDGET_SECONDS * 2)
        # scan beats per pair: worst pair = 3 candle requests at full budget
        self.assertGreater(lim["granmago-scan"], 3 * server.HTTP_BUDGET_SECONDS + 90 + server.SCAN_RETRY_DELAY_SECONDS)
        self.assertGreater(lim["granmago-trading"], 5 + server.RECONCILE_SECONDS)

    def test_close_retry_backoff(self):
        self.assertGreaterEqual(server.CLOSE_RETRY_SECONDS, sum(bt.ORDER_TIMEOUT))


if __name__ == "__main__":
    unittest.main()
