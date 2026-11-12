#!/usr/bin/env bash
# Restart loop for the standalone GranMago paper engine (no systemd/cron on the box).
# * Exponential backoff 5s -> 300s when the engine exits; reset after a run > 10 min.
# * Heartbeat: every GRANMAGO_HEARTBEAT_EVERY s it checks status.json. If status.json
#   is not refreshed for GRANMAGO_HEARTBEAT_MAX s (default 300) or no engine cycle for
#   GRANMAGO_CYCLE_MAX_RUN_AGE s (default 600), it dumps the engine's thread stacks
#   (SIGUSR1 -> faulthandler -> engine.log), restarts the engine and logs it.
#   Ages are run time (CLOCK_MONOTONIC = /proc/uptime), so platform pauses of the
#   box VM, which freeze everything, never trigger a restart.
# SIGTERM/SIGINT to the watchdog stops the engine and exits.
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BOX="${GRANMAGO_BOX:-/workspace/granmago-box}"
LOG_DIR="${GRANMAGO_LOG_DIR:-$BOX/logs}"
RUN_DIR="${GRANMAGO_RUN_DIR:-$BOX/run}"
DATA_DIR="${DATA_DIR:-$BOX/data}"
PYTHON="${GRANMAGO_PYTHON:-python3}"
MIN_BACKOFF="${GRANMAGO_MIN_BACKOFF:-5}"
MAX_BACKOFF="${GRANMAGO_MAX_BACKOFF:-300}"
HEALTHY_RUN="${GRANMAGO_HEALTHY_RUN:-600}"
HB_EVERY="${GRANMAGO_HEARTBEAT_EVERY:-30}"
export DATA_DIR
mkdir -p "$LOG_DIR" "$RUN_DIR"
echo $$ > "$RUN_DIR/watchdog.pid"
child=""
stopping=0
wlog() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) watchdog: $*" >> "$LOG_DIR/watchdog.log"; }
mono() { cut -d' ' -f1 /proc/uptime; }
stop_child() {
  [ -n "$child" ] || return 0
  if kill -0 "$child" 2>/dev/null; then
    kill -TERM "$child" 2>/dev/null
    for _ in $(seq 1 20); do kill -0 "$child" 2>/dev/null || break; sleep 0.5; done
    kill -0 "$child" 2>/dev/null && kill -KILL "$child" 2>/dev/null
  fi
  wait "$child" 2>/dev/null
}
on_term() {
  stopping=1
  wlog "stop requested"
  stop_child
  rm -f "$RUN_DIR/engine.pid" "$RUN_DIR/watchdog.pid"
  exit 0
}
trap on_term TERM INT
backoff="$MIN_BACKOFF"
wlog "started (pid $$)"
while [ "$stopping" -eq 0 ]; do
  start=$(date +%s)
  # stdout/stderr go straight to a file (never a pipe), so the engine cannot block on output.
  "$PYTHON" -u "$HERE/granmago_standalone.py" >> "$LOG_DIR/engine.log" 2>&1 < /dev/null &
  child=$!
  child_mono=$(mono)
  echo "$child" > "$RUN_DIR/engine.pid"
  wlog "engine started pid $child"
  hung=0
  while kill -0 "$child" 2>/dev/null; do
    sleep "$HB_EVERY" & wait $!
    kill -0 "$child" 2>/dev/null || break
    reason=$("$PYTHON" "$HERE/heartbeat_check.py" "$DATA_DIR/status.json" "$child" "$child_mono" 2>&1)
    if [ $? -eq 1 ]; then
      wlog "HEARTBEAT engine pid $child hung: $reason; dumping stacks and restarting"
      kill -USR1 "$child" 2>/dev/null; sleep 2
      hung=1
      stop_child
      break
    fi
  done
  wait "$child" 2>/dev/null
  rc=$?
  [ "$stopping" -eq 1 ] && break
  ran=$(( $(date +%s) - start ))
  if [ "$hung" -eq 1 ] || [ "$ran" -ge "$HEALTHY_RUN" ]; then backoff="$MIN_BACKOFF"; fi
  wlog "engine pid $child exited rc=$rc hung=$hung after ${ran}s; restart in ${backoff}s"
  sleep "$backoff" & wait $!
  backoff=$(( backoff * 2 )); [ "$backoff" -gt "$MAX_BACKOFF" ] && backoff="$MAX_BACKOFF"
done
