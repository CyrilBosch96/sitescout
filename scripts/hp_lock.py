#!/usr/bin/env python3
"""
Shared run-lock — prevents two pipeline-mutating script runs from
overlapping, whichever of orchestrator.py's hourly cycle or the
dashboard's manual "Trigger" button starts them.

One lock file guards both entry points on purpose: a manual trigger
racing the hourly orchestrator (or another manual trigger) is exactly
the scenario that caused real duplicate Notion leads (2026-08-17) —
two concurrent lead_discovery runs each queried Notion's existing-names
list before either had written anything, so both independently wrote
the same qualifying businesses.
"""

import os
import time

import hp_env
import hp_config

LOCK_FILE = os.path.join(hp_env.PROJECT_ROOT, "orchestrator.lock")
LOCK_MAX_AGE_SECONDS = hp_config.ORCHESTRATOR_LOCK_MAX_AGE_SECONDS

# Set by orchestrator.py in the environment of every child script it
# subprocesses, since orchestrator.py already holds the lock for the
# whole cycle before any child starts. Lets a script that also wants to
# be safe to invoke *standalone* (see lead_discovery's own acquire_lock()
# call) skip re-acquiring a lock its parent already holds, rather than
# refusing to run under its own parent.
HELD_BY_PARENT_ENV_VAR = "HP_LOCK_HELD_BY_PARENT"


def acquire_lock():
    if os.path.exists(LOCK_FILE):
        age = time.time() - os.path.getmtime(LOCK_FILE)
        if age < LOCK_MAX_AGE_SECONDS:
            print(f"Another run appears to be in progress (lock file is {int(age)}s old). Skipping this run.")
            return False
        else:
            print(f"Found a stale lock file ({int(age)}s old) — likely a crashed run. Proceeding anyway.")
    with open(LOCK_FILE, "w") as f:
        f.write(str(time.time()))
    return True


def release_lock():
    if os.path.exists(LOCK_FILE):
        os.remove(LOCK_FILE)


def refresh_lock():
    """Touches the lock file's mtime so a genuinely still-running cycle
    doesn't get treated as stale/abandoned just because it's taking a
    while. Found live 2026-08-31: a real Lead Discovery run against a
    large market (Los Angeles) plus the crawler's own added latency
    (per-site booking-page-slug probing) legitimately took over 30
    minutes — LOCK_MAX_AGE_SECONDS's original single-cycle assumption
    from Sprint 8, causing a second orchestrator cycle to treat the
    lock as abandoned and start a genuinely concurrent duplicate run,
    the exact double-write scenario this lock exists to prevent.
    Callers inside a long-running loop (lead_discovery's per-place loop)
    should call this periodically. Safe no-op if no lock is currently
    held — e.g. a script run standalone outside the orchestrator."""
    if os.path.exists(LOCK_FILE):
        os.utime(LOCK_FILE, None)
