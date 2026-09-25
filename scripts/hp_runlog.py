#!/usr/bin/env python3
"""
Run-log + wrapper — SiteScout

Shared module every script's `if __name__ == "__main__":` block calls
through instead of invoking main() bare. Logs every run to state.db
(SQLite) — start, finish, success/fail — so nothing dies silently, and
sends a real Himalaya alert email on failure, regardless of HP_ENV: a
script crash during dev testing is exactly what testing is for, not
noise to be gated away (unlike Sprint 6's positive-reply notification,
which is dev-gated specifically to avoid noise from fake TEST-lead data,
not because failures are less important in dev).

Known scope boundary: this only catches exceptions raised *inside*
main() — an import-time failure (e.g. hp_env.py's SystemExit on a
missing required key) happens before main() is ever called and can't be
caught here.
"""

import os
import sqlite3
import subprocess
import sys
import traceback
from datetime import datetime, timezone

import hp_env
import hp_config

DB_FILE = os.path.join(hp_env.PROJECT_ROOT, "state.db")
ALERT_ADDRESS = hp_config.OPERATOR_EMAIL


def _connect():
    conn = sqlite3.connect(DB_FILE, timeout=10)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS runs (
            run_id INTEGER PRIMARY KEY AUTOINCREMENT,
            script_name TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            status TEXT NOT NULL,
            error_message TEXT,
            leads_found INTEGER,
            emails_sent INTEGER,
            notes TEXT
        )
    """)
    # Sprint 13a: audit trail for config.yaml edits made via the dashboard's
    # settings panel. Reuses state.db rather than a separate file, per the
    # plan's own suggestion. Single-user tool, no auth exists anywhere in
    # this project — "who" is implicitly always Cyril, so only when/what
    # is tracked, not a real identity column.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS settings_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            key TEXT NOT NULL,
            old_value TEXT,
            new_value TEXT NOT NULL,
            changed_at TEXT NOT NULL
        )
    """)
    # Sprint 22: same audit-trail pattern as settings_history, for edits
    # made via the dashboard's Content page (email_templates/*.md,
    # ghost_templates/*.md). Full old/new text stored, not a diff — these
    # files are short enough that a diff would save little and cost
    # readability.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS content_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            key TEXT NOT NULL,
            old_text TEXT,
            new_text TEXT NOT NULL,
            changed_at TEXT NOT NULL
        )
    """)
    return conn


def record_setting_change(key, old_value, new_value):
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO settings_history (key, old_value, new_value, changed_at) VALUES (?, ?, ?, ?)",
            (key, str(old_value) if old_value is not None else None, str(new_value), _now()),
        )
        conn.commit()
    finally:
        conn.close()


def get_settings_history(limit=20):
    conn = _connect()
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM settings_history ORDER BY changed_at DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def record_content_change(key, old_text, new_text):
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO content_history (key, old_text, new_text, changed_at) VALUES (?, ?, ?, ?)",
            (key, old_text, new_text, _now()),
        )
        conn.commit()
    finally:
        conn.close()


def get_content_history(limit=20):
    conn = _connect()
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM content_history ORDER BY changed_at DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_run_history(script_name, limit=10):
    """Sprint 22: the dashboard's per-script drill-down — last N runs for
    ONE script, not just the latest (which get_status_grid()'s own query
    in dashboard/app.py already covers)."""
    conn = _connect()
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM runs WHERE script_name = ? ORDER BY started_at DESC LIMIT ?",
        (script_name, limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _now():
    return datetime.now(timezone.utc).isoformat()


def start_run(script_name):
    conn = _connect()
    try:
        cursor = conn.execute(
            "INSERT INTO runs (script_name, started_at, status) VALUES (?, ?, ?)",
            (script_name, _now(), "running"),
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def finish_run_success(run_id, leads_found=None, emails_sent=None, notes=None):
    conn = _connect()
    try:
        conn.execute(
            "UPDATE runs SET finished_at = ?, status = ?, leads_found = ?, emails_sent = ?, notes = ? WHERE run_id = ?",
            (_now(), "success", leads_found, emails_sent, notes, run_id),
        )
        conn.commit()
    finally:
        conn.close()


def finish_run_failure(run_id, error_message):
    conn = _connect()
    try:
        conn.execute(
            "UPDATE runs SET finished_at = ?, status = ?, error_message = ? WHERE run_id = ?",
            (_now(), "fail", error_message, run_id),
        )
        conn.commit()
    finally:
        conn.close()


def send_failure_alert(script_name, error_message):
    subject = f"SITESCOUT FAILURE: {script_name}"
    body = f'"{script_name}" crashed:\n\n"""\n{error_message}\n"""'
    message = f"From: {ALERT_ADDRESS}\nTo: {ALERT_ADDRESS}\nSubject: {subject}\n\n{body}\n"
    result = subprocess.run(
        ["himalaya", "message", "send"],
        input=message, text=True, capture_output=True,
    )
    if result.returncode != 0:
        print(f"    Failure alert itself failed to send: {result.stderr[:200]}")
    return result.returncode == 0


def run_wrapped(script_name, main_func):
    """Calls main_func(). main_func may return a summary dict with any of
    "leads_found"/"emails_sent"/"notes" keys, or None. Never lets an
    exception escape this call — on failure, logs a fail row, sends a real
    alert email, prints the traceback for local debugging, then exits with
    a controlled non-zero code instead of an unhandled crash."""
    run_id = start_run(script_name)
    try:
        summary = main_func() or {}
        finish_run_success(
            run_id,
            leads_found=summary.get("leads_found"),
            emails_sent=summary.get("emails_sent"),
            notes=summary.get("notes"),
        )
    except Exception:
        error_message = traceback.format_exc()
        print(error_message, file=sys.stderr)
        finish_run_failure(run_id, error_message)
        send_failure_alert(script_name, error_message)
        sys.exit(1)
