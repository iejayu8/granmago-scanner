#!/usr/bin/env bash
# Restart loop for the standalone GranMago paper engine (no systemd on the box).
# Exponential backoff 5s -> 300s; reset after a run longer than 10 minutes.
# SIGTERM/SIGINT to the watchdog stops the engine and exits.
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BOX="${GRANMAGO_BOX:-/workspace/granmago-box}"
LOG_DIR="${GRANMAGO_LOG_DIR:-$BOX/logs}"
RUN_DIR="${GRANMAGO_RUN_DIR:-$BOX/run}"
PYTHON="${GRANMAGO_PYTHON:-python3}"
MIN_BACKOFF="${GRANMAGO_MIN_BACKOFF:-5}"
MAX_BACKOFF="${GRANMAGO_MAX_BACKOFF:-300}"
HEALTHY_RUN="${GRANMAGO_HEALTHY_RUN:-600}"
mkdir -p "$LOG_DIR" "$RUN_DIR"
echo $$ > "$RUN_DIR/watchdog.pid"
child=""
stopping=0
wlog() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) watchdog: $*" >> "$LOG_DIR/watchdog.log"; }
on_term() {
  stopping=1
  wlog "stop requested"
  if [ -n "$child" ] && kill -0 "$child" 2>/dev/null; then
    kill -TERM "$child" 2>/dev/null
    for _ in $(seq 1 20); do kill -0 "$child" 2>/dev/null || break; sleep 0.5; done
    kill -0 "$child" 2>/dev/null && kill -KILL "$child" 2>/dev/null
  fi
  rm -f "$RUN_DIR/engine.pid" "$RUN_DIR/watchdog.pid"
  exit 0
}
trap on_term TERM INT
backoff="$MIN_BACKOFF"
wlog "started (pid $$)"
while [ "$stopping" -eq 0 ]; do
  start=$(date +%s)
  "$PYTHON" -u "$HERE/granmago_standalone.py" >> "$LOG_DIR/engine.log" 2>&1 &
  child=$!
  echo "$child" > "$RUN_DIR/engine.pid"
  wlog "engine started pid $child"
  wait "$child"
  rc=$?
  [ "$stopping" -eq 1 ] && break
  ran=$(( $(date +%s) - start ))
  if [ "$ran" -ge "$HEALTHY_RUN" ]; then backoff="$MIN_BACKOFF"; fi
  wlog "engine pid $child exited rc=$rc after ${ran}s; restart in ${backoff}s"
  sleep "$backoff" & wait $!
  backoff=$(( backoff * 2 )); [ "$backoff" -gt "$MAX_BACKOFF" ] && backoff="$MAX_BACKOFF"
done
