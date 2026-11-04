#!/usr/bin/env python3
"""Download public Binance 15-minute klines for the 28-pair book.

History comes from data.binance.vision monthly and daily zip files (the
public kline dump). The in-progress day is not in those zips, so the tail
is read from data-api.binance.vision when the symbol is on spot. Symbols
Binance spot no longer lists through today (XMR, 1000BONK) use the USD-M
futures dump and a Blofin tail for the current day only.

Candles are cached under /tmp/granmago-bt-binance. Nothing here is a
strategy default.
"""

import io
import json
import time
import urllib.error
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

CACHE = Path("/tmp/granmago-bt-binance")
VISION = "https://data.binance.vision/data"
SPOT_API = "https://data-api.binance.vision/api/v3/klines"
BLOFIN = "https://openapi.blofin.com/api/v1/market/candles"
BAR_MS = 15 * 60 * 1000
START_MS = int(datetime(2023, 1, 12, tzinfo=timezone.utc).timestamp() * 1000)
UA = {"User-Agent": "granmago-backtest"}

# Live book. PEPE on the add-on is the spot-scale token, not 1000PEPE.
PAIRS = [
    "BTC-USDT", "ETH-USDT", "SOL-USDT", "XRP-USDT", "BNB-USDT", "ADA-USDT",
    "DOGE-USDT", "AVAX-USDT", "LINK-USDT", "DOT-USDT", "LTC-USDT", "BCH-USDT",
    "ZEC-USDT", "PEPE-USDT", "1000BONK-USDT", "NEAR-USDT", "XMR-USDT", "INJ-USDT",
    "SUI-USDT", "TAO-USDT", "IOST-USDT", "XLM-USDT", "TRX-USDT", "HBAR-USDT",
    "ENA-USDT", "WLD-USDT", "AAVE-USDT", "QNT-USDT",
]

# Spot history stops or the symbol is futures-only. Fixed from a listing check.
FUTURES_ONLY = {
    "1000BONK-USDT": "1000BONKUSDT",
    "XMR-USDT": "XMRUSDT",
}


def _get(url, timeout=60):
    req = urllib.request.Request(url, headers=UA)
    last = None
    for attempt in range(6):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code in (404, 451):
                return None
            last = exc
            time.sleep(0.8 * (attempt + 1))
        except Exception as exc:
            last = exc
            time.sleep(0.8 * (attempt + 1))
    raise RuntimeError("GET failed %s (%s)" % (url, last))


def _months(start_ms, end_ms):
    start = datetime.fromtimestamp(start_ms / 1000, timezone.utc)
    end = datetime.fromtimestamp(end_ms / 1000, timezone.utc)
    year, month = start.year, start.month
    out = []
    while (year, month) < (end.year, end.month):
        out.append("%04d-%02d" % (year, month))
        month += 1
        if month == 13:
            year, month = year + 1, 1
    return out


def _days_of_month(end_ms):
    end = datetime.fromtimestamp(end_ms / 1000, timezone.utc)
    return ["%04d-%02d-%02d" % (end.year, end.month, day) for day in range(1, end.day)]


def _zip_url(market, symbol, stamp, daily):
    kind = "daily" if daily else "monthly"
    return "%s/%s/%s/klines/%s/15m/%s-15m-%s.zip" % (VISION, market, kind, symbol, symbol, stamp)


def _parse_csv(raw):
    rows = []
    text = raw.decode("utf-8", errors="replace")
    for line in text.splitlines():
        if not line or line[0].isalpha():
            continue
        parts = line.split(",")
        if len(parts) < 5:
            continue
        try:
            ts = int(float(parts[0]))
            close = float(parts[4])
            if close <= 0:
                continue
            rows.append({
                "ts": ts,
                "o": float(parts[1]),
                "h": float(parts[2]),
                "l": float(parts[3]),
                "c": close,
            })
        except ValueError:
            continue
    return rows


def _zip_rows(url):
    blob = _get(url)
    if not blob:
        return []
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            name = zf.namelist()[0]
            return _parse_csv(zf.read(name))
    except zipfile.BadZipFile:
        return []


def _spot_tail(symbol, start_ms, end_ms):
    rows = []
    cursor = start_ms
    while cursor <= end_ms:
        url = "%s?symbol=%s&interval=15m&startTime=%d&endTime=%d&limit=1000" % (
            SPOT_API, symbol, cursor, end_ms,
        )
        blob = _get(url)
        if not blob:
            break
        try:
            page = json.loads(blob.decode("utf-8"))
        except json.JSONDecodeError:
            break
        if not isinstance(page, list) or not page:
            break
        for candle in page:
            try:
                close = float(candle[4])
                if close <= 0:
                    continue
                rows.append({
                    "ts": int(candle[0]),
                    "o": float(candle[1]),
                    "h": float(candle[2]),
                    "l": float(candle[3]),
                    "c": close,
                })
            except (TypeError, ValueError, IndexError):
                continue
        last = int(page[-1][0])
        nxt = last + BAR_MS
        if nxt <= cursor:
            break
        cursor = nxt
        if len(page) < 1000:
            break
        time.sleep(0.05)
    return rows


def _blofin_tail(inst_id, start_ms, end_ms):
    """15m pages, newest first, walking back with the `after` cursor."""
    rows = []
    after = end_ms + BAR_MS
    guard = 0
    while guard < 20:
        guard += 1
        url = "%s?instId=%s&bar=15m&limit=300&after=%d" % (BLOFIN, inst_id, after)
        blob = _get(url)
        if not blob:
            break
        try:
            body = json.loads(blob.decode("utf-8"))
        except json.JSONDecodeError:
            break
        data = body.get("data") if isinstance(body, dict) else None
        if not data:
            break
        oldest = None
        for candle in data:
            try:
                ts = int(float(candle[0]))
                close = float(candle[4])
            except (TypeError, ValueError, IndexError):
                continue
            if close <= 0:
                continue
            oldest = ts if oldest is None else min(oldest, ts)
            if start_ms <= ts <= end_ms:
                rows.append({"ts": ts, "o": float(candle[1]), "h": float(candle[2]), "l": float(candle[3]), "c": close})
        if oldest is None or oldest <= start_ms:
            break
        after = oldest
        time.sleep(0.05)
    return rows


def _merge(chunks):
    dedup = {}
    for row in chunks:
        dedup[row["ts"]] = row
    return [dedup[k] for k in sorted(dedup)]


def _closed(rows, end_ms):
    return [row for row in rows if row["ts"] + BAR_MS <= end_ms]


def fetch_pair(pair, end_ms):
    futures_symbol = FUTURES_ONLY.get(pair)
    if futures_symbol:
        market, symbol, tail = "futures/um", futures_symbol, "blofin"
    else:
        market, symbol, tail = "spot", pair.replace("-", ""), "spot"
    rows = []
    for stamp in _months(START_MS, end_ms):
        rows.extend(_zip_rows(_zip_url(market, symbol, stamp, daily=False)))
    for stamp in _days_of_month(end_ms):
        rows.extend(_zip_rows(_zip_url(market, symbol, stamp, daily=True)))
    merged = _closed(_merge(rows), end_ms)
    tail_rows = []
    if merged:
        tail_from = merged[-1]["ts"] + BAR_MS
    else:
        tail_from = START_MS
    if tail_from <= end_ms:
        if tail == "spot":
            tail_rows = _spot_tail(symbol, tail_from, end_ms)
        else:
            tail_rows = _blofin_tail(pair, tail_from, end_ms)
    all_rows = _closed(_merge(merged + tail_rows), end_ms)
    all_rows = [row for row in all_rows if row["ts"] >= START_MS]
    gap = None
    if merged and tail_rows:
        joined = _merge(tail_rows)
        if joined:
            prev = merged[-1]["c"]
            nxt = joined[0]["o"]
            if prev:
                gap = (nxt - prev) / prev
    return {
        "pair": pair,
        "symbol": symbol,
        "market": market,
        "tail": tail,
        "rows": all_rows,
        "join_gap": gap,
    }


def main():
    CACHE.mkdir(parents=True, exist_ok=True)
    now_ms = int(time.time() * 1000)
    end_ms = now_ms - (now_ms % BAR_MS)
    print("fetch 15m through %s" % datetime.fromtimestamp(end_ms / 1000, timezone.utc).isoformat(), flush=True)
    manifest = {"end_ms": end_ms, "pairs": {}}
    workers = 6
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(fetch_pair, pair, end_ms): pair for pair in PAIRS}
        for fut in as_completed(futs):
            pair = futs[fut]
            payload = fut.result()
            rows = payload["rows"]
            path = CACHE / ("%s-15m.json" % pair)
            path.write_text(json.dumps({"rows": rows}), encoding="utf-8")
            first = rows[0]["ts"] if rows else None
            last = rows[-1]["ts"] if rows else None
            manifest["pairs"][pair] = {
                "symbol": payload["symbol"],
                "market": payload["market"],
                "tail": payload["tail"],
                "bars": len(rows),
                "first": first,
                "last": last,
                "join_gap": payload["join_gap"],
            }
            print("  %s %s %s bars %d gap %s" % (
                pair, payload["market"], payload["symbol"], len(rows), payload["join_gap"],
            ), flush=True)
    (CACHE / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print("wrote", CACHE / "manifest.json", flush=True)
    missing = [pair for pair, info in manifest["pairs"].items() if info["bars"] < 1000]
    if missing:
        raise SystemExit("short history: %s" % ", ".join(missing))


if __name__ == "__main__":
    main()
