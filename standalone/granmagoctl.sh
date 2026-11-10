#!/usr/bin/env bash
# granmagoctl.sh start|stop|restart|status|logs  -- standalone PAPER engine on the box.
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BOX="${GRANMAGO_BOX:-/workspace/granmago-box}"
LOG_DIR="${GRANMAGO_LOG_DIR:-$BOX/logs}"
RUN_DIR="${GRANMAGO_RUN_DIR:-$BOX/run}"
PORT="${PORT:-8797}"
export GRANMAGO_BOX="$BOX" GRANMAGO_LOG_DIR="$LOG_DIR" GRANMAGO_RUN_DIR="$RUN_DIR"
alive() { [ -f "$1" ] && kill -0 "$(cat "$1")" 2>/dev/null; }
case "${1:-status}" in
  start)
    if alive "$RUN_DIR/watchdog.pid"; then echo "already running (watchdog $(cat "$RUN_DIR/watchdog.pid"))"; exit 0; fi
    mkdir -p "$LOG_DIR" "$RUN_DIR"
    nohup setsid bash "$HERE/watchdog.sh" >> "$LOG_DIR/watchdog.log" 2>&1 < /dev/null &
    sleep 1
    echo "watchdog $(cat "$RUN_DIR/watchdog.pid" 2>/dev/null) started; logs in $LOG_DIR"
    ;;
  stop)
    if alive "$RUN_DIR/watchdog.pid"; then
      kill -TERM "$(cat "$RUN_DIR/watchdog.pid")"
      for _ in $(seq 1 30); do alive "$RUN_DIR/watchdog.pid" || break; sleep 0.5; done
      echo "stopped"
    else
      echo "not running"
      alive "$RUN_DIR/engine.pid" && kill -TERM "$(cat "$RUN_DIR/engine.pid")" && echo "stray engine stopped"
    fi
    ;;
  restart) "$0" stop; "$0" start ;;
  status)
    alive "$RUN_DIR/watchdog.pid" && echo "watchdog: $(cat "$RUN_DIR/watchdog.pid") up" || echo "watchdog: down"
    alive "$RUN_DIR/engine.pid" && echo "engine:   $(cat "$RUN_DIR/engine.pid") up" || echo "engine:   down"
    curl -s -m 5 "http://127.0.0.1:$PORT/status" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("health=%s version=%s mode=%s running=%s balance=%s open=%s lastCycle=%s uptime=%ss" % (d["health"], d["version"], d["mode"], d["running"], d["balance"], d["openCount"], d["lastCycleAt"], d["uptimeSeconds"]))' 2>/dev/null || echo "status endpoint unreachable"
    ;;
  logs) tail -n "${2:-50}" "$LOG_DIR/engine.log" ;;
  *) echo "usage: $0 start|stop|restart|status|logs [n]"; exit 2 ;;
esac
