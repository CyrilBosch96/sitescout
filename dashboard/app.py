#!/usr/bin/env python3
"""
Dashboard — SiteScout (Sprint 13)
Usage: python3 dashboard/app.py

Flask app, localhost:5050 only (127.0.0.1, no external exposure, no auth —
single-user internal tool). Status grid + failures panel read from
state.db (Sprint 11). Manual trigger buttons run a real script via
subprocess, synchronously, then redirect back so the grid reflects the
fresh row. "Today's numbers" reuses daily_reporting.build_report()
directly (Gap 9) rather than recomputing the same aggregation twice.

Built in the dev sibling directory, not the plan's stated production
path (~/sitescout/dashboard/) — same dev-first pattern as every
other sprint in this rebuild; promotion is Sprint 14's job.
"""

import os
import sqlite3
import subprocess
import sys
import threading
from datetime import date, datetime

from flask import Flask, redirect, render_template, request, url_for

SCRIPTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
sys.path.insert(0, SCRIPTS_DIR)

import hp_env  # noqa: E402
import hp_runlog  # noqa: E402
import hp_settings  # noqa: E402
import hp_content  # noqa: E402
import hp_lock  # noqa: E402
import hp_status  # noqa: E402
import daily_reporting  # noqa: E402
import cold_email  # noqa: E402
import ghost_template_processing as gtp  # noqa: E402
import ghost_template_request as gtr  # noqa: E402
import requests  # noqa: E402

app = Flask(__name__)
app.jinja_env.filters["ist"] = hp_status.to_ist


@app.context_processor
def inject_globals():
    # HP_ENV shown in the topnav on every page (base.html) — a global
    # context processor instead of passing config_env from each route
    # individually, so a new page can't silently forget it.
    return {"config_env": hp_env.HP_ENV}

# Maps the exact script_name strings hp_runlog.run_wrapped() logs under
# (see each script's `if __name__ == "__main__"` block) to their file.
SCRIPTS = {
    "queue_check": "queue_check.py",
    "lead_discovery_and_evaluation_Contactsearch": "lead_discovery_and_evaluation_Contactsearch.py",
    "cold_email": "cold_email.py",
    "inbox_monitoring": "inbox_monitoring.py",
    "bounce_check": "bounce_check.py",
    "daily_reporting": "daily_reporting.py",
    "orchestrator": "orchestrator.py",
    "ghost_site_sequence": "ghost_site_sequence.py",
    "check_opens_reminder": "check_opens_reminder.py",
    "ghost_template_request": "ghost_template_request.py",
    "ghost_site_builder": "ghost_site_builder.py",
    "ghost_site_expiry": "ghost_site_expiry.py",
}


def get_status_grid():
    """Most recent row per script, including scripts that have never run
    (shown as "never run" rather than omitted — a script Sprint 11 wraps
    but nobody's triggered yet is still worth seeing on the grid)."""
    conn = hp_runlog._connect()  # creates the runs table if it doesn't exist yet
    conn.row_factory = sqlite3.Row
    rows = {}
    for row in conn.execute("SELECT * FROM runs ORDER BY started_at ASC"):
        rows[row["script_name"]] = dict(row)
    conn.close()

    grid = []
    for script_name in SCRIPTS:
        row = rows.get(script_name)
        if not row:
            grid.append({"script_name": script_name, "status": "never run", "started_at": None,
                         "duration_seconds": None, "notes": None})
            continue
        duration = None
        if row["started_at"] and row["finished_at"]:
            started = datetime.fromisoformat(row["started_at"])
            finished = datetime.fromisoformat(row["finished_at"])
            duration = (finished - started).total_seconds()
        grid.append({
            "script_name": script_name, "status": row["status"],
            "started_at": row["started_at"], "duration_seconds": duration,
            "notes": row["notes"],
        })
    return grid


def get_recent_failures(limit=10):
    conn = hp_runlog._connect()
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM runs WHERE status = 'fail' ORDER BY started_at DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_leads():
    url = f"https://api.notion.com/v1/data_sources/{hp_env.NOTION_DATA_SOURCE_ID}/query"
    headers = {
        "Authorization": f"Bearer {hp_env.NOTION_API_KEY}",
        "Notion-Version": "2025-09-03",
        "Content-Type": "application/json",
    }
    leads = []
    cursor = None
    has_more = True
    while has_more:
        body = {"start_cursor": cursor} if cursor else {}
        resp = requests.post(url, headers=headers, json=body)
        if resp.status_code != 200:
            break
        data = resp.json()
        for page in data.get("results", []):
            props = page.get("properties", {})
            title = props.get("Business Name", {}).get("title", [])
            box = props.get("Box", {}).get("select") or {}
            contact_status = props.get("Contact Status", {}).get("select") or {}
            ghost_status = props.get("Ghost Site Status", {}).get("select") or {}
            leads.append({
                "page_id": page["id"],
                "name": title[0]["text"]["content"] if title else "Unknown",
                "box": box.get("name"),
                "contact_status": contact_status.get("name"),
                "ghost_site_status": ghost_status.get("name"),
            })
        has_more = data.get("has_more", False)
        cursor = data.get("next_cursor")
    return leads


def set_ghost_site_status_selected(page_ids):
    headers = {
        "Authorization": f"Bearer {hp_env.NOTION_API_KEY}",
        "Notion-Version": "2025-09-03",
        "Content-Type": "application/json",
    }
    results = {}
    for page_id in page_ids:
        url = f"https://api.notion.com/v1/pages/{page_id}"
        resp = requests.patch(url, headers=headers, json={
            "properties": {"Ghost Site Status": {"select": {"name": "Selected"}}}
        })
        results[page_id] = resp.status_code == 200
    return results


def clear_ghost_site_selection(page_ids):
    """Reverts "Selected" leads back to no Ghost Site Status at all, so
    they return to the main candidate pool. Only meant for leads still at
    "Selected" (not yet built) — a lead already "Live" has real preview
    emails already sent, so clearing that status back to null would be
    misleading (the dashboard only offers this for "Selected" leads, but
    this function itself doesn't re-check status — callers must filter)."""
    headers = {
        "Authorization": f"Bearer {hp_env.NOTION_API_KEY}",
        "Notion-Version": "2025-09-03",
        "Content-Type": "application/json",
    }
    results = {}
    for page_id in page_ids:
        url = f"https://api.notion.com/v1/pages/{page_id}"
        resp = requests.patch(url, headers=headers, json={
            "properties": {"Ghost Site Status": {"select": None}}
        })
        results[page_id] = resp.status_code == 200
    return results


def mark_converted(page_id):
    """Closing a deal is a human sales conversation — this button exists
    so that human call has somewhere to land, not to automate it. Sets
    Converted At alongside the status so reporting has a real timestamp
    to work with, unlike the old lifetime-snapshot-only count."""
    url = f"https://api.notion.com/v1/pages/{page_id}"
    headers = {
        "Authorization": f"Bearer {hp_env.NOTION_API_KEY}",
        "Notion-Version": "2025-09-03",
        "Content-Type": "application/json",
    }
    resp = requests.patch(url, headers=headers, json={
        "properties": {
            "Ghost Site Status": {"select": {"name": "Converted"}},
            "Converted At": {"date": {"start": date.today().isoformat()}},
        }
    })
    return resp.status_code == 200


def trigger_script(script_name, extra_args=None):
    # sys.executable, not bare "python3" — found live: launchd's minimal
    # PATH resolves bare "python3" to a stray /usr/local/bin/python3
    # (python.org 3.10, missing pyyaml/flask) ahead of the real
    # /opt/homebrew/bin/python3 this project actually installs into.
    #
    # Shares orchestrator.py's lock (hp_lock) — found live 2026-08-17: two
    # manual Trigger clicks 23s apart both ran lead_discovery concurrently,
    # each reading Notion's existing-leads list before either had written,
    # so both wrote the same qualifying businesses as "new". Without this
    # lock a manual trigger can equally race the hourly orchestrator cycle.
    #
    # Exception: triggering "orchestrator" itself must NOT acquire the lock
    # here — orchestrator.py acquires it internally already, and holding it
    # for the whole subprocess would make the child's own acquire_lock()
    # always see it held and skip immediately, silently no-op'ing every
    # manual orchestrator run.
    needs_lock = script_name != "orchestrator"
    if needs_lock and not hp_lock.acquire_lock():
        return False, "Another pipeline run is already in progress — try again shortly."
    script_path = os.path.join(SCRIPTS_DIR, SCRIPTS[script_name])
    args = [sys.executable, script_path] + (extra_args or [])
    # Backgrounded (2026-08-18) — subprocess.run() used to block the whole
    # HTTP request until the script finished, which for "orchestrator" can
    # be minutes: clicking Run just hung the page with zero feedback. Now
    # the request returns immediately; hp_status.get_current_status() (via
    # index.html's while-running auto-refresh) shows live progress instead.
    _start_background(_run_and_release, (script_name, args, needs_lock))
    return True, None


def _run_and_release(script_name, args, needs_lock):
    try:
        subprocess.run(args, env=os.environ.copy())
    finally:
        if needs_lock:
            hp_lock.release_lock()


def _start_background(target, args):
    """Thin seam so tests can run the target synchronously (deterministic,
    no real thread/race) instead of monkeypatching threading.Thread
    itself."""
    threading.Thread(target=target, args=args, daemon=True).start()


STAGE_LABELS = {
    "Not Contacted": "First cold email",
    "Follow 1": "Follow-up #1",
    "Follow 2": "Follow-up #2",
    "Follow 3": "Follow-up #3",
    "Follow 4": "Follow-up #4",
}


def get_upcoming_sends_summary():
    """Formats cold_email.preview_upcoming_sends() for the dashboard's
    "what's about to happen" panel — counts per stage plus, for leads only
    blocked on their local morning, the distinct timezones and how long
    until each one's next send window."""
    preview = cold_email.preview_upcoming_sends()
    stage_order = ["Not Contacted", "Follow 1", "Follow 2", "Follow 3", "Follow 4"]
    stages = []
    for status in stage_order:
        data = preview["stages"].get(status)
        if not data:
            continue
        waiting_windows = []
        for tz in sorted(set(data["waiting_timezones"])):
            count = data["waiting_timezones"].count(tz)
            window = cold_email.next_local_send_window(tz)
            waiting_windows.append({
                "timezone": tz,
                "count": count,
                "next_window": window.strftime("%-I:%M %p %Z, %b %-d") if window else "unknown",
                # Same moment in Cyril's own timezone (2026-08-18) — the
                # lead-local time alone means mentally converting a UTC
                # offset every time this panel is checked.
                "next_window_ist": window.astimezone(hp_status.IST).strftime("%-I:%M %p, %b %-d") + " IST" if window else "unknown",
            })
        stages.append({
            "status": status,
            "label": STAGE_LABELS.get(status, status),
            "ready": data["ready"],
            "waiting_for_quota": data["waiting_for_quota"],
            "not_yet_due": data["not_yet_due"],
            "waiting_windows": waiting_windows,
        })
    return {
        "stages": stages,
        "max_new_sends": preview["max_new_sends"],
        "new_quota_remaining": preview["new_quota_remaining"],
    }


@app.route("/")
def index():
    today_report = daily_reporting.build_report(date.today())
    # Candidates only — a lead leaves this pool the moment it's Selected
    # (2026-08-17), so the table always reflects "leads to pick from
    # right now," not a mix of picked and unpicked. It refills automatically
    # as new Box 1 leads land in the CRM with no Ghost Site Status yet.
    candidate_leads = [l for l in get_leads() if not l["ghost_site_status"]]
    return render_template(
        "index.html",
        grid=get_status_grid(),
        failures=get_recent_failures(),
        today_report=today_report,
        leads=candidate_leads,
        scripts=list(SCRIPTS.keys()),
        config_env=hp_env.HP_ENV,
        pipeline_status=hp_status.get_current_status(),
        checked_places_cache=hp_status.get_checked_places_cache_info(),
        upcoming_sends=get_upcoming_sends_summary(),
    )


@app.route("/cache/clear-checked-places", methods=["POST"])
def clear_checked_places_cache():
    hp_status.clear_checked_places_cache()
    return redirect(url_for("index"))


@app.route("/trigger/<script_name>", methods=["POST"])
def trigger(script_name):
    if script_name not in SCRIPTS:
        return "Unknown script", 404

    if script_name == "lead_discovery_and_evaluation_Contactsearch":
        niche = request.form.get("niche", "").strip()
        location = request.form.get("location", "").strip()
        if not niche or not location:
            return "Lead Discovery requires both niche and location", 400
        ok, error = trigger_script(script_name, [niche, location])
    elif script_name == "orchestrator":
        # Without this, a manual trigger always runs dry — sys.argv for a
        # dashboard-launched subprocess never contains "--live" on its own,
        # so orchestrator.py's own LIVE = hp_env.live_allowed("--live" in
        # sys.argv) would be False even in production, silently no-op'ing
        # every send/discovery step. Match the scheduled hourly job's flag
        # exactly: --live only where hp_env allows it at all.
        ok, error = trigger_script(script_name, ["--live"] if hp_env.IS_PRODUCTION else [])
    else:
        ok, error = trigger_script(script_name)

    if not ok:
        return error, 409

    return redirect(url_for("index"))


@app.route("/leads/select", methods=["POST"])
def select_leads():
    page_ids = request.form.getlist("page_id")
    if page_ids:
        set_ghost_site_status_selected(page_ids)
    # Selecting moves leads off the candidate pool (index.html filters to
    # ghost_site_status is None) straight into the working list — land
    # there so Cyril sees exactly what he just queued up.
    return redirect(url_for("selected_leads"))


@app.route("/leads/selected")
def selected_leads():
    """Track B's "working" list — separate from the main candidate pool on
    purpose (2026-08-17): once a lead is Selected it's no longer part of
    "leads to pick from," it's part of "what's currently happening." Drops
    a lead automatically the moment ghost_site_sequence.py sends its first
    preview email and flips status Selected -> Live, since this page only
    shows "Selected" for the clearable/pending section."""
    leads = get_leads()
    pending = [l for l in leads if l["ghost_site_status"] == "Selected"]
    active = [l for l in leads if l["ghost_site_status"] in ("Live", "Replied")]
    return render_template("selected_leads.html", pending=pending, active=active, config_env=hp_env.HP_ENV)


@app.route("/leads/clear-selection", methods=["POST"])
def clear_selection():
    page_ids = request.form.getlist("page_id")
    if page_ids:
        clear_ghost_site_selection(page_ids)
    return redirect(url_for("selected_leads"))


@app.route("/leads/convert/<page_id>", methods=["POST"])
def convert_lead(page_id):
    mark_converted(page_id)
    return redirect(url_for("selected_leads"))


@app.route("/settings")
def settings():
    current = hp_settings.read_current_config()
    fields = []
    for key, meta in hp_settings.EDITABLE_KEYS.items():
        fields.append({
            "key": key,
            "label": meta["label"],
            "current_value": current.get(key),
            "default_value": hp_settings.GOOGLE_DEFAULTS.get(key),
        })
    return render_template(
        "settings.html",
        fields=fields,
        history=hp_runlog.get_settings_history(),
        error=request.args.get("error"),
    )


@app.route("/settings/update", methods=["POST"])
def update_settings():
    key = request.form.get("key", "")
    raw_value = request.form.get("value", "")
    success, error = hp_settings.update_setting(key, raw_value)
    if not success:
        return redirect(url_for("settings", error=error))
    return redirect(url_for("settings"))


@app.route("/content")
def content():
    items = []
    for key, meta in hp_content.CONTENT_REGISTRY.items():
        items.append({
            "key": key, "label": meta["label"], "category": meta["category"],
            "placeholders": meta["placeholders"], "text": hp_content.read_content(key),
        })
    return render_template(
        "content.html",
        items=items,
        history=hp_runlog.get_content_history(),
        error=request.args.get("error"),
    )


@app.route("/content/update", methods=["POST"])
def update_content():
    key = request.form.get("key", "")
    new_text = request.form.get("text", "")
    success, error = hp_content.write_content(key, new_text)
    if not success:
        return redirect(url_for("content", error=error))
    return redirect(url_for("content"))


@app.route("/history/<script_name>")
def history(script_name):
    if script_name not in SCRIPTS:
        return "Unknown script", 404
    return render_template(
        "history.html",
        script_name=script_name,
        runs=hp_runlog.get_run_history(script_name, limit=10),
    )


@app.route("/templates")
def templates_page():
    state = gtr.load_state()
    existing = []
    if os.path.isdir(gtr.TEMPLATES_DIR):
        for name in sorted(os.listdir(gtr.TEMPLATES_DIR)):
            index_path = os.path.join(gtr.TEMPLATES_DIR, name, "index.html")
            if os.path.exists(index_path):
                existing.append(name)
    return render_template(
        "templates_upload.html",
        state=state,
        existing=existing,
        error=request.args.get("error"),
    )


def _parse_price(raw_price):
    """Returns a non-negative float, or None if raw_price is blank/invalid.
    Price lives per-niche (Cyril's call, 2026-08-28): every lead in a niche
    gets the same template, so the price is set once per template rather
    than per individual lead."""
    if raw_price is None or raw_price.strip() == "":
        return None
    try:
        value = float(raw_price)
    except ValueError:
        return None
    return value if value >= 0 else None


@app.route("/templates/upload", methods=["POST"])
def upload_template():
    niche = request.form.get("niche", "").strip()
    file = request.files.get("html_file")
    if not niche or not file or not file.filename:
        return redirect(url_for("templates_page", error="A niche name and an HTML file are both required"))

    price = _parse_price(request.form.get("price"))

    raw_html = file.read().decode("utf-8", errors="replace")
    try:
        cleaned = gtp.process_template_upload(raw_html)
    except ValueError as e:
        return redirect(url_for("templates_page", error=f"Couldn't process that file: {e}"))

    slug = gtr.niche_slug(niche)
    out_dir = os.path.join(gtr.TEMPLATES_DIR, slug)
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "index.html")
    with open(out_path, "w") as f:
        f.write(cleaned)

    # Optional — a template may be a single HTML file with everything
    # inlined, or HTML + a separate CSS file (how Paper AI actually
    # exports). Not required.
    css_path = None
    css_file = request.files.get("css_file")
    if css_file and css_file.filename:
        css_path = os.path.join(out_dir, "style.css")
        css_file.save(css_path)

    data_slots = sorted(gtp.find_data_slots(cleaned))

    # A niche uploaded here may already have an in-flight email ask
    # (existing entry from ghost_template_request.py's automated
    # threshold trigger) or may be brand new (Cyril templating a niche
    # proactively, ahead of the 10-lead threshold) — either way it lands
    # in the exact same pending_confirmation state ghost_template_request.py
    # itself uses, so ghost_site_builder.py needs zero changes to pick it
    # up once approved.
    now = datetime.now().astimezone().isoformat()
    state = gtr.load_state()
    entry = state.get(niche, {})
    entry["status"] = "pending_confirmation"
    entry["template_path"] = out_path
    entry["css_path"] = css_path
    entry["detected_placeholders"] = data_slots
    entry["uploaded_via"] = "dashboard"
    if price is not None:
        entry["price"] = price
    entry.setdefault("subject", f"Ghost Site template needed: {niche}")
    entry.setdefault("thread_message_id", None)
    entry.setdefault("requested_at", now)
    entry.setdefault("last_reminded_at", now)
    state[niche] = entry
    gtr.save_state(state)

    return redirect(url_for("templates_page"))


@app.route("/templates/confirm/<niche>", methods=["POST"])
def confirm_template(niche):
    state = gtr.load_state()
    entry = state.get(niche)
    if entry and entry.get("status") == "pending_confirmation":
        entry["status"] = "approved"
        gtr.save_state(state)
    return redirect(url_for("templates_page"))


@app.route("/templates/price/<niche>", methods=["POST"])
def update_template_price(niche):
    """Set or adjust an existing template's price — works for any niche
    already in state, including ones approved via the email-reply flow
    (ghost_template_request.py) that never had a price set at upload time,
    since that flow doesn't go through the dashboard's upload form at all."""
    state = gtr.load_state()
    entry = state.get(niche)
    if not entry:
        return redirect(url_for("templates_page", error=f"No template found for '{niche}'"))

    price = _parse_price(request.form.get("price"))
    if price is None:
        return redirect(url_for("templates_page", error="Price must be a non-negative number"))

    entry["price"] = price
    gtr.save_state(state)
    return redirect(url_for("templates_page"))


if __name__ == "__main__":
    # Configurable so dev and production dashboards can run simultaneously
    # on the same Mac without a port conflict — production's plist sets
    # DASHBOARD_PORT=5051, dev stays on the original 5050 by default.
    port = int(os.environ.get("DASHBOARD_PORT", 5050))
    app.run(host="127.0.0.1", port=port)
