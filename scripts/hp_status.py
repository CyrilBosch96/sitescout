#!/usr/bin/env python3
"""
Live pipeline status + the "rejected leads" cache — dashboard support
module (2026-08-17).

get_current_status() reads state.db's `runs` table for a row still
marked "running" (hp_runlog.start_run() inserts one before main_func()
runs, hp_runlog.finish_run_*() closes it out) and translates the active
script into an operator-facing phrase. A "running" row older than
STALE_AFTER_SECONDS is treated as an orphan from a crash run_wrapped()
couldn't catch (e.g. an import-time SystemExit — a documented scope
boundary in hp_runlog.py) rather than shown as if still in progress.
"""

import json
import os
import sqlite3
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import hp_env

IST = ZoneInfo("Asia/Kolkata")

DB_FILE = os.path.join(hp_env.PROJECT_ROOT, "state.db")
CACHE_FILE = os.path.join(hp_env.PROJECT_ROOT, "checked_places_cache.json")

# Matches hp_lock's own staleness threshold — a "running" row older than
# this is more likely an orphan than a script actually still executing.
STALE_AFTER_SECONDS = 1800

STATUS_LABELS = {
    "lead_discovery_and_evaluation_Contactsearch": "Searching & qualifying leads",
    "queue_check": "Checking for your niche/city reply",
    "cold_email": "Sending emails",
    "inbox_monitoring": "Checking inbox for replies",
    "bounce_check": "Checking for bounced emails",
    "ghost_template_request": "Checking ghost site template requests",
    "ghost_site_builder": "Building ghost sites",
    "ghost_site_sequence": "Running ghost site preview sequence",
    "ghost_site_expiry": "Checking ghost site expirations",
    "check_opens_reminder": "Checking email opens",
    "daily_reporting": "Generating daily report",
    "orchestrator": "Running pipeline cycle",
}


def get_current_status():
    """Returns {"running": bool, "label": str, "script_name": str|None,
    "started_at": str|None}. "running" False means idle — nothing has an
    open run row right now (or the only one found is stale)."""
    conn = sqlite3.connect(DB_FILE, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT script_name, started_at FROM runs WHERE status = 'running' "
            "ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
    except sqlite3.OperationalError:
        # No script has ever run yet — the `runs` table (created lazily by
        # hp_runlog._connect() on first real use) doesn't exist.
        row = None
    finally:
        conn.close()

    if row is None:
        return {"running": False, "label": "Idle — waiting for the next scheduled run", "script_name": None, "started_at": None}

    # started_at is always a UTC ISO string (hp_runlog._now() ==
    # datetime.now(timezone.utc).isoformat()) — must parse it timezone-aware
    # and compare against an aware "now". A naive strptime/mktime pair would
    # silently interpret the stored UTC time as local wall-clock time,
    # producing a wildly wrong age on any machine not set to UTC.
    started_at = row["started_at"]
    try:
        started_dt = datetime.fromisoformat(started_at)
        if started_dt.tzinfo is None:
            started_dt = started_dt.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - started_dt).total_seconds()
    except ValueError:
        age = 0

    if age > STALE_AFTER_SECONDS:
        return {"running": False, "label": "Idle — waiting for the next scheduled run", "script_name": None, "started_at": None}

    script_name = row["script_name"]
    label = STATUS_LABELS.get(script_name, f"Running {script_name}")
    return {"running": True, "label": label, "script_name": script_name, "started_at": started_at}


def to_ist(value):
    """Every timestamp stored anywhere in this project (state.db, Notion
    date properties) is UTC — this is the one place that converts to IST
    for display, so the dashboard reads in Cyril's own local time instead
    of requiring a mental UTC offset every time (2026-08-18). Falsy or
    unparseable input passes through unchanged (falsy -> "—"), so this is
    safe to apply blindly to any "_at" field including already-missing
    ones like a still-running row's finished_at."""
    if not value:
        return "—"
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return value
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    ist = dt.astimezone(IST)
    return ist.strftime("%-I:%M %p, %b %-d") + " IST"


def get_checked_places_cache_info():
    if not os.path.exists(CACHE_FILE):
        return {"count": 0}
    with open(CACHE_FILE) as f:
        try:
            cache = json.load(f)
        except json.JSONDecodeError:
            return {"count": 0}
    return {"count": len(cache)}


def clear_checked_places_cache():
    """Empties the "already searched, rejected, don't re-check" cache
    (not_box1 / no_email / already-written place IDs). Rejected places
    aren't CRM leads — they never reach Notion — so this cache is the
    only record of "we already looked at this business and it didn't
    qualify." Clearing it means the next search re-evaluates every
    business in the area from scratch instead of skipping known rejects."""
    with open(CACHE_FILE, "w") as f:
        json.dump({}, f)
