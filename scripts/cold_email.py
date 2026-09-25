#!/usr/bin/env python3
"""
Cold Email + Follow-Up sender — SiteScout
Usage:
  python3 cold_email.py            (dry-run: prints what WOULD send, no real emails)
  python3 cold_email.py --live     (actually sends + updates Notion)

Real threading: first-touch generates a Message-ID, stores it in Notion,
follow-ups reference it via In-Reply-To/References so Gmail threads
everything correctly. Follow-up content is fixed (no AI calls needed).
Timezone-gated: only sends when it's ~8am local for that specific lead.
"""

import sys
import os
import subprocess
import requests
from datetime import datetime, date, timedelta
from email.utils import make_msgid
from zoneinfo import ZoneInfo

import hp_env
import hp_config
import hp_template

DAILY_SEND_LIMIT = hp_config.DAILY_SEND_LIMIT
FROM_ADDRESS = hp_config.OPERATOR_EMAIL

NOTION_API_KEY = hp_env.NOTION_API_KEY
NOTION_DATA_SOURCE_ID = hp_env.NOTION_DATA_SOURCE_ID

TIMEZONE_TO_IANA = {
    "PT": "America/Los_Angeles",
    "MT": "America/Denver",
    "CT": "America/Chicago",
    "ET": "America/New_York",
}
TARGET_LOCAL_HOUR = hp_config.COLD_EMAIL_TARGET_LOCAL_HOUR
SEND_WINDOW_END_HOUR = hp_config.COLD_EMAIL_SEND_WINDOW_END_HOUR

FOLLOWUP_TEMPLATES_DIR = os.path.join(hp_env.PROJECT_ROOT, "email_templates")


def load_followup_text(followup_num):
    """Follow-up copy lives in email_templates/followup_{n}.md (Sprint 22)
    so it's editable from the dashboard's Content page without a code
    deploy — same file-backed pattern ghost_site_sequence.py's
    load_template() already established for the Track B sequence."""
    path = os.path.join(FOLLOWUP_TEMPLATES_DIR, f"followup_{followup_num}.md")
    with open(path) as f:
        return f.read().rstrip("\n")


DRY_RUN = not hp_env.live_allowed("--live" in sys.argv)


def _parse_max_new_sends(argv):
    """--max-new-sends N, passed by orchestrator.py as (DAILY_SEND_LIMIT minus
    however many new leads already got a first email today) — so the daily cap
    on *new* leads holds across multiple hourly invocations, not just within a
    single run. Defaults to the full DAILY_SEND_LIMIT for standalone runs.
    Follow-ups are never capped by this — see main()."""
    if "--max-new-sends" in argv:
        idx = argv.index("--max-new-sends")
        if idx + 1 < len(argv):
            try:
                return max(0, int(argv[idx + 1]))
            except ValueError:
                pass
    return DAILY_SEND_LIMIT


MAX_NEW_SENDS = _parse_max_new_sends(sys.argv)

STATE_TABLE = {
    "Not Contacted": {"delta": 0, "next": "Follow 1", "followup_num": 0},
    "Follow 1":      {"delta": 3, "next": "Follow 2", "followup_num": 1},
    "Follow 2":      {"delta": 4, "next": "Follow 3", "followup_num": 2},
    "Follow 3":      {"delta": 7, "next": "Follow 4", "followup_num": 3},
    "Follow 4":      {"delta": 14, "next": "Lead Lost", "followup_num": 4},
}
STOP_REPLY_STATUSES = {"Interested", "Not Interested", "Unsubscribed"}


def is_local_send_time(timezone_abbr):
    # No resolvable timezone means we don't know the lead's local hour at
    # all — skip rather than default to "always eligible," which would
    # send at literally any hour including the middle of the night.
    if not timezone_abbr or timezone_abbr not in TIMEZONE_TO_IANA:
        return False
    local_now = datetime.now(ZoneInfo(TIMEZONE_TO_IANA[timezone_abbr]))
    # A window (not a single matching hour) so a lead isn't entirely
    # dependent on the orchestrator happening to run during its one exact
    # local hour — paired with orchestrator.py's 10-minute cadence, this
    # gives every lead multiple real chances to be caught each morning.
    return TARGET_LOCAL_HOUR <= local_now.hour < SEND_WINDOW_END_HOUR


def get_box1_leads():
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
                {"property": "Contact Status", "select": {"does_not_equal": "Lead Lost"}},
            ]
        }
    }
    leads = []
    cursor = None
    has_more = True
    while has_more:
        if cursor:
            body["start_cursor"] = cursor
        resp = requests.post(url, headers=headers, json=body)
        if resp.status_code != 200:
            print(f"Notion query error: {resp.status_code} {resp.text[:200]}")
            break
        data = resp.json()
        for page in data.get("results", []):
            props = page.get("properties", {})
            leads.append({
                "page_id": page["id"],
                "name": _get_title(props, "Business Name"),
                "email": _get_email(props, "Email"),
                "contact_status": _get_select(props, "Contact Status"),
                "reply_status": _get_select(props, "Reply Status"),
                "last_contact_date": _get_date(props, "Last Contact Date"),
                "emails_sent": _get_number(props, "Emails Sent") or 0,
                "email_draft": _get_rich_text(props, "Email Draft"),
                "email_subject": _get_rich_text(props, "Email Subject"),
                "thread_message_id": _get_rich_text(props, "Thread Message ID"),
                "timezone": _get_select(props, "Timezone"),
                "ghost_site_status": _get_select(props, "Ghost Site Status"),
            })
        has_more = data.get("has_more", False)
        cursor = data.get("next_cursor")
    return leads


def _get_title(props, key):
    t = props.get(key, {}).get("title", [])
    return t[0]["text"]["content"] if t else ""

def _get_rich_text(props, key):
    t = props.get(key, {}).get("rich_text", [])
    return t[0]["text"]["content"] if t else ""

def _get_email(props, key):
    return props.get(key, {}).get("email")

def _get_select(props, key):
    s = props.get(key, {}).get("select")
    return s["name"] if s else None

def _get_date(props, key):
    d = props.get(key, {}).get("date")
    if d and d.get("start"):
        return datetime.fromisoformat(d["start"].replace("Z", "+00:00")).date()
    return None

def _get_number(props, key):
    return props.get(key, {}).get("number")


def days_since(last_contact_date):
    if last_contact_date is None:
        return None
    return (date.today() - last_contact_date).days


def send_email(to_address, subject, body, message_id=None, in_reply_to=None):
    headers = f"From: {FROM_ADDRESS}\nTo: {to_address}\nSubject: {subject}\n"
    if message_id:
        headers += f"Message-ID: {message_id}\n"
    if in_reply_to:
        headers += f"In-Reply-To: {in_reply_to}\nReferences: {in_reply_to}\n"
    message = headers + f"\n{body}\n"
    result = subprocess.run(
        ["himalaya", "message", "send"],
        input=message,
        text=True,
        capture_output=True,
    )
    return result.returncode == 0, result.stderr


def update_notion_after_send(page_id, next_status, emails_sent_count, thread_message_id=None):
    url = f"https://api.notion.com/v1/pages/{page_id}"
    headers = {
        "Authorization": f"Bearer {NOTION_API_KEY}",
        "Notion-Version": "2025-09-03",
        "Content-Type": "application/json",
    }
    properties = {
        "Contact Status": {"select": {"name": next_status}},
        "Last Contact Date": {"date": {"start": date.today().isoformat()}},
        "Emails Sent": {"number": emails_sent_count + 1},
    }
    if thread_message_id:
        properties["Thread Message ID"] = {"rich_text": [{"text": {"content": thread_message_id}}]}
    resp = requests.patch(url, headers=headers, json={"properties": properties})
    return resp.status_code == 200


def count_todays_new_sends():
    """Duplicated from orchestrator.py's identical function on purpose —
    this module has no dependency on orchestrator.py today, and importing
    it just for this one query would pull in its argv-parsing (LIVE =
    hp_env.live_allowed("--live" in sys.argv)) as a side effect wherever
    cold_email.py gets imported (e.g. dashboard/app.py, for the preview
    below)."""
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
        return 0
    return len(resp.json().get("results", []))


def next_local_send_window(timezone_abbr):
    """Next moment this timezone's local clock hits TARGET_LOCAL_HOUR —
    for the dashboard's "waiting until" display. None for an unresolvable
    timezone, same as is_local_send_time()'s "don't know, so no" stance."""
    if not timezone_abbr or timezone_abbr not in TIMEZONE_TO_IANA:
        return None
    tz = ZoneInfo(TIMEZONE_TO_IANA[timezone_abbr])
    now_local = datetime.now(tz)
    candidate = now_local.replace(hour=TARGET_LOCAL_HOUR, minute=0, second=0, microsecond=0)
    if candidate <= now_local:
        candidate += timedelta(days=1)
    return candidate


def preview_upcoming_sends():
    """Read-only preview of what the *next* cold_email.py run would do —
    no sending, no Notion writes. Mirrors main()'s eligibility order
    exactly (skip conditions, then quota gate for first-touch leads, then
    due-date gate for follow-ups, then the per-lead-timezone local-morning
    gate) so the counts here are the same ones a real run would produce.
    Built for the dashboard's "what's about to happen" panel (2026-08-17).
    """
    max_new_sends = max(0, DAILY_SEND_LIMIT - count_todays_new_sends())
    leads = get_box1_leads()
    leads.sort(key=lambda lead: STATE_TABLE.get(lead["contact_status"] or "Not Contacted", {"followup_num": -1})["followup_num"] == 0)

    stages = {}
    new_sent_count = 0

    for lead in leads:
        if not lead["email"]:
            continue
        if lead["ghost_site_status"]:
            continue
        if lead["reply_status"] in STOP_REPLY_STATUSES:
            continue

        status = lead["contact_status"] or "Not Contacted"
        if status not in STATE_TABLE:
            continue

        rule = STATE_TABLE[status]
        followup_num = rule["followup_num"]
        entry = stages.setdefault(status, {
            "followup_num": followup_num,
            "ready": 0,
            "waiting_for_quota": 0,
            "not_yet_due": 0,
            "waiting_timezones": [],
        })

        if followup_num == 0:
            if new_sent_count >= max_new_sends:
                entry["waiting_for_quota"] += 1
                continue
        else:
            elapsed = days_since(lead["last_contact_date"])
            if elapsed is None or elapsed < rule["delta"]:
                entry["not_yet_due"] += 1
                continue

        if is_local_send_time(lead["timezone"]):
            entry["ready"] += 1
            if followup_num == 0:
                new_sent_count += 1
        else:
            entry["waiting_timezones"].append(lead["timezone"])

    return {
        "max_new_sends": max_new_sends,
        "new_quota_remaining": max(0, max_new_sends - new_sent_count),
        "stages": stages,
    }


def main():
    mode = "LIVE — will actually send emails" if not DRY_RUN else "DRY RUN — nothing will actually send"
    print(f"=== Cold Email run: {mode} (HP_ENV={hp_env.HP_ENV}) ===\n")

    # Full stop for mailbox warmup periods (see config.yaml's comment on
    # this key) — checked before touching Notion at all, since a paused
    # run has nothing useful to report either way. Covers first-touch
    # AND follow-ups; daily_send_limit alone can't do this since
    # follow-ups are deliberately uncapped.
    if hp_config.COLD_EMAIL_PAUSED:
        print("cold_email_paused is set in config.yaml — skipping this run entirely (no sends, new or follow-up).")
        return {"notes": "paused via cold_email_paused config flag, no sends attempted"}

    leads = get_box1_leads()
    print(f"Found {len(leads)} Box 1 leads (excluding Lead Lost).\n")
    print(f"New-lead quota this run: {MAX_NEW_SENDS} (follow-ups are never capped).\n")

    # Follow-ups before fresh leads: two independent quotas, not one shared
    # count. Every due follow-up sends regardless of volume; new/first-touch
    # leads are capped at MAX_NEW_SENDS (the orchestrator computes this as
    # the day's remaining new-lead quota; standalone runs default to the
    # full DAILY_SEND_LIMIT). Sorting follow-ups first ensures they're never
    # starved by a burst of freshly-discovered leads within one run.
    leads.sort(key=lambda lead: STATE_TABLE.get(lead["contact_status"] or "Not Contacted", {"followup_num": -1})["followup_num"] == 0)

    new_sent_count = 0
    followup_sent_count = 0
    skipped_timing = 0

    for lead in leads:
        if not lead["email"]:
            continue

        if lead["ghost_site_status"]:
            # Track B stub (Sprint 15+ not built yet): any lead already in the
            # Ghost Site sequence is owned by that sequence instead — skip
            # entirely, not just for follow-ups, so Sprint 7 never needs
            # revisiting once Track B exists.
            continue

        if lead["reply_status"] in STOP_REPLY_STATUSES:
            continue

        status = lead["contact_status"] or "Not Contacted"
        if status not in STATE_TABLE:
            continue

        rule = STATE_TABLE[status]
        followup_num = rule["followup_num"]

        if followup_num == 0:
            if new_sent_count >= MAX_NEW_SENDS:
                continue
        else:
            elapsed = days_since(lead["last_contact_date"])
            if elapsed is None or elapsed < rule["delta"]:
                continue

        if not is_local_send_time(lead["timezone"]):
            skipped_timing += 1
            continue

        new_message_id = None
        in_reply_to = None

        if followup_num == 0:
            subject = lead["email_subject"] or f"Quick question about {lead['name']}'s website"
            body = lead["email_draft"]
            if not body:
                print(f"  SKIP {lead['name']}: no Email Draft found, can't send first email.")
                continue
            # Email Draft is generated once at discovery time, before the
            # lead's Notion page (and therefore its page ID) exists — the
            # stored draft still has a literal [TRACKING_LINK] token in it,
            # rendered here instead, now that the page ID is known.
            body = hp_template.render(body, tracking_link=hp_template.tracking_link(lead["page_id"]))
            new_message_id = make_msgid()
        else:
            original_subject = lead["email_subject"] or f"{lead['name']}'s website"
            subject = f"Re: {original_subject}"
            body = hp_template.render(load_followup_text(followup_num), tracking_link=hp_template.tracking_link(lead["page_id"]))
            in_reply_to = lead["thread_message_id"] or None
            if not in_reply_to:
                print(f"  WARNING {lead['name']}: no stored Thread Message ID, follow-up may not thread correctly.")

        label = "first cold email" if followup_num == 0 else f"follow-up #{followup_num}"
        tz_note = f" [{lead['timezone']}, local morning send window]" if lead["timezone"] else " [no timezone set]"
        running_count = new_sent_count + 1 if followup_num == 0 else followup_sent_count + 1
        print(f"  [{running_count}] {lead['name']} <{lead['email']}> -> {label}{tz_note} -> new status: {rule['next']}")

        if DRY_RUN:
            print(f"      Subject: {subject}")
            print(f"      Body preview: {body[:150]}...")
            if new_message_id:
                print(f"      Would generate Message-ID: {new_message_id}")
            if in_reply_to:
                print(f"      Would thread via In-Reply-To: {in_reply_to}")
        else:
            recipient = hp_env.resolve_recipient(lead["email"])
            success, err = send_email(recipient, subject, body, message_id=new_message_id, in_reply_to=in_reply_to)
            if not success:
                print(f"      SEND FAILED: {err[:200]}")
                continue
            updated = update_notion_after_send(
                lead["page_id"], rule["next"], lead["emails_sent"],
                thread_message_id=new_message_id
            )
            if not updated:
                print(f"      WARNING: email sent but Notion update failed for {lead['name']}")

        if followup_num == 0:
            new_sent_count += 1
        else:
            followup_sent_count += 1

    print(f"\n--- Summary ---")
    verb = "Would send" if DRY_RUN else "Sent"
    print(f"{verb} (new leads): {new_sent_count}/{MAX_NEW_SENDS}")
    print(f"{verb} (follow-ups, uncapped): {followup_sent_count}")
    print(f"Skipped (not their local 8am yet): {skipped_timing}")
    if DRY_RUN:
        print("This was a DRY RUN. Re-run with --live to actually send.")

    return {
        "emails_sent": new_sent_count + followup_sent_count,
        "notes": f"new={new_sent_count}/{MAX_NEW_SENDS} followups={followup_sent_count} "
                 f"skipped_timing={skipped_timing} dry_run={DRY_RUN}",
    }


if __name__ == "__main__":
    import hp_runlog
    hp_runlog.run_wrapped("cold_email", main)
