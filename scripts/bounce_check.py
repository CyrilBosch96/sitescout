#!/usr/bin/env python3
"""
Bounce Check — SiteScout
Usage: python3 bounce_check.py

Reads Gmail's own "Delivery Status Notification (Failure)" bounce emails
(from mailer-daemon@googlemail.com) and, for each one that maps to a real
CRM lead, marks that lead Lead Lost and notifies Cyril — instead of the
lead silently continuing through the entire follow-up cadence as if every
email had actually been delivered.

Found live 2026-08-27, during QA of the Track A/B flow: a real follow-up to
Example Barber Shop genuinely bounced (their mail server timed out),
but cold_email.py has no way to know that — it advances Contact Status the
moment send_email() returns success from the SMTP handoff to Gmail, which
says nothing about whether the recipient's server ever actually accepted
it. Cyril would only ever find out by manually checking Gmail's own bounce
notices himself.

Deliberately scoped to permanent failures only ("Delivery Status
Notification (Failure)"), not "(Delay)" — a delay is not a bounce, the
message may still eventually deliver, and reacting to it would be a false
positive. "(Delay)" notifications are left alone entirely (not even
dedup-tracked) rather than guessed at.
"""

import json
import os
import re
import subprocess

import requests

import hp_env
import hp_config

NOTION_API_KEY = hp_env.NOTION_API_KEY
NOTION_DATA_SOURCE_ID = hp_env.NOTION_DATA_SOURCE_ID
NOTIFY_ADDRESS = hp_config.OPERATOR_EMAIL

PROCESSED_FILE = os.path.join(hp_env.PROJECT_ROOT, "processed_bounces.json")

BOUNCE_SENDER = "mailer-daemon@googlemail.com"
BOUNCE_SUBJECT_MARKER = "Delivery Status Notification (Failure)"

# Matches Gmail's own bounce-notice wording: "...delivering your message to
# info@examplebarbershop.test. See the technical details..." — the
# trailing ". " (period+space) naturally isn't consumed since the email
# pattern requires a word character immediately after each internal dot.
RECIPIENT_RE = re.compile(r"delivering your message to ([\w.+-]+@[\w-]+(?:\.[\w-]+)+)")


def load_processed_ids():
    if os.path.exists(PROCESSED_FILE):
        with open(PROCESSED_FILE) as f:
            return set(json.load(f))
    return set()


def save_processed_ids(ids):
    os.makedirs(os.path.dirname(PROCESSED_FILE), exist_ok=True)
    with open(PROCESSED_FILE, "w") as f:
        json.dump(list(ids), f, indent=2)


def list_inbox():
    result = subprocess.run(
        ["himalaya", "envelope", "list", "--json"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"Error listing inbox: {result.stderr}")
        return []
    try:
        data = json.loads(result.stdout)
        return data.get("envelopes", [])
    except Exception as e:
        print(f"Could not parse inbox listing: {e}")
        return []


def read_message_raw(message_id):
    result = subprocess.run(
        ["himalaya", "message", "read", str(message_id)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout


def extract_bounced_recipient(raw_output):
    """Gmail's own bounce body — no MIME-marker-line stripping needed here
    (unlike inbox_monitoring.py/queue_check.py's reply parsing) since we
    search the whole raw text for the fixed wording rather than reading
    "the first line" of a stripped body."""
    match = RECIPIENT_RE.search(raw_output or "")
    return match.group(1) if match else None


def find_lead_by_email(email):
    """Returns {"page_id": ..., "name": ..., "contact_status": ...} for the
    lead with this email, or None."""
    url = f"https://api.notion.com/v1/data_sources/{NOTION_DATA_SOURCE_ID}/query"
    headers = {
        "Authorization": f"Bearer {NOTION_API_KEY}",
        "Notion-Version": "2025-09-03",
        "Content-Type": "application/json",
    }
    body = {"filter": {"property": "Email", "email": {"equals": email}}}
    resp = requests.post(url, headers=headers, json=body)
    if resp.status_code != 200:
        return None
    results = resp.json().get("results", [])
    if not results:
        return None
    page = results[0]
    props = page.get("properties", {})
    title = props.get("Business Name", {}).get("title", [])
    name = title[0]["text"]["content"] if title else "Unknown"
    contact_status = props.get("Contact Status", {}).get("select")
    return {
        "page_id": page["id"], "name": name,
        "contact_status": contact_status["name"] if contact_status else None,
    }


def mark_lead_lost(page_id):
    url = f"https://api.notion.com/v1/pages/{page_id}"
    headers = {
        "Authorization": f"Bearer {NOTION_API_KEY}",
        "Notion-Version": "2025-09-03",
        "Content-Type": "application/json",
    }
    body = {"properties": {"Contact Status": {"select": {"name": "Lead Lost"}}}}
    resp = requests.patch(url, headers=headers, json=body)
    return resp.status_code == 200


def send_bounce_notification(business_name, bounced_email):
    subject = f"EMAIL BOUNCED: {business_name}"
    body = (
        f"A real email to {business_name} <{bounced_email}> bounced (permanent "
        f"delivery failure) and this lead has been marked Lead Lost — it won't "
        f"receive any further follow-ups.\n\n"
        f"If this looks wrong (e.g. a typo'd address that's actually fixable), "
        f"you can manually reset its Contact Status in Notion."
    )

    if not hp_env.IS_PRODUCTION:
        print(f"    WOULD NOTIFY (bounce): {subject}")
        return True

    message = f"From: {NOTIFY_ADDRESS}\nTo: {NOTIFY_ADDRESS}\nSubject: {subject}\n\n{body}\n"
    result = subprocess.run(
        ["himalaya", "message", "send"],
        input=message, text=True, capture_output=True,
    )
    if result.returncode != 0:
        print(f"    Bounce notification failed: {result.stderr[:200]}")
    return result.returncode == 0


def main():
    processed = load_processed_ids()
    envelopes = list_inbox()
    print(f"Checked inbox: {len(envelopes)} messages total, {len(processed)} already processed.")

    new_count = 0
    for env in envelopes:
        sender_email = (env.get("from") or [{}])[0].get("email", "")
        subject = env.get("subject") or ""
        if sender_email != BOUNCE_SENDER or BOUNCE_SUBJECT_MARKER not in subject:
            continue

        # Same fragility this project already fixed once in
        # inbox_monitoring.py (2026-08-26): himalaya's envelope "id" is an
        # IMAP UID scoped to whatever folder currently lists the message,
        # not stable identity. Message-ID is assigned once by the sending
        # server and never changes across folder moves.
        local_id = str(env.get("id"))
        dedup_key = env.get("message-id") or local_id
        if dedup_key in processed:
            continue

        raw = read_message_raw(local_id)
        processed.add(dedup_key)
        save_processed_ids(processed)
        if not raw:
            continue

        bounced_email = extract_bounced_recipient(raw)
        if not bounced_email:
            print(f"  [{local_id}] bounce notice found but couldn't extract the failed recipient.")
            continue

        lead = find_lead_by_email(bounced_email)
        if not lead:
            print(f"  [{local_id}] bounce for {bounced_email} — not a known lead, skipping.")
            continue

        if lead["contact_status"] == "Lead Lost":
            print(f"  [{local_id}] {lead['name']} <{bounced_email}> already Lead Lost, skipping.")
            continue

        success = mark_lead_lost(lead["page_id"])
        notified = send_bounce_notification(lead["name"], bounced_email)
        print(f"  [{local_id}] {lead['name']} <{bounced_email}> -> Lead Lost "
              f"({'OK' if success else 'FAILED'}, notified {'OK' if notified else 'FAILED'})")
        new_count += 1

    print(f"\nProcessed {new_count} new bounce(s) affecting known leads.")
    return {"notes": f"{len(envelopes)} inbox messages checked, {new_count} new bounce(s) processed"}


if __name__ == "__main__":
    import hp_runlog
    hp_runlog.run_wrapped("bounce_check", main)
