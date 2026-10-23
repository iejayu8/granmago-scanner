#!/usr/bin/env python3
"""Cache Blofin 8h funding history for the 1.6.0 study."""

import json
import os
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

os.environ.setdefault("BACKTEST_CACHE", "/tmp/granmago-bt-cache-long")
os.environ.setdefault("DATA_DIR", "/tmp/granmago-bt-data")
os.environ.setdefault("LOG_LEVEL", "error")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "granmago"))

import walkforward_sweep as ws  # noqa: E402

OUT = Path("/tmp/granmago-funding.json")
URL = "https://openapi.blofin.com/api/v1/market/funding-rate-history"
START = 1673481600000  # same candle fetch start


def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "granmago-backtest"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    if str(body.get("code")) != "0":
        raise RuntimeError(body)
    return body.get("data") or []


def one(pair):
    found = {}
    cursor = None
    pages = 0
    while pages < 80:
        params = {"instId": pair, "limit": "100"}
        if cursor is not None:
            params["after"] = str(cursor)
        data = _get(URL + "?" + urllib.parse.urlencode(params))
        pages += 1
        if not data:
            break
        oldest = None
        for row in data:
            ts = int(row["fundingTime"])
            found[ts] = float(row["fundingRate"])
            oldest = ts if oldest is None else min(oldest, ts)
        if oldest is None or oldest <= START or len(data) < 100:
            break
        cursor = oldest
        time.sleep(0.04)
    series = sorted(found.items())
    print("%s funding %d %s → %s" % (
        pair, len(series), series[0][0] if series else "-", series[-1][0] if series else "-",
    ), flush=True)
    return pair, series


def main():
    if OUT.exists():
        print("cache exists", OUT, flush=True)
        return
    out = {}
    with ThreadPoolExecutor(max_workers=2) as pool:
        futs = [pool.submit(one, pair) for pair in ws.EXTENDED]
        for fut in as_completed(futs):
            pair, series = fut.result()
            out[pair] = series
    OUT.write_text(json.dumps(out), encoding="utf-8")
    print("wrote", OUT, flush=True)


if __name__ == "__main__":
    main()
