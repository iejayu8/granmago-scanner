#!/usr/bin/env python3
"""Download the long Blofin cache used by the 1.6.0 study. No trading."""

import os
import sys
import time

os.environ["BACKTEST_CACHE"] = "/tmp/granmago-bt-cache-long"
os.environ.setdefault("DATA_DIR", "/tmp/granmago-bt-data")
os.environ.setdefault("LOG_LEVEL", "error")

TOOLS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TOOLS)
sys.path.insert(0, os.path.join(os.path.dirname(TOOLS), "granmago"))

import walkforward_sweep as ws  # noqa: E402


def main():
    start = ws._ms(ws.FETCH_START)
    end = ws._last_open_ms()
    print("fetch %s → %s" % (start, end), flush=True)
    t0 = time.time()
    market = ws.load_market(ws.EXTENDED, start, end)
    print("loaded %d in %.0fs" % (len(market), time.time() - t0), flush=True)
    for pair in ws.EXTENDED:
        data = market[pair]
        print("%s 15m %d 4H %d 1D %d 1W %d" % (
            pair, len(data.m1), len(data.h4), len(data.d1), len(data.w1),
        ), flush=True)


if __name__ == "__main__":
    main()
