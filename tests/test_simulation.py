"""Hours of simulated operation on a fake clock (1.7.2 robustness audit).

Drives the real scan/monitor/trading/panel code paths on the add-on schedule
(monitor 5 s, scan 90 s, trading 5 s, panel poll 4 s) against a fake public
Blofin API with injected faults and FakeBlofin for the demo account:
latency, timeouts, 5xx, 429, empty books, slow books, IOST missing on demo,
crash restarts, paper -> demo -> paper -> demo, bot stopped with positions
and kill switch. Invariants are checked continuously.
"""
import json
import random
import sys
import tempfile
import threading
import time as real_time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_blofin_only as base  # noqa: E402
import test_demo_server as tds  # noqa: E402
import fake_blofin  # noqa: E402
from fake_blofin import FakeBlofin  # noqa: E402

server = base.server
bt = server.blofin_trading
START = 1794132000.0
PAIRS = ["XMR-USDT", "SOL-USDT", "LINK-USDT", "DOGE-USDT", "IOST-USDT", "XLM-USDT"]
PRICES = {"XMR-USDT": 613.76, "SOL-USDT": 151.3, "LINK-USDT": 14.62, "DOGE-USDT": 0.1712,
          "IOST-USDT": 0.00412, "XLM-USDT": 0.2731}
HOUR = 3600


class SimClock:
    """time.time/monotonic/sleep on one shared simulated clock."""

    def __init__(self, t):
        self.t = float(t)
        self._lock = threading.Lock()

    def time(self):
        return self.t

    def monotonic(self):
        return self.t - START + 1000.0

    def sleep(self, s):
        self.advance(max(0.0, float(s)))

    def advance(self, s):
        with self._lock:
            self.t += s

    def gmtime(self, secs=None):
        return real_time.gmtime(self.t if secs is None else secs)

    def localtime(self, secs=None):
        return real_time.localtime(self.t if secs is None else secs)

    def __getattr__(self, name):
        return getattr(real_time, name)


def real_save_state():
    payload = {k: v for k, v in server.STATE.items() if k != "busy"}
    server.STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = server.STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    tmp.replace(server.STATE_FILE)


class Market:
    """Fake public Blofin (prod + demo instruments) with fault injection."""

    def __init__(self, sim, rng):
        self.sim, self.rng = sim, rng
        self.prices = dict(PRICES)
        self.faults = {}         # kind -> probability
        self.empty_book = set()  # pairs with an empty book
        self.calls = 0
        self.foreign = []
        self.lock = threading.Lock()

    def walk(self, dt):
        for p in self.prices:
            self.prices[p] *= 1 + self.rng.gauss(0, 0.00035 * dt ** 0.5)

    def meta(self, pair):
        cv = "0.01" if pair == "XMR-USDT" else "1"
        tick = "0.01" if self.prices[pair] > 10 else "0.00001"
        return dict(base.instrument(pair), contractValue=cv, lotSize="1", minSize="1",
                    tickSize=tick, maxMarketSize="100000000")

    def __call__(self, session, method, url, **kw):
        if not (url.startswith(server.BLOFIN_MARKET) or url.startswith(server.DEMO_MARKET)):
            self.foreign.append(url)  # e.g. ECB FX for the fiscal journal
            raise requests.exceptions.ConnectionError("sim: sin red externa")
        assert method.upper() == "GET" and not kw.get("allow_redirects", True)
        assert url.startswith(server.BLOFIN_MARKET) or url.startswith(server.DEMO_MARKET), url
        with self.lock:
            self.calls += 1
            roll = self.rng.random()
        self.sim.advance(0.15 + 0.1 * roll)  # latency
        acc = 0.0
        for kind, prob in self.faults.items():
            acc += prob
            if roll < acc:
                if kind == "timeout":
                    self.sim.advance(server.HTTP_READ_TIMEOUT)
                    raise requests.exceptions.ReadTimeout("sim timeout")
                if kind == "5xx":
                    return base.response(status=503)
                if kind == "429":
                    return base.response(status=429, headers={"Retry-After": "2"})
                if kind == "slow":
                    self.sim.advance(2.5)
                break
        if url.startswith(server.DEMO_MARKET):
            if "/instruments" in url:
                return base.response(dict(code="0", data=[self.meta(p) for p in PAIRS if p != "IOST-USDT"]))
        q = parse_qs(urlsplit(url).query)
        pair = q.get("instId", [None])[0]
        if "/instruments" in url:
            return base.response(dict(code="0", data=[self.meta(pair)]))
        if "/books" in url:
            px = self.prices[pair]
            ts = int((self.sim.t - self.rng.uniform(0.05, 0.6)) * 1000)
            if pair in self.empty_book:
                return base.response(dict(code="0", data=[dict(asks=[], bids=[], ts=str(ts))]))
            half = px * 0.0001
            return base.response(dict(code="0", data=[dict(asks=[[repr(px + half), "50"]],
                                                           bids=[[repr(px - half), "50"]], ts=str(ts))]))
        if "/candles" in url:
            return base.response(dict(code="0", data=[]))
        raise AssertionError("unexpected endpoint " + url)


class SimulationTest(unittest.TestCase):
    HOURS_PAPER, HOURS_DEMO, HOURS_PAPER2, HOURS_DEMO2 = 1.5, 2.5, 1.0, 0.75

    def setUp(self):
        self.sim = SimClock(START)
        self.rng = random.Random(1702)
        self.market = Market(self.sim, self.rng)
        self.saved = (server.STATE, server.STATE_FILE, server._clock)
        self.tmp = tempfile.TemporaryDirectory()
        server.STATE_FILE = Path(self.tmp.name) / "state.json"
        self.exceptions = []
        patches = [
            patch.object(server, "DATA_DIR", Path(self.tmp.name)),
            patch.object(server, "time", self.sim),
            patch.object(bt, "time", self.sim),
            patch.object(fake_blofin, "time", self.sim),
            patch.object(server, "save_state", real_save_state),
            patch.object(server, "analyze_pair", self.analyze),
            patch.object(server, "kick", lambda: None),
            patch.object(requests.sessions.Session, "request",
                         new=lambda session, method, url, **kw: self.market(session, method, url, **kw)),
            patch.object(server.log, "exception", self.log_exception),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        server._clock = self.sim.time
        self.fake = FakeBlofin(prices=self.market.prices)
        self.fake.prices = self.market.prices  # same dict: exchange marks follow the market
        self.client = server.app.test_client()
        self.ingress = {"REMOTE_ADDR": server.INGRESS_IP}
        self.posted_cids = []
        self.stats = {"polls": 0, "pos_polls": 0, "stale_polls": 0, "stale_polls_normal": 0,
                      "pos_polls_normal": 0, "entries": 0, "sl_hits": 0, "tp_hits": 0, "restarts": 0}
        self.normal = True

    def tearDown(self):
        server.init_trading({})
        server.STATE, server.STATE_FILE, server._clock = self.saved
        server._RATE_LIMIT_UNTIL = 0
        server._NEXT_REQUEST_AT = 0
        for d in (server._INSTRUMENTS, server._DEMO_INSTRUMENTS, server._PAIR_HEALTH, server._CLOSE_TRIES):
            d.clear()
        server._DEMO_UNIVERSE.update(ids=None, at=None, missing=[])
        self.tmp.cleanup()

    def log_exception(self, msg, *a, **kw):
        import traceback
        self.exceptions.append((msg, traceback.format_exc()))

    # ------------------------------------------------------------- signals
    def analyze(self, pair, threshold=None):
        if server.fetch_json(server.BLOFIN_MARKET + "candles?instId=%s&bar=4H&limit=300" % pair) is None:
            raise RuntimeError("sin velas de %s" % pair)
        bar = int(self.sim.t // 1800) * 1800  # a "bar" every 30 min keeps the book busy
        r = random.Random("%s-%d" % (pair, bar))
        px = self.market.prices[pair]
        row = dict(pair=pair, price=px, analyzedAtMs=int(self.sim.t * 1000), scoreLong=50, scoreShort=50,
                   bias="-", stoch=50.0, nearEMA=False, granmago=None)
        if r.random() < 0.5:
            side = "long" if r.random() < 0.5 else "short"
            d = 1 if side == "long" else -1
            row["granmago"] = dict(pair=pair, side=side, price=px, sl=px * (1 - d * 0.012),
                                   tp=px * (1 + d * 0.02), setup="A", closeMs=bar * 1000, confirmMs=14400000)
        return row

    # ------------------------------------------------------------ lifecycle
    def boot(self, env, crash=True):
        """Process start. crash=True: no save before, state comes from disk."""
        if not crash:
            real_save_state()
        # A new process: no trader, state only from disk (never init_trading here,
        # that would switch the in-memory ledger and save it).
        server.TRADER = server.TRADING_CONFIG = server.JOURNAL = server.TRADING_ERROR = None
        server.STATE = server.load_state()
        for d in (server._INSTRUMENTS, server._DEMO_INSTRUMENTS, server._PAIR_HEALTH, server._CLOSE_TRIES):
            d.clear()
        server._DEMO_UNIVERSE.update(ids=None, at=None, missing=[])
        server._RATE_LIMIT_UNTIL = 0
        server._NEXT_REQUEST_AT = 0
        mode = server.init_trading(env, transport=self.fake, sleep=self.sim.sleep)
        if server.TRADER is not None:
            t = server.TRADER
            t._clock = t.client._clock = self.sim.time
            t.client.limiter = bt.SlidingWindowLimiter(clock=self.sim.monotonic, sleep=self.sim.sleep)
            server.start_trading()
        self.stats["restarts"] += 1
        return mode

    def configure(self):
        s = server.STATE["settings"]
        s.update(pairs=",".join(PAIRS), maxPositions=3, regimeFilter="off", scanSeconds=90)
        server.STATE["running"] = True
        real_save_state()

    # ------------------------------------------------------------ exchange
    def exchange_events(self):
        f = self.fake
        for inst, pos in f.positions.items():
            if pos["positions"] == 0:
                for t in f.tpsl:
                    if t["instId"] == inst and t["state"] == "live":
                        t["state"] = "canceled"  # Blofin drops the position TPSL on close
                continue
            live = [t for t in f.tpsl if t["instId"] == inst and t["state"] == "live"]
            if not live:
                continue
            t = live[0]
            px = self.market.prices[inst]
            sl, tp = float(t["slTriggerPrice"]), float(t["tpTriggerPrice"] or 0)
            long_ = pos["positions"] > 0
            if (px <= sl) if long_ else (px >= sl):
                f.trigger_stop(inst)
                self.market.prices[inst] = px  # keep the path
                self.stats["sl_hits"] += 1
            elif tp and ((px >= tp) if long_ else (px <= tp)):
                f._fill(inst, "sell" if long_ else "buy", abs(pos["positions"]), "tp" + t["tpslId"], closing=True)
                pos["positions"] = 0
                t["state"] = "effective"
                self.stats["tp_hits"] += 1

    def check_exchange_invariants(self):
        f = self.fake
        for inst, pos in f.positions.items():
            if pos["positions"] != 0:
                live = [t for t in f.tpsl if t["instId"] == inst and t["state"] == "live" and t.get("slTriggerPrice")]
                self.assertEqual(len(live), 1, "%s en Blofin sin SL único: %r" % (inst, live))
        posts = [c for c in f.calls if c[0] == "POST" and isinstance(c[3], dict)]
        cids = [c[3].get("clientOrderId") for c in posts if c[1] in (
            "/api/v1/trade/order", "/api/v1/trade/order-tpsl", "/api/v1/trade/close-position")]
        cids = [c for c in cids if c]
        self.assertEqual(len(cids), len(set(cids)), "clientOrderId reenviado")
        self.assertNotIn("IOST-USDT", [c[3].get("instId") for c in posts])

    def check_panel(self):
        snap = json.loads(self.client.get("/api/state").get_data(as_text=True))
        self.stats["polls"] += 1
        d = snap["display"]
        pos = snap["openPositions"]
        if snap["tradingMode"] == "demo":
            acc = snap.get("exchangeAccount")
            if d["equity"] is not None:
                self.assertEqual(d["equity"], round(acc["equity"], 2))  # USDT, not totalEquity (USD)
            if acc and acc.get("ageSeconds") is not None:
                self.assertGreaterEqual(acc["ageSeconds"], -1)
        elif d["equity"] is not None:
            self.assertAlmostEqual(d["equity"], round(snap["balance"] + sum(p["unrealized"] for p in pos), 2), places=1)
        if pos:
            self.stats["pos_polls"] += 1
            stale = d["equity"] is None or d["unrealized"] is None
            self.stats["stale_polls"] += stale
            if self.normal:
                self.stats["pos_polls_normal"] += 1
                self.stats["stale_polls_normal"] += stale
        return snap

    # ------------------------------------------------------------- the run
    def run_for(self, seconds, demo, events=None):
        end = self.sim.t + seconds
        nxt = {"monitor": self.sim.t, "scan": self.sim.t, "trading": self.sim.t, "panel": self.sim.t}
        while self.sim.t < end:
            now = self.sim.t
            if events:
                for at in sorted(list(events)):
                    if now >= at:
                        events.pop(at)()
            self.market.walk(1.0)
            self.exchange_events()
            with server.LOCK:
                running = server.STATE["running"]
                has_pos = bool(server.STATE["openPositions"])
            if now >= nxt["monitor"]:
                acts = server.next_actions(running, has_pos)
                if acts["monitor"]:
                    server.monitor_position()
                elif has_pos:
                    server.monitor_position(display_only=True)
                nxt["monitor"] = self.sim.t + (server.MONITOR_SECONDS if has_pos else 1)
            if now >= nxt["scan"]:
                if server.next_actions(running, has_pos)["scan"]:
                    n0 = len(server.STATE["openPositions"])
                    server.scan_all()
                    server.maybe_open()
                    self.stats["entries"] += max(0, len(server.STATE["openPositions"]) - n0)
                nxt["scan"] = self.sim.t + 90
            if demo and now >= nxt["trading"]:
                server.trading_cycle()
                self.check_exchange_invariants()
                nxt["trading"] = self.sim.t + 5
            if now >= nxt["panel"]:
                self.check_panel()
                nxt["panel"] = self.sim.t + 4
            if self.sim.t < now + 1:
                self.sim.advance(now + 1 - self.sim.t)
        self.assertEqual(self.exceptions, [], self.exceptions[:1])

    def faults(self, **probs):
        self.market.faults = probs
        self.normal = not probs and not self.market.empty_book

    def test_hours_of_operation_with_faults_restarts_and_mode_switches(self):
        sim, m = self.sim, self.market
        # ---------------- phase 1: paper, 1.5 h
        self.assertEqual(self.boot({}), "paper")
        self.configure()
        t0 = sim.t
        self.run_for(1800, demo=False)
        self.faults(**{"5xx": 0.05, "timeout": 0.02, "slow": 0.05})
        self.run_for(900, demo=False)
        self.faults()
        before = json.loads(json.dumps({k: server.STATE[k] for k in ("openPositions", "history", "balance")}))
        self.boot({})  # crash restart
        after = {k: server.STATE[k] for k in ("openPositions", "history", "balance")}
        self.assertEqual(json.loads(json.dumps(after)), before, "estado perdido en reinicio paper")
        self.run_for(self.HOURS_PAPER * HOUR - (sim.t - t0), demo=False)
        paper_entries = self.stats["entries"]
        self.assertGreater(paper_entries, 0, "la simulación no abrió nada en paper")
        # paper positions are not taken to demo: close them as the user would
        for p in list(server.STATE["openPositions"]):
            r = self.client.post("/api/close_position", json={"pair": p["pair"]}, environ_base=self.ingress)
            self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        paper_book = json.loads(json.dumps({k: server.STATE[k] for k in ("balance", "history", "initialBalance")}))

        # ---------------- phase 2: demo, 2.5 h
        self.assertEqual(self.boot(tds.DEMO_ENV), "demo")
        self.assertNotIn("IOST-USDT", server.active_pairs())
        snapshot = json.loads(json.dumps(server.STATE.get("paperSnapshot")))
        self.assertEqual(snapshot["balance"], paper_book["balance"])
        t1 = sim.t
        ev = {
            t1 + 1200: lambda: self.faults(**{"429": 0.03, "5xx": 0.03}),
            t1 + 1800: lambda: self.faults(),
            t1 + 2400: lambda: m.empty_book.add("SOL-USDT"),
            t1 + 2700: lambda: (m.empty_book.discard("SOL-USDT"), self.faults()),
        }
        self.run_for(3000, demo=True, events=ev)
        self.assertGreater(len(self.fake.orders), 0, "la simulación no operó en demo")
        # crash restart with positions on Blofin
        local = sorted(p["pair"] for p in server.STATE["openPositions"])
        self.boot(tds.DEMO_ENV)
        self.assertEqual(sorted(p["pair"] for p in server.STATE["openPositions"]), local)
        self.assertTrue(server.TRADER.safe, server.TRADER.status())
        # bot stopped with positions for 20 min: Blofin SL/TP keep working, panel stays fresh
        self.ensure_positions()
        self.client.post("/api/stop", environ_base=self.ingress)
        self.assertFalse(server.STATE["running"])
        n_orders = len(self.fake.orders)
        polls = self.stats["pos_polls"]
        self.run_for(1200, demo=True)
        self.assertEqual(len(self.fake.orders), n_orders, "orden con el bot parado")
        self.assertGreater(self.stats["pos_polls"], polls, "parado sin posiciones: la fase no prueba nada")
        self.client.post("/api/start", environ_base=self.ingress)
        self.run_for(1200, demo=True)
        # kill switch with close-all
        self.ensure_positions()
        r = self.client.post("/api/kill_switch", json={"enabled": True, "closeAll": True}, environ_base=self.ingress)
        self.assertEqual(r.status_code, 200)
        self.run_for(120, demo=True)
        self.assertEqual([p for p in self.fake.positions.values() if p["positions"]], [])
        self.assertEqual(server.STATE["openPositions"], [])
        self.boot(tds.DEMO_ENV)  # kill switch survives a crash
        self.assertTrue(server.STATE["killSwitch"])
        n = len(self.fake.orders)
        self.run_for(600, demo=True)
        self.assertEqual(len(self.fake.orders), n, "entrada con kill switch activo")
        self.client.post("/api/kill_switch", json={"enabled": False}, environ_base=self.ingress)
        self.run_for(self.HOURS_DEMO * HOUR - (sim.t - t1), demo=True)
        self.assertEqual(server.STATE.get("paperSnapshot"), snapshot, "copia paper alterada en demo")
        demo_open = sorted(p["pair"] for p in server.STATE["openPositions"])
        demo_hist = len(server.STATE["history"])

        # ---------------- phase 3: back to paper with demo positions still on Blofin
        self.assertEqual(self.boot({}), "paper")
        self.assertEqual(server.STATE["balance"], paper_book["balance"])
        self.assertEqual(server.STATE["history"], paper_book["history"])
        self.assertFalse(any(p.get("exchange") for p in server.STATE["openPositions"]))
        self.configure()
        self.run_for(self.HOURS_PAPER2 * HOUR, demo=False)
        for p in list(server.STATE["openPositions"]):
            self.client.post("/api/close_position", json={"pair": p["pair"]}, environ_base=self.ingress)

        # ---------------- phase 4: demo again: its own book comes back and reconciles
        self.boot(tds.DEMO_ENV)
        self.assertGreaterEqual(len(server.STATE["history"]), demo_hist)
        on_exchange = sorted(i for i, p in self.fake.positions.items() if p["positions"])
        self.assertEqual(sorted(p["pair"] for p in server.STATE["openPositions"]), on_exchange)
        self.assertTrue(set(on_exchange) <= set(demo_open) | set(on_exchange))
        self.configure()
        self.run_for(self.HOURS_DEMO2 * HOUR, demo=True)
        self.assertTrue(server.TRADER.safe, server.TRADER.status())

        # ---------------- global invariants
        s = self.stats
        total_h = (sim.t - START) / HOUR
        self.assertGreater(total_h, 5.5)
        self.assertGreater(s["sl_hits"] + s["tp_hits"], 0)
        self.assertLessEqual(s["stale_polls_normal"], 0.01 * max(1, s["pos_polls_normal"]), s)
        self.assertLessEqual(s["stale_polls"], 0.10 * max(1, s["pos_polls"]), s)
        self.check_exchange_invariants()
        self.assertEqual(self.exceptions, [])
        self.assertTrue(all("ecb.europa.eu" in u for u in m.foreign), m.foreign[:3])
        print("\nSIM %.1f h: %s, ordenes demo=%d, http=%d" % (total_h, s, len(self.fake.orders), m.calls))

    def ensure_positions(self):
        """Advance until something is open (bounded) so the stop/kill phases matter."""
        for _ in range(12):
            if server.STATE["openPositions"]:
                return
            self.run_for(300, demo=True)
        self.fail("no se abrió ninguna posición en 1 h simulada")


if __name__ == "__main__":
    unittest.main()
