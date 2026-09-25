#!/usr/bin/env python3
"""
Ghost Site Template Request — SiteScout (Track B)
Usage: python3 ghost_template_request.py

Watches Ghost Site Status="Selected" leads, grouped by niche (never mixed
— one HTML template only fits one business type). Once a niche's count
reaches 10, emails Cyril asking for a template, re-sends that same ask
every 12 hours in the same thread until he replies with an HTML
attachment, processes the upload mechanically (see
ghost_template_processing.py — unbundle + strip scripts, no content
judgment), then asks for one confirmation reply before marking the
template approved and ready for Sprint 17 to build real sites from.

Batch semantics (Cyril's call, 2026-08-13): not frozen at exactly 10 —
whatever HTML he sends back applies to ALL currently-Selected,
not-yet-approved leads in that niche at reply time, however many that is
by then. A lead selected after the ask but before the reply still gets
included.

Confirmation step exists because Paper AI exports need real judgment to
convert safely (Sprint 16 found fabricated staff/pricing data in the
first one) — the mechanical parts (unbundling, script-stripping) are
fully automated, but nothing goes live without Cyril's explicit go-ahead
after seeing what was detected. Revisit removing this step once it's
proven reliable over a few cycles.
"""

import json
import os
import re
import subprocess
from datetime import datetime, timedelta, timezone
from email.utils import make_msgid

import requests

import hp_env
import hp_template
import hp_config
import ghost_template_processing as gtp

NOTION_API_KEY = hp_env.NOTION_API_KEY
NOTION_DATA_SOURCE_ID = hp_env.NOTION_DATA_SOURCE_ID
FROM_ADDRESS = hp_config.OPERATOR_EMAIL

STATE_FILE = os.path.join(hp_env.PROJECT_ROOT, "ghost_template_requests.json")
PROCESSED_FILE = os.path.join(hp_env.PROJECT_ROOT, "processed_ghost_template_replies.json")
TEMPLATES_DIR = os.path.join(hp_env.PROJECT_ROOT, "ghost-sites", "templates")
EMAIL_TEMPLATES_DIR = os.path.join(hp_env.PROJECT_ROOT, "email_templates")

BATCH_THRESHOLD = 10
REMINDER_INTERVAL_HOURS = 12


def load_ask_template():
    """Sprint 22: file-backed so it's editable from the dashboard's
    Content page without a code deploy."""
    with open(os.path.join(EMAIL_TEMPLATES_DIR, "template_request_ask.md")) as f:
        return f.read().rstrip("\n")


def load_reminder_template():
    with open(os.path.join(EMAIL_TEMPLATES_DIR, "template_request_reminder.md")) as f:
        return f.read().rstrip("\n")


# --- state ---

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def load_processed_ids():
    if os.path.exists(PROCESSED_FILE):
        with open(PROCESSED_FILE) as f:
            return set(json.load(f))
    return set()


def save_processed_ids(ids):
    with open(PROCESSED_FILE, "w") as f:
        json.dump(list(ids), f, indent=2)


def hours_since(iso_timestamp):
    then = datetime.fromisoformat(iso_timestamp)
    return (datetime.now(timezone.utc) - then).total_seconds() / 3600


# --- Notion: selected leads grouped by niche ---

def extract_niche(source_niche_location):
    if not source_niche_location or " / " not in source_niche_location:
        return None
    return source_niche_location.split(" / ", 1)[0].strip()


def niche_slug(niche):
    return re.sub(r"[^a-z0-9]+", "-", niche.lower()).strip("-")


def get_selected_leads_by_niche():
    url = f"https://api.notion.com/v1/data_sources/{NOTION_DATA_SOURCE_ID}/query"
    headers = {
        "Authorization": f"Bearer {NOTION_API_KEY}",
        "Notion-Version": "2025-09-03",
        "Content-Type": "application/json",
    }
    body = {"filter": {"property": "Ghost Site Status", "select": {"equals": "Selected"}}}
    by_niche = {}
    cursor = None
    has_more = True
    while has_more:
        if cursor:
            body["start_cursor"] = cursor
        resp = requests.post(url, headers=headers, json=body)
        if resp.status_code != 200:
            break
        data = resp.json()
        for page in data.get("results", []):
            props = page.get("properties", {})
            title = props.get("Business Name", {}).get("title", [])
            name = title[0]["text"]["content"] if title else "Unknown"
            loc = props.get("Source Niche/Location", {}).get("rich_text", [])
            loc_text = loc[0]["text"]["content"] if loc else ""
            niche = extract_niche(loc_text)
            if not niche:
                continue
            by_niche.setdefault(niche, []).append({"page_id": page["id"], "name": name})
        has_more = data.get("has_more", False)
        cursor = data.get("next_cursor")
    return by_niche


# --- email: send / list / reply detection / attachments ---

def send_email(to_address, subject, body, message_id=None, in_reply_to=None):
    headers = f"From: {FROM_ADDRESS}\nTo: {to_address}\nSubject: {subject}\n"
    if message_id:
        headers += f"Message-ID: {message_id}\n"
    if in_reply_to:
        headers += f"In-Reply-To: {in_reply_to}\nReferences: {in_reply_to}\n"
    message = headers + f"\n{body}\n"
    result = subprocess.run(
        ["himalaya", "message", "send"],
        input=message, text=True, capture_output=True,
    )
    return result.returncode == 0, result.stderr


def list_inbox():
    result = subprocess.run(
        ["himalaya", "envelope", "list", "--json"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return []
    try:
        return json.loads(result.stdout).get("envelopes", [])
    except Exception:
        return []


def find_reply_for_subject(envelopes, expected_subject_contains, processed_ids):
    for env in envelopes:
        msg_id = str(env.get("id"))
        if msg_id in processed_ids:
            continue
        subject = env.get("subject", "")
        if expected_subject_contains.lower() not in subject.lower() or not subject.lower().startswith("re:"):
            continue
        from_list = env.get("from", [])
        has_real_name = any(f.get("name") for f in from_list)
        if has_real_name:
            return msg_id
    return None


def list_attachments(message_id):
    result = subprocess.run(
        ["himalaya", "attachment", "list", str(message_id), "--json"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return []
    try:
        return json.loads(result.stdout).get("attachments", [])
    except Exception:
        return []


def _download_attachment_by_extension(message_id, dest_dir, extension):
    attachments = list_attachments(message_id)
    attachment = next(
        (a for a in attachments if a.get("filename", "").lower().endswith(extension)), None
    )
    if not attachment:
        return None
    os.makedirs(dest_dir, exist_ok=True)
    result = subprocess.run(
        ["himalaya", "attachment", "download", str(message_id), attachment["id"], "-d", dest_dir],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return None
    return os.path.join(dest_dir, attachment["filename"])


def download_html_attachment(message_id, dest_dir):
    return _download_attachment_by_extension(message_id, dest_dir, ".html")


def download_css_attachment(message_id, dest_dir):
    """Optional — a template may be a single HTML file with everything
    inlined, or HTML + a separate CSS file (how Paper AI actually
    exports). Returns None if no .css attachment exists; that's not an
    error, just "this template has no separate stylesheet"."""
    return _download_attachment_by_extension(message_id, dest_dir, ".css")


# --- the three phases ---

def start_new_requests(selected_by_niche, state, run_calls):
    for niche, leads in selected_by_niche.items():
        if len(leads) < BATCH_THRESHOLD or niche in state:
            continue
        subject = f"Ghost Site template needed: {niche}"
        body = hp_template.render(
            load_ask_template(),
            niche=niche, lead_count=len(leads),
            lead_names=", ".join(l["name"] for l in leads),
        )
        message_id = make_msgid()
        recipient = hp_env.resolve_recipient(FROM_ADDRESS)
        success, err = send_email(recipient, subject, body, message_id=message_id)
        run_calls.append(("new_request", niche, success))
        if success:
            now = datetime.now(timezone.utc).isoformat()
            state[niche] = {
                "status": "pending_template",
                "subject": subject,
                "thread_message_id": message_id,
                "requested_at": now,
                "last_reminded_at": now,
            }
        else:
            print(f"Failed to send template request for {niche}: {err[:200]}")


def send_reminders_where_due(state, run_calls):
    for niche, entry in state.items():
        if entry["status"] not in ("pending_template", "pending_confirmation"):
            continue
        if hours_since(entry["last_reminded_at"]) < REMINDER_INTERVAL_HOURS:
            continue
        recipient = hp_env.resolve_recipient(FROM_ADDRESS)
        success, err = send_email(
            recipient, f"Re: {entry['subject']}", load_reminder_template(),
            in_reply_to=entry["thread_message_id"],
        )
        run_calls.append(("reminder", niche, success))
        if success:
            entry["last_reminded_at"] = datetime.now(timezone.utc).isoformat()
        else:
            print(f"Failed to send reminder for {niche}: {err[:200]}")


def check_template_replies(state, envelopes, processed, run_calls):
    for niche, entry in list(state.items()):
        if entry["status"] != "pending_template":
            continue
        reply_id = find_reply_for_subject(envelopes, entry["subject"], processed)
        if not reply_id:
            continue
        processed.add(reply_id)

        html_path = download_html_attachment(reply_id, os.path.join(TEMPLATES_DIR, "_uploads"))
        if not html_path:
            recipient = hp_env.resolve_recipient(FROM_ADDRESS)
            send_email(
                recipient, f"Re: {entry['subject']}",
                "Couldn't find an HTML file attached to that reply — please resend with one HTML file attached.",
                in_reply_to=entry["thread_message_id"],
            )
            run_calls.append(("no_attachment", niche, True))
            continue

        with open(html_path) as f:
            raw_html = f.read()
        try:
            cleaned = gtp.process_template_upload(raw_html)
        except ValueError as e:
            recipient = hp_env.resolve_recipient(FROM_ADDRESS)
            send_email(
                recipient, f"Re: {entry['subject']}",
                f"Couldn't process that file: {e}. Please resend.",
                in_reply_to=entry["thread_message_id"],
            )
            run_calls.append(("process_failed", niche, True))
            continue

        slug = niche_slug(niche)
        out_dir = os.path.join(TEMPLATES_DIR, slug)
        os.makedirs(out_dir, exist_ok=True)
        out_path = os.path.join(out_dir, "index.html")
        with open(out_path, "w") as f:
            f.write(cleaned)

        css_path = None
        downloaded_css = download_css_attachment(reply_id, os.path.join(TEMPLATES_DIR, "_uploads"))
        if downloaded_css:
            css_path = os.path.join(out_dir, "style.css")
            with open(downloaded_css) as f:
                css_text = f.read()
            with open(css_path, "w") as f:
                f.write(css_text)

        data_slots = sorted(gtp.find_data_slots(cleaned))
        confirm_body = (
            f"Processed the template for {niche} and saved it to {out_path}"
            f"{' (with a separate style.css)' if css_path else ''}.\n\n"
            f"Data slots detected: {', '.join(data_slots) if data_slots else '(none found)'}\n\n"
            "Reply anything to this email to confirm and start building the sites."
        )
        recipient = hp_env.resolve_recipient(FROM_ADDRESS)
        success, err = send_email(
            recipient, f"Re: {entry['subject']}", confirm_body,
            in_reply_to=entry["thread_message_id"],
        )
        run_calls.append(("confirmation_requested", niche, success))
        if success:
            entry["status"] = "pending_confirmation"
            entry["last_reminded_at"] = datetime.now(timezone.utc).isoformat()
            entry["template_path"] = out_path
            entry["css_path"] = css_path


def check_confirmation_replies(state, envelopes, processed, run_calls):
    for niche, entry in state.items():
        if entry["status"] != "pending_confirmation":
            continue
        reply_id = find_reply_for_subject(envelopes, entry["subject"], processed)
        if not reply_id:
            continue
        processed.add(reply_id)
        entry["status"] = "approved"
        entry["approved_at"] = datetime.now(timezone.utc).isoformat()
        run_calls.append(("approved", niche, True))


def main():
    selected_by_niche = get_selected_leads_by_niche()
    state = load_state()
    processed = load_processed_ids()
    envelopes = list_inbox()

    run_calls = []
    start_new_requests(selected_by_niche, state, run_calls)
    check_template_replies(state, envelopes, processed, run_calls)
    check_confirmation_replies(state, envelopes, processed, run_calls)
    send_reminders_where_due(state, run_calls)

    save_state(state)
    save_processed_ids(processed)

    approved = [n for n, e in state.items() if e["status"] == "approved"]
    pending = [n for n, e in state.items() if e["status"] != "approved"]
    print(f"Ghost template requests: {len(approved)} approved, {len(pending)} pending.")
    return {"notes": f"approved={approved} pending={pending} actions={run_calls}"}


if __name__ == "__main__":
    import hp_runlog
    hp_runlog.run_wrapped("ghost_template_request", main)
