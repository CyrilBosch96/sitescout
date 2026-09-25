#!/usr/bin/env python3
"""
Inbox Monitoring — SiteScout
Usage: python3 inbox_monitoring.py

Reads inbox replies, matches them to CRM leads by sender email, classifies
intent (Interested / Not Interested / Unsubscribed), updates Notion.
Ambiguous replies default to Interested (safer to over-flag than miss one).
"""

import json
import os
import re
import subprocess
from datetime import date

import requests

import hp_env
import hp_config
import hp_llm

NOTION_API_KEY = hp_env.NOTION_API_KEY
NOTION_DATA_SOURCE_ID = hp_env.NOTION_DATA_SOURCE_ID

PROCESSED_FILE = os.path.join(hp_env.PROJECT_ROOT, "processed_replies.json")


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


def extract_reply_text(raw_output):
    parts = raw_output.split("\n\n", 1)
    body = parts[1] if len(parts) > 1 else raw_output

    # himalaya's `message read` prints a MIME part marker line ("[2]
    # text/plain (192 B)") plus its own indented Content-Type /
    # Content-Transfer-Encoding lines before the actual body, separated
    # from it by another blank line. Found live 2026-08-27 (queue_check.py
    # rejected a real reply as "unparseable" for exactly this reason):
    # every real inbound reply this whole pipeline has ever classified
    # actually had this marker line prepended to the text handed to
    # classify_reply(). Skip past it if present.
    body_lines = body.split("\n")
    if body_lines and re.match(r"^\[\d+\]\s+\S+/\S+", body_lines[0]):
        idx = 1
        while idx < len(body_lines) and body_lines[idx].strip():
            idx += 1
        body = "\n".join(body_lines[idx + 1:])

    lines = []
    for line in body.split("\n"):
        stripped = line.strip()
        if stripped.startswith(">"):
            break
        if re.match(r"^On .+ wrote:$", stripped):
            break
        lines.append(line)
    return "\n".join(lines).strip()


def read_message_body(message_id):
    result = subprocess.run(
        ["himalaya", "message", "read", str(message_id)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return None
    return extract_reply_text(result.stdout)


def find_lead_by_email(email):
    """Returns {"page_id": ..., "name": ..., "ghost_site_status": ...} for
    the lead with this email, or None."""
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
    ghost_status = props.get("Ghost Site Status", {}).get("select")
    return {
        "page_id": page["id"], "name": name,
        "ghost_site_status": ghost_status["name"] if ghost_status else None,
    }


def classify_reply(body_text):
    """LLM call (hp_llm, Gemini) — single-word classification, defaults to Interested."""
    prompt = f"""Classify this email reply into EXACTLY ONE of these three words:
INTERESTED, NOT_INTERESTED, or UNSUBSCRIBED.

- UNSUBSCRIBED: they explicitly asked to be removed/unsubscribed/stop emailing them.
- NOT_INTERESTED: they clearly declined or said no, without asking to unsubscribe.
- INTERESTED: anything else — including questions, requests for more info, or
  genuine interest. If unsure, choose INTERESTED.

Reply text:
\"\"\"{body_text}\"\"\"

Output ONLY one of the three words above. Nothing else."""

    raw = hp_llm.generate(prompt, timeout=60)
    if raw is not None:
        raw = raw.strip().upper()
        if "UNSUBSCRIB" in raw:
            return "Unsubscribed"
        if "NOT_INTERESTED" in raw or "NOT INTERESTED" in raw:
            return "Not Interested"
        return "Interested"
    return "Interested"  # default per AC4


NOTIFY_ADDRESS = hp_config.OPERATOR_EMAIL


def send_notification(business_name, reply_text):
    """Immediate flagged alert for a positive reply. Gated on HP_ENV==production
    (same safety pattern as cold_email.py's DRY_RUN) — dev/test runs against the
    synthetic TEST lead print instead of firing a real email."""
    subject = f"INTERESTED REPLY: {business_name}"
    body = f'{business_name} replied and was classified as Interested:\n\n"""\n{reply_text}\n"""'

    if not hp_env.IS_PRODUCTION:
        print(f"    WOULD NOTIFY: {subject}")
        return True

    message = f"From: {NOTIFY_ADDRESS}\nTo: {NOTIFY_ADDRESS}\nSubject: {subject}\n\n{body}\n"
    result = subprocess.run(
        ["himalaya", "message", "send"],
        input=message, text=True, capture_output=True,
    )
    if result.returncode != 0:
        print(f"    Notification send failed: {result.stderr[:200]}")
    return result.returncode == 0


TRACK_B_ACTIVE_STATUSES = {"Selected", "Live"}


def mark_ghost_site_replied(page_id):
    url = f"https://api.notion.com/v1/pages/{page_id}"
    headers = {
        "Authorization": f"Bearer {NOTION_API_KEY}",
        "Notion-Version": "2025-09-03",
        "Content-Type": "application/json",
    }
    body = {"properties": {"Ghost Site Status": {"select": {"name": "Replied"}}}}
    resp = requests.patch(url, headers=headers, json=body)
    return resp.status_code == 200


def resurrect_ghost_site(page_id):
    """Reply to a resurrection email (Sprint 19): auto-rebuild rather than
    just notify (Cyril's call, 2026-08-14) — flips status back to
    Selected so ghost_site_builder.py's existing Selected+no-URL pipeline
    picks it up naturally, no new rebuild logic needed. Clears
    Resurrection Sent Date so a future expiry cycle can send another one
    if this same lead lapses again."""
    url = f"https://api.notion.com/v1/pages/{page_id}"
    headers = {
        "Authorization": f"Bearer {NOTION_API_KEY}",
        "Notion-Version": "2025-09-03",
        "Content-Type": "application/json",
    }
    body = {"properties": {
        "Ghost Site Status": {"select": {"name": "Selected"}},
        "Resurrection Sent Date": {"date": None},
    }}
    resp = requests.patch(url, headers=headers, json=body)
    return resp.status_code == 200


def send_resurrection_reply_notification(business_name, reply_text):
    subject = f"GHOST SITE RESURRECTED: {business_name}"
    body = (
        f"{business_name} replied to their resurrection email and their site is being rebuilt:\n\n"
        f'"""\n{reply_text}\n"""'
    )

    if not hp_env.IS_PRODUCTION:
        print(f"    WOULD NOTIFY (resurrection): {subject}")
        return True

    message = f"From: {NOTIFY_ADDRESS}\nTo: {NOTIFY_ADDRESS}\nSubject: {subject}\n\n{body}\n"
    result = subprocess.run(
        ["himalaya", "message", "send"],
        input=message, text=True, capture_output=True,
    )
    if result.returncode != 0:
        print(f"    Resurrection notification failed: {result.stderr[:200]}")
    return result.returncode == 0


def send_ghost_site_reply_notification(business_name, reply_text):
    """Any reply during an active Track B sequence gets flagged immediately,
    regardless of Qwen's Interested/Not Interested/Unsubscribed classification
    (Cyril's call, 2026-08-12) — matches his stated 5-minute reply SLA for
    ghost site leads specifically, unlike Track A's Interested-only gate."""
    subject = f"GHOST SITE REPLY: {business_name}"
    body = f'{business_name} replied during their ghost site sequence:\n\n"""\n{reply_text}\n"""'

    if not hp_env.IS_PRODUCTION:
        print(f"    WOULD NOTIFY (ghost site reply): {subject}")
        return True

    message = f"From: {NOTIFY_ADDRESS}\nTo: {NOTIFY_ADDRESS}\nSubject: {subject}\n\n{body}\n"
    result = subprocess.run(
        ["himalaya", "message", "send"],
        input=message, text=True, capture_output=True,
    )
    if result.returncode != 0:
        print(f"    Ghost site reply notification failed: {result.stderr[:200]}")
    return result.returncode == 0


def update_reply_status(page_id, status):
    url = f"https://api.notion.com/v1/pages/{page_id}"
    headers = {
        "Authorization": f"Bearer {NOTION_API_KEY}",
        "Notion-Version": "2025-09-03",
        "Content-Type": "application/json",
    }
    body = {
        "properties": {
            "Reply Status": {"select": {"name": status}},
            # Sprint 9's daily report scopes "replies needing review" to a
            # specific day — Reply Status alone is just a snapshot value
            # with no timestamp, so this is what makes that filtering possible.
            "Reply Received Date": {"date": {"start": date.today().isoformat()}},
        }
    }
    resp = requests.patch(url, headers=headers, json=body)
    return resp.status_code == 200


def main():
    processed = load_processed_ids()
    envelopes = list_inbox()
    print(f"Checked inbox: {len(envelopes)} messages total, {len(processed)} already processed.")

    new_count = 0
    for env in envelopes:
        # local_id is himalaya's own envelope id — an IMAP UID, scoped to
        # whichever folder it's currently listed in. dedup_key is the RFC
        # Message-ID header instead: local_id is what we need to actually
        # fetch the message body, but it is NOT stable dedup identity — a
        # thread that Gmail auto-restores from Archive back to Inbox (which
        # it does the moment any new message lands in that same thread) gets
        # re-added to INBOX under a fresh UID, making an already-handled
        # reply look brand new forever. Found live (2026-08-26): a stale
        # Track B resurrection reply kept re-triggering resurrect_ghost_site()
        # and a fresh notification every time the notification itself (which
        # lands in the same thread) caused Gmail to restore the thread,
        # looping indefinitely. Message-ID is assigned once by the sending
        # server and never changes across folder moves, so it's the only
        # safe dedup key here.
        local_id = str(env.get("id"))
        dedup_key = env.get("message-id") or local_id
        if dedup_key in processed:
            continue

        # Saved after every message, not once at the end (regression, found
        # 2026-08-18: a resurrection notification went out 6 times for the
        # same reply). processed_replies.json used to be written only once
        # after the whole loop finished — a crash on ANY message, including
        # one later in the same batch, meant nothing from that run got
        # persisted, so already-handled messages (already resurrected,
        # already notified) got fully reprocessed on the next run. Same
        # per-message-save pattern queue_check.py already uses for exactly
        # this reason.
        from_list = env.get("from", [])
        if not from_list:
            processed.add(dedup_key)
            save_processed_ids(processed)
            continue
        sender_email = from_list[0].get("email", "")

        lead = find_lead_by_email(sender_email)
        if not lead:
            processed.add(dedup_key)
            save_processed_ids(processed)
            continue  # not from a known lead, skip

        body = read_message_body(local_id)
        if not body:
            processed.add(dedup_key)
            save_processed_ids(processed)
            continue

        status = classify_reply(body)
        success = update_reply_status(lead["page_id"], status)
        print(f"  [{local_id}] {sender_email} -> {status} ({'OK' if success else 'FAILED'})")

        if lead["ghost_site_status"] == "Expired":
            resurrect_ghost_site(lead["page_id"])
            send_resurrection_reply_notification(lead["name"], body)
        elif lead["ghost_site_status"] in TRACK_B_ACTIVE_STATUSES:
            mark_ghost_site_replied(lead["page_id"])
            send_ghost_site_reply_notification(lead["name"], body)
        elif status == "Interested":
            send_notification(lead["name"], body)

        processed.add(dedup_key)
        save_processed_ids(processed)
        new_count += 1

    print(f"\nProcessed {new_count} new replies from known leads.")

    return {"notes": f"{len(envelopes)} inbox messages checked, {new_count} new replies processed"}


if __name__ == "__main__":
    import hp_runlog
    hp_runlog.run_wrapped("inbox_monitoring", main)
