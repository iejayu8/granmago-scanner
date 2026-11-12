#!/usr/bin/env bash
# granmagoctl.sh start|ensure|stop|restart|status|logs  -- standalone PAPER engine on the box.
# start/ensure are idempotent: they do nothing when the watchdog is already up, and are
# safe to run from a periodic routine (pid files are checked against the real command
# line, so a stale pid file reused by another process after a box restart is ignored).
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BOX="${GRANMAGO_BOX:-/workspace/granmago-box}"
LOG_DIR="${GRANMAGO_LOG_DIR:-$BOX/logs}"
RUN_DIR="${GRANMAGO_RUN_DIR:-$BOX/run}"
PORT="${PORT:-8797}"
export GRANMAGO_BOX="$BOX" GRANMAGO_LOG_DIR="$LOG_DIR" GRANMAGO_RUN_DIR="$RUN_DIR"
# alive PIDFILE PATTERN: pid exists and its command line matches PATTERN.
alive() {
  [ -f "$1" ] || return 1
  local pid; pid=$(cat "$1" 2>/dev/null) || return 1
  [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null || return 1
  tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null | grep -q -- "$2"
}
W=watchdog.sh
E=granmago_standalone.py
do_start() {
  mkdir -p "$LOG_DIR" "$RUN_DIR"
  exec 9> "$RUN_DIR/ctl.lock"
  flock -w 30 9 || { echo "another granmagoctl is running"; exit 1; }
  if alive "$RUN_DIR/watchdog.pid" "$W"; then echo "already running (watchdog $(cat "$RUN_DIR/watchdog.pid"))"; return 0; fi
  if alive "$RUN_DIR/engine.pid" "$E"; then  # orphan engine without watchdog
    kill -TERM "$(cat "$RUN_DIR/engine.pid")"; sleep 3
  fi
  rm -f "$RUN_DIR/watchdog.pid" "$RUN_DIR/engine.pid"
  echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) ctl: start (${1:-manual})" >> "$LOG_DIR/watchdog.log"
  nohup setsid bash "$HERE/watchdog.sh" >> "$LOG_DIR/watchdog.log" 2>&1 < /dev/null 9>&- &
  for _ in $(seq 1 10); do alive "$RUN_DIR/watchdog.pid" "$W" && break; sleep 0.5; done
  echo "watchdog $(cat "$RUN_DIR/watchdog.pid" 2>/dev/null) started; logs in $LOG_DIR"
}
case "${1:-status}" in
  start) do_start manual ;;
  ensure) do_start ensure ;;
  stop)
    if alive "$RUN_DIR/watchdog.pid" "$W"; then
      kill -TERM "$(cat "$RUN_DIR/watchdog.pid")"
      for _ in $(seq 1 30); do alive "$RUN_DIR/watchdog.pid" "$W" || break; sleep 0.5; done
      echo "stopped"
    else
      echo "not running"
      alive "$RUN_DIR/engine.pid" "$E" && kill -TERM "$(cat "$RUN_DIR/engine.pid")" && echo "stray engine stopped"
    fi
    ;;
  restart) "$0" stop; "$0" start ;;
  status)
    alive "$RUN_DIR/watchdog.pid" "$W" && echo "watchdog: $(cat "$RUN_DIR/watchdog.pid") up" || echo "watchdog: down"
    alive "$RUN_DIR/engine.pid" "$E" && echo "engine:   $(cat "$RUN_DIR/engine.pid") up" || echo "engine:   down"
    curl -s -m 5 "http://127.0.0.1:$PORT/status" | python3 -c 'import json,sys; d=json.load(sys.stdin); s=d.get("suspensions") or {}; print("health=%s version=%s mode=%s running=%s balance=%s open=%s lastCycle=%s cycleRunAge=%ss uptime=%ss boxPauses=%s (%.0f min)" % (d["health"], d["version"], d["mode"], d["running"], d["balance"], d["openCount"], d["lastCycleAt"], d.get("lastCycleRunAgeSeconds"), d["uptimeSeconds"], s.get("count"), (s.get("totalSeconds") or 0)/60))' 2>/dev/null || echo "status endpoint unreachable"
    ;;
  logs) tail -n "${2:-50}" "$LOG_DIR/engine.log" ;;
  *) echo "usage: $0 start|ensure|stop|restart|status|logs [n]"; exit 2 ;;
esac
