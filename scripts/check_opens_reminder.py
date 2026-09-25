#!/usr/bin/env python3
"""
Check-Opens Reminder — SiteScout
Usage: python3 check_opens_reminder.py

Fires once daily, 3 hours after the last of the day's timezone-gated
cold-email send windows (Pacific's local 8am — the latest of PT/MT/CT/ET
in absolute time, so by then every timezone's batch for the day has gone
out). Prompts Cyril to check Mail Suite for opens — no automated
open-tracking exists (Mail Suite's free tier has no API), this is
intentionally a manual step, per his own workflow.

Runs the same way cold_email.py's own timezone gating does: an internal
current-time check against a real IANA zone (correct across DST without
needing a schedule adjustment twice a year), not a launchd
StartCalendarInterval pinned to a wall-clock hour. Called every hour from
orchestrator.py's chain; self-gates on both the target hour and a
once-per-day dedup so it only actually sends once.
"""

import json
import os
import subprocess
from datetime import date, datetime
from zoneinfo import ZoneInfo

import hp_env
import hp_config

RECIPIENT_ADDRESS = hp_config.OPERATOR_EMAIL
STATE_FILE = os.path.join(hp_env.PROJECT_ROOT, "check_opens_reminder_state.json")

# 3 hours after Pacific's 8am send window — the last of the 4 timezone
# windows (PT/MT/CT/ET) to fire each day, so every lead's cold email has
# had its chance to send by the time this reminder goes out.
TARGET_PT_HOUR = 11


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"last_sent_date": None}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def is_reminder_time():
    now_pt = datetime.now(ZoneInfo("America/Los_Angeles"))
    return now_pt.hour == TARGET_PT_HOUR


def send_email(to_address, subject, body):
    message = f"From: {RECIPIENT_ADDRESS}\nTo: {to_address}\nSubject: {subject}\n\n{body}\n"
    result = subprocess.run(
        ["himalaya", "message", "send"],
        input=message, text=True, capture_output=True,
    )
    return result.returncode == 0, result.stderr


def main():
    if not is_reminder_time():
        return {"notes": "not reminder time yet"}

    state = load_state()
    today = date.today().isoformat()
    if state.get("last_sent_date") == today:
        return {"notes": "already sent today"}

    subject = "Check your email opens"
    body = (
        "Today's cold emails should all be sent by now across every timezone.\n\n"
        "Head over to Mail Suite and check which ones have been opened, then mark "
        "any you want to pursue for a ghost site as Selected in Notion "
        "(or via the dashboard's bulk-select)."
    )
    success, err = send_email(hp_env.resolve_recipient(RECIPIENT_ADDRESS), subject, body)
    if success:
        state["last_sent_date"] = today
        save_state(state)
        print("Reminder sent.")
        return {"notes": "reminder sent"}

    print(f"Failed to send reminder: {err}")
    return {"notes": f"send failed: {err[:200]}"}


if __name__ == "__main__":
    import hp_runlog
    hp_runlog.run_wrapped("check_opens_reminder", main)
