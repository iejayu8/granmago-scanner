"""GranMago standalone runner: the same 1.6.2 engine outside Home Assistant.

PAPER ONLY. It imports granmago/server.py unchanged and adds, from outside:

* env defaults equal to the add-on defaults (risk 1%, 3x, 3 positions,
  TP 1.7R pinned by the engine, the 28 liquid pairs, fee 0.06%, slippage 0.02%),
* bind to 127.0.0.1 by default (GRANMAGO_HOST to change it),
* an append-only fiscal journal fed by every close_position() call,
* GET /status and an atomic DATA_DIR/status.json for supervisors,
* autostart (STATE["running"] = True) so no HA panel is needed.

Nothing here talks to Home Assistant or the Supervisor, and no exchange keys
are read: the engine only uses public Blofin market data.
"""
import json
import logging
import os
import socket
import sys
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ENGINE_DIR = HERE.parent / "granmago"

DEFAULT_ENV = {
    "DATA_DIR": "/workspace/granmago-box/data",
    "PORT": "8797",
    "RISK_PCT": "1",
    "LEVERAGE": "3",
    "MAX_POSITIONS": "3",
    "FEE_PCT_PER_SIDE": "0.06",
    "SLIPPAGE_PCT_PER_SIDE": "0.02",
    "PAIRS": "",            # empty = the engine's 28 LIQUID_PAIRS
    "INITIAL_BALANCE": "1000",
}
FORBIDDEN_ENV = ("SUPERVISOR_TOKEN", "HASSIO_TOKEN", "HA_TOKEN")
MODE = "paper"
STATUS_INTERVAL = float(os.environ.get("GRANMAGO_STATUS_INTERVAL", "15"))
FX_INTERVAL = float(os.environ.get("GRANMAGO_FX_INTERVAL", "1800"))

server = None
journal = None
_STARTED_MONO = time.monotonic()
_STARTED_AT = datetime.now(timezone.utc)
_ERRORS = deque(maxlen=50)
_JOURNAL_ERRORS = deque(maxlen=20)
_FX_STATE = {"lastAttemptAt": None, "lastOkAt": None, "lastError": None}
_STATUS_LOCK = threading.Lock()


class SuspendDetector:
    """Detect box freezes: wall time jumps while CLOCK_MONOTONIC does not.

    The Grok Bot box is a VM that the platform pauses when no bot is active.
    During a pause every process is frozen (CLOCK_MONOTONIC and /proc/uptime
    stop too) and on resume the wall clock is stepped forward. A gap of wall
    time larger than the monotonic gap is that freeze, not a hang.
    """

    def __init__(self, threshold=30.0, wall=time.time, mono=time.monotonic):
        self.threshold = float(threshold)
        self._wall, self._mono = wall, mono
        self._last = (wall(), mono())
        self.count = 0
        self.total = 0.0
        self.recent = deque(maxlen=20)

    def tick(self):
        """Return the suspension dict when one ended since the last tick, else None."""
        wall, mono = self._wall(), self._mono()
        lw, lm = self._last
        self._last = (wall, mono)
        frozen = (wall - lw) - (mono - lm)
        if frozen < self.threshold:
            return None
        ev = {"from": _iso(lw), "to": _iso(wall), "seconds": round(frozen, 1)}
        self.count += 1
        self.total += frozen
        self.recent.append(ev)
        return ev

    def as_dict(self):
        return {"count": self.count, "totalSeconds": round(self.total, 1),
                "last": self.recent[-1] if self.recent else None, "recent": list(self.recent)[-5:]}


class CycleTracker:
    """Run-time (monotonic) age of the last engine cycle; immune to box pauses."""

    def __init__(self, mono=time.monotonic):
        self._mono = mono
        self.value = None
        self.at = mono()

    def observe(self, last_tick):
        if last_tick != self.value:
            self.value = last_tick
            self.at = self._mono()
        return round(self._mono() - self.at, 1)


SUSPEND = SuspendDetector(float(os.environ.get("GRANMAGO_SUSPEND_THRESHOLD", "30")))
CYCLE = CycleTracker()
CYCLE_MAX_RUN_AGE = float(os.environ.get("GRANMAGO_CYCLE_MAX_RUN_AGE", "600"))


def _iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _utc_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def apply_env_defaults(environ=None):
    environ = os.environ if environ is None else environ
    for key, value in DEFAULT_ENV.items():
        environ.setdefault(key, value)
    for key in FORBIDDEN_ENV:  # never hand HA credentials to the engine
        environ.pop(key, None)
    return environ


def bind_host(environ=None):
    environ = os.environ if environ is None else environ
    return environ.get("GRANMAGO_HOST", "127.0.0.1") or "127.0.0.1"


class _ErrorCapture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.WARNING)

    def emit(self, record):
        try:
            _ERRORS.append({"at": _utc_now(), "level": record.levelname,
                            "logger": record.name, "msg": record.getMessage()[:500]})
        except Exception:
            pass


def load_engine():
    """Import granmago/server.py after env defaults. Returns the module."""
    global server
    if server is None:
        apply_env_defaults()
        if str(ENGINE_DIR) not in sys.path:
            sys.path.insert(0, str(ENGINE_DIR))
        import server as engine  # noqa: E402
        server = engine
    return server


def install_journal(engine, fj):
    """Wrap engine.close_position so every new history row is journaled."""
    original = engine.close_position
    if getattr(original, "_fiscal_wrapped", False):
        return original

    def close_position(*args, **kwargs):
        hist = engine.STATE.get("history") or []
        before = hist[0] if hist else None
        pos = original(*args, **kwargs)
        hist = engine.STATE.get("history") or []
        if pos is not None and hist and hist[0] is not before:
            try:
                fj.record_close(hist[0], pos)
            except Exception as exc:  # journaling must never break the engine
                _JOURNAL_ERRORS.append({"at": _utc_now(), "msg": repr(exc)[:300]})
                logging.getLogger("granmago.standalone").error("fiscal journal append failed: %r", exc)
        return pos

    close_position._fiscal_wrapped = True
    close_position._original = original
    engine.close_position = close_position
    return close_position


def uninstall_journal(engine):
    fn = engine.close_position
    if getattr(fn, "_fiscal_wrapped", False):
        engine.close_position = fn._original


def build_status(engine, fj=None):
    snap = engine.public_state()
    positions = []
    for p in snap.get("openPositions") or []:
        positions.append({k: p.get(k) for k in (
            "pair", "side", "entry", "sl", "tp", "notional", "riskAmt", "time", "tradeId", "price",
            "unrealized", "priceStale")})
    last_tick = snap.get("lastTickAt")
    last_scan = snap.get("lastScanAt")
    age = None
    try:
        if last_tick:
            age = round(time.time() - datetime.fromisoformat(last_tick).timestamp(), 1)
    except ValueError:
        age = None
    running = bool(snap.get("running"))
    errors = list(_ERRORS)[-10:]
    uptime = time.monotonic() - _STARTED_MONO
    # Run-time age (excludes box pauses). Wall age is kept for humans only.
    run_age = CYCLE.observe(last_tick)
    if not running:
        health = "stopped"
    elif last_tick is None and uptime < CYCLE_MAX_RUN_AGE:
        health = "starting"
    elif run_age > CYCLE_MAX_RUN_AGE:
        health = "stale"
    elif _JOURNAL_ERRORS or (errors and errors[-1]["level"] in ("ERROR", "CRITICAL")):
        health = "degraded"
    else:
        health = "ok"
    out = {
        "schema": 2,
        "generatedAt": _utc_now(),
        "health": health,
        "version": engine.VERSION,
        "mode": MODE,
        "realMoney": False,
        "running": running,
        "pid": os.getpid(),
        "host": socket.gethostname(),
        "bind": "%s:%s" % (bind_host(), engine.PORT),
        "dataDir": str(engine.DATA_DIR),
        "startedAt": _STARTED_AT.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "uptimeSeconds": round(time.monotonic() - _STARTED_MONO, 1),
        "balance": snap.get("balance"),
        "initialBalance": snap.get("initialBalance"),
        "openPositions": positions,
        "openCount": len(positions),
        "closedInHistory": len(snap.get("history") or []),
        "lastCycleAt": last_tick,
        "lastCycleAgeSeconds": age,
        "lastCycleRunAgeSeconds": run_age,
        "monotonicAt": round(time.monotonic(), 1),
        "wallUptimeSeconds": round(time.time() - _STARTED_AT.timestamp(), 1),
        "suspensions": SUSPEND.as_dict(),
        "heartbeat": {"cycleMaxRunAgeSeconds": CYCLE_MAX_RUN_AGE, "statusIntervalSeconds": STATUS_INTERVAL},
        "lastScanAt": last_scan,
        "recentErrors": errors,
        "journalErrors": list(_JOURNAL_ERRORS),
        "config": {
            "riskPct": (snap.get("settings") or {}).get("riskPct"),
            "leverage": engine._leverage(),
            "maxPositions": engine.max_positions(),
            "takeProfitR": engine.take_profit_r(),
            "pairs": len(engine.active_pairs()),
            "feePctPerSide": engine.fee_pct_per_side(),
            "slippagePctPerSide": engine.slippage_pct_per_side(),
        },
        "recentLog": (snap.get("log") or [])[:10],
    }
    if fj is not None:
        try:
            out["journal"] = dict(fj.counts(), path=str(fj.path), fx=dict(_FX_STATE))
        except Exception as exc:
            out["journal"] = {"error": repr(exc)[:200]}
    return out


def write_status(path, payload):
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    with _STATUS_LOCK:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, default=str)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)


def install_status_route(engine, fj):
    from flask import jsonify
    if "standalone_status" in engine.app.view_functions:
        return

    def standalone_status():
        return jsonify(build_status(engine, fj))

    engine.app.add_url_rule("/status", "standalone_status", standalone_status, methods=["GET"])


def _status_loop(engine, fj, stop):
    path = engine.DATA_DIR / "status.json"
    while not stop.is_set():
        ev = SUSPEND.tick()
        if ev is not None:
            on_resume(engine, ev)
        try:
            write_status(path, build_status(engine, fj))
        except Exception as exc:
            logging.getLogger("granmago.standalone").warning("status.json write failed: %r", exc)
        stop.wait(STATUS_INTERVAL)


def on_resume(engine, ev):
    """Log a box pause and start a fresh cycle right away."""
    msg = ("Caja en pausa %.0f min (%s -> %s): la plataforma suspendió la VM; reanudado."
           % (ev["seconds"] / 60.0, ev["from"], ev["to"]))
    logging.getLogger("granmago.standalone").info(msg)
    try:
        with engine.LOCK:
            engine.STATE["log"].insert(0, "[%s] %s" % (datetime.now().strftime("%H:%M:%S"), msg))
            del engine.STATE["log"][80:]
        engine.kick()
    except Exception as exc:
        logging.getLogger("granmago.standalone").warning("resume handling failed: %r", exc)


def _fx_loop(fj, stop, csv_path):
    from fiscal_journal import fetch_ecb_rates
    while not stop.is_set():
        try:
            if fj.pending_fx():
                _FX_STATE["lastAttemptAt"] = _utc_now()
                rates = fetch_ecb_rates()
                fj.resolve_pending(rates)
                _FX_STATE["lastOkAt"] = _utc_now()
                _FX_STATE["lastError"] = None
            fj.export_csv(csv_path)
        except Exception as exc:  # FX down: rows stay pending, retried later
            _FX_STATE["lastError"] = repr(exc)[:200]
            try:
                fj.export_csv(csv_path)
            except Exception:
                pass
        stop.wait(FX_INTERVAL)


def main():
    global journal
    engine = load_engine()
    if str(HERE) not in sys.path:
        sys.path.insert(0, str(HERE))
    from fiscal_journal import FiscalJournal
    logging.getLogger().addHandler(_ErrorCapture())
    import faulthandler
    import signal
    faulthandler.register(signal.SIGUSR1, all_threads=True)  # `kill -USR1 <pid>` -> stacks in engine.log
    engine.DATA_DIR.mkdir(parents=True, exist_ok=True)
    journal = FiscalJournal(engine.DATA_DIR / "fiscal_journal.sqlite", mode=MODE,
                            engine_version=engine.VERSION, instance=os.environ.get("GRANMAGO_INSTANCE", "box"))
    install_journal(engine, journal)
    install_status_route(engine, journal)
    with engine.LOCK:
        if os.environ.get("GRANMAGO_AUTOSTART", "1") != "0":
            engine.STATE["running"] = True
        engine.add_log("Modo standalone PAPER en la caja (sin HA). Diario fiscal: %s" % journal.path)
    stop = threading.Event()
    threading.Thread(target=_status_loop, args=(engine, journal, stop), name="standalone-status", daemon=True).start()
    threading.Thread(target=_fx_loop, args=(journal, stop, engine.DATA_DIR / "fiscal_journal.csv"),
                     name="standalone-fx", daemon=True).start()

    import waitress
    host = bind_host()
    real_serve = waitress.serve

    def serve(app, **kw):  # engine.main() hardcodes 0.0.0.0; force our bind
        kw["host"] = host
        logging.getLogger("granmago.standalone").info("standalone listening on %s:%s", host, kw.get("port"))
        return real_serve(app, **kw)

    waitress.serve = serve
    engine.main()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    main()
