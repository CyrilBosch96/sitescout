#!/usr/bin/env python3
"""
Orchestrator — SiteScout
Usage: python3 orchestrator.py [--live]

Includes a lock file (scripts/hp_lock.py) — if a previous run is still in
progress (or crashed without cleaning up, checked via age), this run skips
instead of overlapping. The dashboard's manual "Trigger" buttons share the
same lock, so a manual run can't race the hourly cycle either.
"""

import json
import os
import subprocess
import sys
from datetime import date

import hp_env
import hp_config
import hp_lock

DAILY_SEND_LIMIT = hp_config.DAILY_SEND_LIMIT
NOTION_API_KEY = hp_env.NOTION_API_KEY
NOTION_DATA_SOURCE_ID = hp_env.NOTION_DATA_SOURCE_ID
STATE_FILE = os.path.join(hp_env.PROJECT_ROOT, "state.json")
SCRIPTS_DIR = os.path.join(hp_env.PROJECT_ROOT, "scripts")

LIVE = hp_env.live_allowed("--live" in sys.argv)


def acquire_lock():
    return hp_lock.acquire_lock()


def release_lock():
    hp_lock.release_lock()


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"active_niche": None, "active_location": None, "exhausted": False, "last_asked_at": None}


def run_script(script_name, extra_args=None):
    # sys.executable, not bare "python3" — a bare command name resolves via
    # PATH, and under launchd's minimal PATH a completely different (and
    # package-incomplete) Python can come first. Found live: a stray
    # /usr/local/bin/python3 (python.org 3.10, no pyyaml/flask installed)
    # shadowed /opt/homebrew/bin/python3 this way and broke every child
    # script the moment Sprint 12 added hp_config's yaml import.
    args = [sys.executable, os.path.join(SCRIPTS_DIR, script_name)]
    if extra_args:
        args.extend(extra_args)
    print(f"\n=== Running {script_name} {' '.join(extra_args or [])} ===")
    child_env = os.environ.copy()
    child_env[hp_lock.HELD_BY_PARENT_ENV_VAR] = "1"
    result = subprocess.run(args, env=child_env)
    return result.returncode == 0


def count_todays_new_sends():
    """Leads that got their FIRST email today — Contact Status "Follow 1" is
    only ever reached via a first-touch send (STATE_TABLE's "Not Contacted" ->
    "Follow 1" transition), so this counts new-lead volume specifically, not
    follow-ups. Follow-ups are never quota-capped — see cold_email.py."""
    import requests
    today = date.today().isoformat()
    url = f"https://api.notion.com/v1/data_sources/{NOTION_DATA_SOURCE_ID}/query"
    headers = {
        "Authorization": f"Bearer {NOTION_API_KEY}",
        "Notion-Version": "2025-09-03",
        "Content-Type": "application/json",
    }
    body = {
        "filter": {
            "and": [
                {"property": "Contact Status", "select": {"equals": "Follow 1"}},
                {"property": "Last Contact Date", "date": {"equals": today}},
            ]
        }
    }
    resp = requests.post(url, headers=headers, json=body)
    if resp.status_code != 200:
        print(f"Warning: couldn't check today's new-lead send count ({resp.status_code}), assuming 0.")
        return 0
    return len(resp.json().get("results", []))


def count_fresh_leads_awaiting_first_touch():
    """Leads genuinely ready for a first-touch send — Contact Status still
    "Not Contacted" — not just "any Box 1 lead we haven't given up on yet".

    Found live 2026-08-27: the old count (Box 1, not Lead Lost) counts a
    lead that's already deep into its follow-up cadence (Follow 1-4) just
    as much as a brand-new one, since a lead only leaves that count once
    it hits Lead Lost. With a real CRM of leads all already at Follow 3,
    that count sat at 17 (above DAILY_SEND_LIMIT) every single day, so
    Lead Discovery's top-up never triggered even though zero leads were
    actually new — the daily new-lead quota went completely unused with
    no visible error, just quietly never firing."""
    import requests
    url = f"https://api.notion.com/v1/data_sources/{NOTION_DATA_SOURCE_ID}/query"
    headers = {
        "Authorization": f"Bearer {NOTION_API_KEY}",
        "Notion-Version": "2025-09-03",
        "Content-Type": "application/json",
    }
    body = {
        "filter": {
            "and": [
                {"property": "Box", "select": {"equals": "Box 1"}},
                {"property": "Contact Status", "select": {"equals": "Not Contacted"}},
            ]
        }
    }
    resp = requests.post(url, headers=headers, json=body)
    if resp.status_code != 200:
        return 0
    return len(resp.json().get("results", []))


def main():
    if not acquire_lock():
        return {"notes": "lock held by another run, skipped"}

    try:
        mode = "LIVE" if LIVE else "DRY RUN"
        print(f"=== Orchestrator run: {mode} (HP_ENV={hp_env.HP_ENV}) ===")

        run_script("inbox_monitoring.py")
        run_script("bounce_check.py")

        new_sent_today = count_todays_new_sends()
        remaining_new_quota = max(0, DAILY_SEND_LIMIT - new_sent_today)
        print(f"\nNew leads emailed today: {new_sent_today}/{DAILY_SEND_LIMIT} (remaining: {remaining_new_quota})")

        # Quota gates the Lead Discovery top-up (no point discovering more
        # new leads today if we can't send to any), but NEVER gates whether
        # cold_email.py itself runs — due follow-ups are uncapped and must
        # always get their chance to send, quota or not.
        discovery_ran = False
        queue_check_ran = False
        if remaining_new_quota == 0:
            print("New-lead quota reached for today — skipping Lead Discovery top-up. Follow-ups still proceed below.")

        available = count_fresh_leads_awaiting_first_touch()
        print(f"Fresh leads awaiting first touch (Box 1, Not Contacted): {available}")

        if remaining_new_quota > 0 and available < DAILY_SEND_LIMIT:
            state = load_state()
            niche = state.get("active_niche")
            location = state.get("active_location")
            exhausted = state.get("exhausted", False)

            if not niche or not location or exhausted:
                print("No active niche/city (or it's exhausted) — running Queue Check to ask for one.")
                run_script("queue_check.py")
                queue_check_ran = True
            else:
                print(f"CRM running low — triggering Lead Discovery for '{niche}' in '{location}'.")
                run_script("lead_discovery_and_evaluation_Contactsearch.py", [niche, location])
                discovery_ran = True

                state = load_state()
                if state.get("exhausted"):
                    print("Lead Discovery marked this niche/city exhausted — running Queue Check.")
                    run_script("queue_check.py")
                    queue_check_ran = True
                available = count_fresh_leads_awaiting_first_touch()

        # Cyril's rule (2026-09-19): tomorrow's DAILY_SEND_LIMIT new leads must
        # never be at risk. If, after any top-up above, fresh stock is still
        # below the daily limit, keep asking for the next city every cycle
        # (queue_check.py self-throttles its own ask email) instead of only
        # asking once a city is fully exhausted.
        if not queue_check_ran and available < DAILY_SEND_LIMIT:
            print(f"Fresh stock ({available}) below the daily limit ({DAILY_SEND_LIMIT}) — asking for the next city.")
            run_script("queue_check.py", ["--low-supply"])

        cold_email_args = (["--live"] if LIVE else []) + ["--max-new-sends", str(remaining_new_quota)]
        run_script("cold_email.py", cold_email_args)

        # All five self-gate internally (time-of-day / dedup state,
        # Selected-lead counts + reply-thread state, approved-template +
        # no-existing-URL, once-per-calendar-day per lead, and
        # lapsed-expiry-date + unsent-resurrection respectively) — safe
        # to call every hour, each one no-ops until it's actually due.
        # Order matters: ghost_site_builder runs after
        # ghost_template_request since it depends on a niche having
        # already been approved; ghost_site_sequence runs after
        # ghost_site_builder since it needs the Ghost Site URL that just
        # got written to trigger Day 1; ghost_site_expiry runs last since
        # it only acts on leads ghost_site_sequence.py already flipped Live.
        run_script("check_opens_reminder.py")
        run_script("ghost_template_request.py")
        run_script("ghost_site_builder.py")
        sequence_args = ["--live"] if LIVE else []
        run_script("ghost_site_sequence.py", sequence_args)
        expiry_args = ["--live"] if LIVE else []
        run_script("ghost_site_expiry.py", expiry_args)

        print("\n=== Orchestrator run complete ===")

        return {
            "notes": f"mode={mode} remaining_new_quota={remaining_new_quota} discovery_ran={discovery_ran}",
        }

    finally:
        release_lock()


if __name__ == "__main__":
    import hp_runlog
    hp_runlog.run_wrapped("orchestrator", main)
