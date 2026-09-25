#!/usr/bin/env python3
"""
Preview Sequence Sender — SiteScout (Sprint 18, Track B)
Usage:
  python3 ghost_site_sequence.py            (dry-run)
  python3 ghost_site_sequence.py --live     (actually sends)

Called from orchestrator.py's hourly chain (2026-08-17 fix — this script
had no scheduling anywhere at all before then, only a manual dashboard
trigger). Self-gated to once per calendar day per lead via a new "Last
Sequence Action Date" field, same shape as check_opens_reminder.py's own
once-daily gate — advance_sequence()/start_sequence() unconditionally
increment Sequence Day by 1 with no gate of their own, so calling this
hourly without one would run a lead through its whole 7-day sequence in
7 hours instead of 7 days.

Two things happen here, per lead:

1. A lead sitting at Ghost Site Status "Selected" with a Ghost Site URL
   filled in is a brand-new Track B lead ready to start — sends Day 1
   (threaded into the original cold-email thread), sets Sequence Day = 1,
   flips status to "Live", and sets Site Expiry Date = today + 7.
   Sprint 17 (the Cloudflare/wrangler builder automation) would normally
   own this transition the moment it deploys a site; it isn't built yet,
   so this script owns it for now (Cyril's call, 2026-08-12) — same
   trigger condition either way (Selected + URL present), so nothing
   here needs to change once Sprint 17 exists.
2. A lead already at "Live" is mid-sequence — increments Sequence Day and
   sends that day's template if one exists (ghost_day1/4/5/6/7.md; days 2
   and 3 have no template, so they increment silently with no send).

Any reply during the sequence is handled by inbox_monitoring.py (Sprint
6), not here — it sets Ghost Site Status to "Replied", which removes the
lead from this script's query on the next run.

Lead-facing sends (unlike Queue Check/notifications/Daily Reporting,
which are internal ops emails), so --live/DRY_RUN gating and
hp_env.resolve_recipient() apply exactly like cold_email.py.
"""

import os
import subprocess
import sys
from datetime import date, timedelta

import requests

import hp_env
import hp_template

NOTION_API_KEY = hp_env.NOTION_API_KEY
NOTION_DATA_SOURCE_ID = hp_env.NOTION_DATA_SOURCE_ID
FROM_ADDRESS = "operator@example.com"

TEMPLATES_DIR = os.path.join(hp_env.PROJECT_ROOT, "ghost_templates")
SITE_LIFESPAN_DAYS = 7

DRY_RUN = not hp_env.live_allowed("--live" in sys.argv)


def load_template(day):
    path = os.path.join(TEMPLATES_DIR, f"ghost_day{day}.md")
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return f.read()


def format_template(text, business_name, url, expiry_date):
    return hp_template.render(text, business_name=business_name, url=url, expiry_date=expiry_date.strftime("%B %-d"))


def get_active_leads():
    url = f"https://api.notion.com/v1/data_sources/{NOTION_DATA_SOURCE_ID}/query"
    headers = {
        "Authorization": f"Bearer {NOTION_API_KEY}",
        "Notion-Version": "2025-09-03",
        "Content-Type": "application/json",
    }
    body = {
        "filter": {
            "or": [
                {"property": "Ghost Site Status", "select": {"equals": "Selected"}},
                {"property": "Ghost Site Status", "select": {"equals": "Live"}},
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
                "ghost_site_status": _get_select(props, "Ghost Site Status"),
                "ghost_site_url": props.get("Ghost Site URL", {}).get("url"),
                "sequence_day": _get_number(props, "Sequence Day") or 0,
                "site_expiry_date": _get_date(props, "Site Expiry Date"),
                "thread_message_id": _get_rich_text(props, "Thread Message ID"),
                "email_subject": _get_rich_text(props, "Email Subject"),
                "last_sequence_action_date": _get_date(props, "Last Sequence Action Date"),
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

def _get_number(props, key):
    return props.get(key, {}).get("number")

def _get_date(props, key):
    d = props.get(key, {}).get("date")
    if d and d.get("start"):
        return date.fromisoformat(d["start"][:10])
    return None


def send_email(to_address, subject, body, in_reply_to=None):
    headers = f"From: {FROM_ADDRESS}\nTo: {to_address}\nSubject: {subject}\n"
    if in_reply_to:
        headers += f"In-Reply-To: {in_reply_to}\nReferences: {in_reply_to}\n"
    message = headers + f"\n{body}\n"
    result = subprocess.run(
        ["himalaya", "message", "send"],
        input=message, text=True, capture_output=True,
    )
    return result.returncode == 0, result.stderr


def start_sequence(page_id, subject, body, in_reply_to, recipient):
    """New Selected+URL lead: send Day 1, flip Live, set Sequence Day=1
    and Site Expiry Date=today+7."""
    if DRY_RUN:
        print(f"      [DRY RUN] Would send Day 1 and set status Live, expiry {date.today() + timedelta(days=SITE_LIFESPAN_DAYS)}")
        return True
    success, err = send_email(recipient, subject, body, in_reply_to=in_reply_to)
    if not success:
        print(f"      SEND FAILED: {err[:200]}")
        return False
    url = f"https://api.notion.com/v1/pages/{page_id}"
    headers = {
        "Authorization": f"Bearer {NOTION_API_KEY}",
        "Notion-Version": "2025-09-03",
        "Content-Type": "application/json",
    }
    expiry = date.today() + timedelta(days=SITE_LIFESPAN_DAYS)
    properties = {
        "Ghost Site Status": {"select": {"name": "Live"}},
        "Sequence Day": {"number": 1},
        "Site Expiry Date": {"date": {"start": expiry.isoformat()}},
        "Preview Sent Date": {"date": {"start": date.today().isoformat()}},
        "Last Sequence Action Date": {"date": {"start": date.today().isoformat()}},
    }
    resp = requests.patch(url, headers=headers, json={"properties": properties})
    return resp.status_code == 200


def advance_sequence(page_id, new_day, subject, body, in_reply_to, recipient, sent_today):
    """Mid-sequence lead: increment Sequence Day, send if a template
    exists for the new day."""
    url = f"https://api.notion.com/v1/pages/{page_id}"
    headers = {
        "Authorization": f"Bearer {NOTION_API_KEY}",
        "Notion-Version": "2025-09-03",
        "Content-Type": "application/json",
    }
    properties = {
        "Sequence Day": {"number": new_day},
        "Last Sequence Action Date": {"date": {"start": date.today().isoformat()}},
    }

    if sent_today:
        if DRY_RUN:
            print(f"      [DRY RUN] Would send Day {new_day}")
        else:
            success, err = send_email(recipient, subject, body, in_reply_to=in_reply_to)
            if not success:
                print(f"      SEND FAILED: {err[:200]}")
                return False

    if not DRY_RUN:
        resp = requests.patch(url, headers=headers, json={"properties": properties})
        return resp.status_code == 200
    return True


def main():
    mode = "LIVE — will actually send emails" if not DRY_RUN else "DRY RUN — nothing will actually send"
    print(f"=== Ghost Site Sequence run: {mode} (HP_ENV={hp_env.HP_ENV}) ===\n")

    leads = get_active_leads()
    print(f"Found {len(leads)} active Track B leads (Selected or Live).\n")

    started, advanced, emailed = 0, 0, 0

    for lead in leads:
        if not lead["email"]:
            continue

        # Safe to call hourly (orchestrator.py, 2026-08-17) precisely
        # because of this check — start_sequence()/advance_sequence()
        # have no gate of their own, they'd otherwise run a lead through
        # its whole 7-day sequence in 7 hours instead of 7 days.
        if lead["last_sequence_action_date"] == date.today():
            continue

        original_subject = lead["email_subject"] or f"{lead['name']}'s website"
        subject = f"Re: {original_subject}"
        recipient = hp_env.resolve_recipient(lead["email"])
        in_reply_to = lead["thread_message_id"] or None

        if lead["ghost_site_status"] == "Selected":
            if not lead["ghost_site_url"]:
                continue  # not ready yet — no URL to show
            body = format_template(
                load_template(1), lead["name"], lead["ghost_site_url"],
                date.today() + timedelta(days=SITE_LIFESPAN_DAYS),
            )
            ok = start_sequence(lead["page_id"], subject, body, in_reply_to, recipient)
            print(f"  {lead['name']} -> starting sequence, Day 1 ({'OK' if ok else 'FAILED'})")
            started += 1
            emailed += 1
            continue

        if lead["ghost_site_status"] != "Live":
            # Defensive: get_active_leads() already filters to
            # Selected/Live, but don't blindly advance a lead whose status
            # changed between the query and now (e.g. a reply just set it
            # to Replied) — only ever act on a status checked right here.
            continue

        # Mid-sequence.
        new_day = lead["sequence_day"] + 1
        template_text = load_template(new_day)
        sent_today = template_text is not None
        body = None
        if sent_today:
            # The real stored expiry date, not a freshly recomputed one —
            # it was fixed on Day 1 and must never drift across the sequence.
            expiry = lead["site_expiry_date"] or (date.today() + timedelta(days=SITE_LIFESPAN_DAYS - new_day))
            body = format_template(template_text, lead["name"], lead["ghost_site_url"], expiry)

        ok = advance_sequence(lead["page_id"], new_day, subject, body, in_reply_to, recipient, sent_today)
        label = f"Day {new_day} sent" if sent_today else f"Day {new_day}, no template, incrementing only"
        print(f"  {lead['name']} -> {label} ({'OK' if ok else 'FAILED'})")
        advanced += 1
        if sent_today:
            emailed += 1

    print("\n--- Summary ---")
    print(f"Sequences started (Day 1): {started}")
    print(f"Sequences advanced: {advanced}")
    print(f"Emails {'would be ' if DRY_RUN else ''}sent: {emailed}")
    if DRY_RUN:
        print("This was a DRY RUN. Re-run with --live to actually send.")

    return {"emails_sent": emailed, "notes": f"started={started} advanced={advanced} dry_run={DRY_RUN}"}


if __name__ == "__main__":
    import hp_runlog
    hp_runlog.run_wrapped("ghost_site_sequence", main)
