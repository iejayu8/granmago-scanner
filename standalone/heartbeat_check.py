"""Heartbeat check used by watchdog.sh (exit 0 = alive, 1 = hung, 2 = no verdict yet).

Ages are measured in *run time*: CLOCK_MONOTONIC, the same clock as
/proc/uptime, which stops while the platform pauses the box VM. A pause
therefore never looks like a hang; only a process that is running and not
progressing does.

Usage: heartbeat_check.py STATUS_JSON ENGINE_PID ENGINE_START_MONO
"""
import json
import os
import sys
import time

STATUS_MAX = float(os.environ.get("GRANMAGO_HEARTBEAT_MAX", "300"))     # status.json not refreshed
CYCLE_MAX = float(os.environ.get("GRANMAGO_CYCLE_MAX_RUN_AGE", "600"))   # no engine cycle
GRACE = float(os.environ.get("GRANMAGO_STARTUP_GRACE", "180"))


def verdict(status, pid, started_mono, now_mono, status_max=STATUS_MAX, cycle_max=CYCLE_MAX, grace=GRACE):
    """(code, reason). status: parsed status.json or None."""
    since_start = now_mono - started_mono
    if not status or status.get("pid") != pid:
        if since_start > max(grace, status_max):
            return 1, "no status.json from engine pid %s after %.0fs run time" % (pid, since_start)
        return 2, "waiting for first status (%.0fs)" % since_start
    stamp = status.get("monotonicAt")
    if not isinstance(stamp, (int, float)):
        return 2, "status without monotonicAt"
    status_age = now_mono - stamp
    if status_age > status_max:
        return 1, "status.json stale %.0fs run time (> %.0f)" % (status_age, status_max)
    run_age = status.get("lastCycleRunAgeSeconds")
    if status.get("running") and isinstance(run_age, (int, float)) and run_age > cycle_max:
        return 1, "no engine cycle for %.0fs run time (> %.0f)" % (run_age, cycle_max)
    return 0, "ok status_age=%.0fs cycle_run_age=%s" % (status_age, run_age)


def main(argv):
    path, pid, started = argv[1], int(argv[2]), float(argv[3])
    try:
        with open(path, encoding="utf-8") as fh:
            status = json.load(fh)
    except (OSError, ValueError):
        status = None
    code, reason = verdict(status, pid, started, time.monotonic())
    print(reason)
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv))
