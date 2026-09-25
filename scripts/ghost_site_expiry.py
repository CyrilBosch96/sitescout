#!/usr/bin/env python3
"""
Ghost Site Expiry + Resurrection — SiteScout (Sprint 19, Track B)
Usage:
  python3 ghost_site_expiry.py            (dry-run — resurrection email only)
  python3 ghost_site_expiry.py --live     (actually sends the resurrection email)

Daily cron (via orchestrator.py). Two independent phases:

1. Expiry. Any `Live` lead whose `Site Expiry Date` has passed gets its
   Cloudflare Pages project torn down for real — domain detached, DNS
   record removed, project deleted outright (not just emptied; Sprint
   17's one-project-per-lead design exists specifically so this is a
   single clean delete). Notion gets `Ghost Site Status` set to
   `Expired` and `Ghost Site URL` cleared. `Site Expiry Date` is
   deliberately left as-is — it's the timestamp phase 2 anchors on to
   know when the lead actually expired, not just a target date that's
   now stale.
   Not gated by --live/DRY_RUN: tearing down infra has no external
   visibility on its own (same reasoning as ghost_site_builder.py's
   deploy step) — nothing about it reaches the lead.

2. Resurrection. Any `Expired` lead that hasn't had a resurrection
   email yet (`Resurrection Sent Date` empty) and expired at least one
   full day ago gets ONE email, ever (Cyril's call, 2026-08-14 — no
   follow-up nagging if they don't reply), asking if they still want
   the site — free, no fee, matching the current "everything included"
   model. IS a lead-facing send, so --live/DRY_RUN gating and
   hp_env.resolve_recipient() apply exactly like cold_email.py /
   ghost_site_sequence.py.

A reply to the resurrection email is handled by inbox_monitoring.py,
not here: any reply from an `Expired` lead flips `Ghost Site Status`
back to `Selected` and clears `Resurrection Sent Date` (Cyril's call —
auto-rebuild, reusing ghost_site_builder.py's existing Selected+no-URL
pipeline as-is rather than writing new rebuild logic; same "same
trigger condition either way" pattern already used across this
codebase).
"""

import os
import subprocess
import sys
from datetime import date

import requests

import hp_env
import hp_template
import hp_config
import cloudflare_pages as cf

NOTION_API_KEY = hp_env.NOTION_API_KEY
NOTION_DATA_SOURCE_ID = hp_env.NOTION_DATA_SOURCE_ID
FROM_ADDRESS = hp_config.OPERATOR_EMAIL

DRY_RUN = not hp_env.live_allowed("--live" in sys.argv)


# --- Notion reads ---

def _get_title(props, key):
    t = props.get(key, {}).get("title", [])
    return t[0]["text"]["content"] if t else ""

def _get_rich_text(props, key):
    t = props.get(key, {}).get("rich_text", [])
    return t[0]["text"]["content"] if t else ""

def _get_email(props, key):
    return props.get(key, {}).get("email")

def _get_date(props, key):
    d = props.get(key, {}).get("date")
    if d and d.get("start"):
        return date.fromisoformat(d["start"][:10])
    return None


def get_lapsed_live_leads():
    """Live leads whose Site Expiry Date has arrived (today) or already
    passed — expiry acts on the expiry date itself, not the day after
    (matches Day 6/7's copy: "comes down on {expiry_date}", not the day
    after it). This also keeps the resurrection gap real: the
    resurrection filter below requires the expiry date to be strictly
    before today, so on the expiry date itself only phase 1 fires; the
    one-day gap only exists because these two filters use different
    comparisons against "today" — flip either one and the gap collapses
    to zero (found live while testing: an `on_or_before` cutoff here
    paired with `on_or_before` there fires both phases in the same run)."""
    url = f"https://api.notion.com/v1/data_sources/{NOTION_DATA_SOURCE_ID}/query"
    headers = {
        "Authorization": f"Bearer {NOTION_API_KEY}",
        "Notion-Version": "2025-09-03",
        "Content-Type": "application/json",
    }
    body = {
        "filter": {
            "and": [
                {"property": "Ghost Site Status", "select": {"equals": "Live"}},
                {"property": "Site Expiry Date", "date": {"on_or_before": date.today().isoformat()}},
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
                "ghost_site_url": props.get("Ghost Site URL", {}).get("url"),
            })
        has_more = data.get("has_more", False)
        cursor = data.get("next_cursor")
    return leads


def get_resurrection_candidates():
    """Expired leads with no resurrection email sent yet, strictly before
    today's Site Expiry Date — i.e. it expired on some earlier day, never
    the same day get_lapsed_live_leads() just tore it down (that
    function's own `on_or_before today` catches Site Expiry Date == today;
    this one's `before today` deliberately excludes that same day, which
    is what creates the one-day gap between teardown and the resurrection
    email)."""
    url = f"https://api.notion.com/v1/data_sources/{NOTION_DATA_SOURCE_ID}/query"
    headers = {
        "Authorization": f"Bearer {NOTION_API_KEY}",
        "Notion-Version": "2025-09-03",
        "Content-Type": "application/json",
    }
    body = {
        "filter": {
            "and": [
                {"property": "Ghost Site Status", "select": {"equals": "Expired"}},
                {"property": "Resurrection Sent Date", "date": {"is_empty": True}},
                {"property": "Site Expiry Date", "date": {"before": date.today().isoformat()}},
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
                "email_subject": _get_rich_text(props, "Email Subject"),
                "thread_message_id": _get_rich_text(props, "Thread Message ID"),
                "site_expiry_date": _get_date(props, "Site Expiry Date"),
            })
        has_more = data.get("has_more", False)
        cursor = data.get("next_cursor")
    return leads


# --- Notion writes ---

def mark_expired(page_id):
    url = f"https://api.notion.com/v1/pages/{page_id}"
    headers = {
        "Authorization": f"Bearer {NOTION_API_KEY}",
        "Notion-Version": "2025-09-03",
        "Content-Type": "application/json",
    }
    resp = requests.patch(url, headers=headers, json={
        "properties": {
            "Ghost Site Status": {"select": {"name": "Expired"}},
            "Ghost Site URL": {"url": None},
        }
    })
    return resp.status_code == 200


def mark_resurrection_sent(page_id):
    url = f"https://api.notion.com/v1/pages/{page_id}"
    headers = {
        "Authorization": f"Bearer {NOTION_API_KEY}",
        "Notion-Version": "2025-09-03",
        "Content-Type": "application/json",
    }
    resp = requests.patch(url, headers=headers, json={
        "properties": {"Resurrection Sent Date": {"date": {"start": date.today().isoformat()}}}
    })
    return resp.status_code == 200


EMAIL_TEMPLATES_DIR = os.path.join(hp_env.PROJECT_ROOT, "email_templates")


def load_resurrection_template():
    """Sprint 22: file-backed so it's editable from the dashboard's
    Content page without a code deploy."""
    path = os.path.join(EMAIL_TEMPLATES_DIR, "resurrection.md")
    with open(path) as f:
        return f.read().rstrip("\n")


# --- email ---

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


# --- Cloudflare project teardown ---

def project_slug_from_url(url_value):
    """"https://{slug}.example.com" -> "{slug}" — the project name is
    always the URL's subdomain label, set that way by ghost_site_builder.py."""
    without_scheme = url_value.split("//", 1)[-1]
    return without_scheme.split(".", 1)[0]


def teardown_site(ghost_site_url):
    """Detach domain -> delete DNS record -> delete project, in that
    order (delete_project() 400s if the domain is still attached —
    found live cleaning up Sprint 17's own test project)."""
    domain = ghost_site_url.split("//", 1)[-1]
    project = project_slug_from_url(ghost_site_url)

    ok, err = cf.detach_custom_domain(project, domain)
    if not ok:
        return False, f"detach_custom_domain failed: {err[:200]}"

    cf.delete_dns_record_for(domain)  # best-effort, not fatal if it fails/finds nothing

    ok, err = cf.delete_project(project)
    if not ok:
        return False, f"delete_project failed: {err[:200]}"
    return True, None


# --- the two phases ---

def expire_lapsed_sites(run_calls):
    leads = get_lapsed_live_leads()
    for lead in leads:
        if not lead["ghost_site_url"]:
            continue  # defensive: nothing to tear down
        ok, err = teardown_site(lead["ghost_site_url"])
        if not ok:
            print(f"  {lead['name']} -> TEARDOWN FAILED: {err}")
            run_calls.append(("teardown_failed", lead["page_id"], False))
            continue
        ok = mark_expired(lead["page_id"])
        print(f"  {lead['name']} -> expired ({'OK' if ok else 'Notion write FAILED'})")
        run_calls.append(("expired", lead["page_id"], ok))
    return leads


def send_resurrection_emails(run_calls):
    leads = get_resurrection_candidates()
    for lead in leads:
        if not lead["email"]:
            continue
        original_subject = lead["email_subject"] or f"{lead['name']}'s website"
        subject = f"Re: {original_subject}"
        body = hp_template.render(load_resurrection_template(), business_name=lead["name"])
        recipient = hp_env.resolve_recipient(lead["email"])

        if DRY_RUN:
            print(f"  {lead['name']} -> [DRY RUN] would send resurrection email")
            run_calls.append(("resurrection_dry_run", lead["page_id"], True))
            continue

        success, err = send_email(recipient, subject, body, in_reply_to=lead["thread_message_id"] or None)
        if not success:
            print(f"  {lead['name']} -> RESURRECTION SEND FAILED: {err[:200]}")
            run_calls.append(("resurrection_send_failed", lead["page_id"], False))
            continue
        ok = mark_resurrection_sent(lead["page_id"])
        print(f"  {lead['name']} -> resurrection email sent ({'OK' if ok else 'Notion write FAILED'})")
        run_calls.append(("resurrection_sent", lead["page_id"], ok))
    return leads


def main():
    mode = "LIVE — will actually send resurrection emails" if not DRY_RUN else "DRY RUN — resurrection emails will not send"
    print(f"=== Ghost Site Expiry run: {mode} (HP_ENV={hp_env.HP_ENV}) ===\n")

    run_calls = []

    print("Phase 1: expiring lapsed Live sites...")
    expired_leads = expire_lapsed_sites(run_calls)

    print("\nPhase 2: sending resurrection emails...")
    resurrection_leads = send_resurrection_emails(run_calls)

    print("\n--- Summary ---")
    print(f"Sites expired: {len(expired_leads)}")
    print(f"Resurrection emails {'would be ' if DRY_RUN else ''}sent: {len(resurrection_leads)}")

    return {"notes": f"expired={len(expired_leads)} resurrection={len(resurrection_leads)} dry_run={DRY_RUN} actions={run_calls}"}


if __name__ == "__main__":
    import hp_runlog
    hp_runlog.run_wrapped("ghost_site_expiry", main)
