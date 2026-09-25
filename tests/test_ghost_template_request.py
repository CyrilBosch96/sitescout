#!/usr/bin/env python3
"""
Tests for scripts/ghost_template_request.py.

Covers: grouping Selected leads by niche, the 10-lead threshold trigger,
12-hour reminder cadence, the batch-not-frozen-at-10 semantics (a lead
selected after the ask but before the reply still gets included since
counting happens fresh from Notion, not from a stored snapshot),
attachment detection/download, the mechanical processing pipeline
integration, and the two-step (template reply -> confirmation reply)
approval flow.
"""

import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

SCRIPTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
sys.path.insert(0, SCRIPTS_DIR)

import ghost_template_request as gtr  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_files(tmp_path, monkeypatch):
    monkeypatch.setattr(gtr, "STATE_FILE", str(tmp_path / "ghost_template_requests.json"))
    monkeypatch.setattr(gtr, "PROCESSED_FILE", str(tmp_path / "processed_ghost_template_replies.json"))
    monkeypatch.setattr(gtr, "TEMPLATES_DIR", str(tmp_path / "templates"))


# --- extract_niche() / niche_slug() ---

def test_extract_niche_splits_correctly():
    assert gtr.extract_niche("Barber Shops / Ann Arbor, Michigan") == "Barber Shops"


def test_extract_niche_none_when_no_separator():
    assert gtr.extract_niche("no separator here") is None


def test_niche_slug_lowercases_and_dashes():
    assert gtr.niche_slug("Barber Shops") == "barber-shops"
    assert gtr.niche_slug("Coffee Shops & Cafes") == "coffee-shops-cafes"


# --- get_selected_leads_by_niche() ---

def _notion_page(page_id, name, niche_location):
    return {
        "id": page_id,
        "properties": {
            "Business Name": {"title": [{"text": {"content": name}}]},
            "Source Niche/Location": {"rich_text": [{"text": {"content": niche_location}}]},
        },
    }


def test_get_selected_leads_by_niche_groups_correctly(monkeypatch):
    class FakeResponse:
        status_code = 200

        def json(self):
            return {
                "results": [
                    _notion_page("p1", "Shop A", "Barber Shops / Wichita, KS"),
                    _notion_page("p2", "Shop B", "Barber Shops / Topeka, KS"),
                    _notion_page("p3", "Cafe C", "Coffee Shops / Detroit, MI"),
                ],
                "has_more": False,
                "next_cursor": None,
            }

    monkeypatch.setattr(gtr.requests, "post", lambda url, headers=None, json=None: FakeResponse())
    result = gtr.get_selected_leads_by_niche()

    assert len(result["Barber Shops"]) == 2
    assert len(result["Coffee Shops"]) == 1
    assert result["Barber Shops"][0]["name"] == "Shop A"


# --- start_new_requests(): the 10-lead threshold ---

def _leads(n, prefix="Shop"):
    return [{"page_id": f"page-{i}", "name": f"{prefix} {i}"} for i in range(n)]


def test_start_new_requests_triggers_at_threshold(monkeypatch):
    monkeypatch.setattr(gtr, "send_email", lambda *a, **k: (True, ""))
    state = {}
    run_calls = []
    gtr.start_new_requests({"Barber Shops": _leads(10)}, state, run_calls)

    assert "Barber Shops" in state
    assert state["Barber Shops"]["status"] == "pending_template"
    assert ("new_request", "Barber Shops", True) in run_calls


def test_start_new_requests_does_not_trigger_below_threshold(monkeypatch):
    monkeypatch.setattr(gtr, "send_email", lambda *a, **k: (True, ""))
    state = {}
    run_calls = []
    gtr.start_new_requests({"Barber Shops": _leads(9)}, state, run_calls)
    assert "Barber Shops" not in state


def test_start_new_requests_does_not_re_trigger_already_tracked_niche(monkeypatch):
    monkeypatch.setattr(gtr, "send_email", lambda *a, **k: (True, ""))
    state = {"Barber Shops": {"status": "pending_template", "subject": "x", "thread_message_id": "y",
                               "requested_at": "2026-08-01T00:00:00+00:00", "last_reminded_at": "2026-08-01T00:00:00+00:00"}}
    run_calls = []
    gtr.start_new_requests({"Barber Shops": _leads(15)}, state, run_calls)
    assert run_calls == []  # already tracked, no new ask sent


# --- send_reminders_where_due(): 12h cadence ---

def test_reminder_sent_after_12_hours(monkeypatch):
    monkeypatch.setattr(gtr, "send_email", lambda *a, **k: (True, ""))
    old = (datetime.now(timezone.utc) - timedelta(hours=13)).isoformat()
    state = {"Barber Shops": {"status": "pending_template", "subject": "Ghost Site template needed: Barber Shops",
                               "thread_message_id": "<x@y>", "requested_at": old, "last_reminded_at": old}}
    run_calls = []
    gtr.send_reminders_where_due(state, run_calls)

    assert ("reminder", "Barber Shops", True) in run_calls


def test_reminder_not_sent_before_12_hours(monkeypatch):
    monkeypatch.setattr(gtr, "send_email", lambda *a, **k: (True, ""))
    recent = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    state = {"Barber Shops": {"status": "pending_template", "subject": "x",
                               "thread_message_id": "<x@y>", "requested_at": recent, "last_reminded_at": recent}}
    run_calls = []
    gtr.send_reminders_where_due(state, run_calls)
    assert run_calls == []


# --- check_template_replies(): attachment handling + processing pipeline ---

def _pending_state(subject="Ghost Site template needed: Barber Shops"):
    now = datetime.now(timezone.utc).isoformat()
    return {"Barber Shops": {
        "status": "pending_template", "subject": subject,
        "thread_message_id": "<orig@x>", "requested_at": now, "last_reminded_at": now,
    }}


def _envelope(msg_id, subject, sender_name="Cyril Bosch"):
    return {"id": msg_id, "subject": subject, "from": [{"name": sender_name, "email": "operator@example.com"}]}


def test_check_template_replies_processes_valid_html_attachment(monkeypatch, tmp_path):
    state = _pending_state()
    envelopes = [_envelope("msg-1", "Re: Ghost Site template needed: Barber Shops")]
    processed = set()
    run_calls = []

    upload_dir = tmp_path / "upload"
    upload_dir.mkdir()
    html_path = upload_dir / "template.html"
    html_path.write_text("<div>{{business_name}}</div>")

    monkeypatch.setattr(gtr, "download_html_attachment", lambda msg_id, dest_dir: str(html_path))
    monkeypatch.setattr(gtr, "send_email", lambda *a, **k: (True, ""))

    gtr.check_template_replies(state, envelopes, processed, run_calls)

    assert "msg-1" in processed
    assert state["Barber Shops"]["status"] == "pending_confirmation"
    saved_path = state["Barber Shops"]["template_path"]
    assert os.path.exists(saved_path)
    with open(saved_path) as f:
        assert f.read() == "<div>{{business_name}}</div>"
    assert ("confirmation_requested", "Barber Shops", True) in run_calls


def test_check_template_replies_no_attachment_sends_retry(monkeypatch):
    state = _pending_state()
    envelopes = [_envelope("msg-2", "Re: Ghost Site template needed: Barber Shops")]
    processed = set()
    run_calls = []

    monkeypatch.setattr(gtr, "download_html_attachment", lambda msg_id, dest_dir: None)
    monkeypatch.setattr(gtr, "send_email", lambda *a, **k: (True, ""))

    gtr.check_template_replies(state, envelopes, processed, run_calls)

    assert state["Barber Shops"]["status"] == "pending_template"  # unchanged
    assert ("no_attachment", "Barber Shops", True) in run_calls


def test_check_template_replies_bad_file_sends_retry(monkeypatch, tmp_path):
    state = _pending_state()
    envelopes = [_envelope("msg-3", "Re: Ghost Site template needed: Barber Shops")]
    processed = set()
    run_calls = []

    bad_path = tmp_path / "bad.html"
    bad_path.write_text('<script type="__bundler/template">not valid json</script>')

    monkeypatch.setattr(gtr, "download_html_attachment", lambda msg_id, dest_dir: str(bad_path))
    monkeypatch.setattr(gtr, "send_email", lambda *a, **k: (True, ""))

    gtr.check_template_replies(state, envelopes, processed, run_calls)

    assert state["Barber Shops"]["status"] == "pending_template"
    assert ("process_failed", "Barber Shops", True) in run_calls


def test_check_template_replies_ignores_unrelated_thread(monkeypatch):
    state = _pending_state()
    envelopes = [_envelope("msg-4", "Re: some unrelated subject")]
    processed = set()
    run_calls = []

    def should_not_download(msg_id, dest_dir):
        raise AssertionError("download_html_attachment called for an unrelated thread")

    monkeypatch.setattr(gtr, "download_html_attachment", should_not_download)
    gtr.check_template_replies(state, envelopes, processed, run_calls)
    assert run_calls == []


# --- check_confirmation_replies(): the second approval step ---

def test_confirmation_reply_marks_approved():
    now = datetime.now(timezone.utc).isoformat()
    state = {"Barber Shops": {
        "status": "pending_confirmation", "subject": "Ghost Site template needed: Barber Shops",
        "thread_message_id": "<x@y>", "requested_at": now, "last_reminded_at": now,
    }}
    envelopes = [_envelope("msg-5", "Re: Ghost Site template needed: Barber Shops")]
    processed = set()
    run_calls = []

    gtr.check_confirmation_replies(state, envelopes, processed, run_calls)

    assert state["Barber Shops"]["status"] == "approved"
    assert ("approved", "Barber Shops", True) in run_calls
    assert "msg-5" in processed


def test_no_confirmation_reply_stays_pending():
    now = datetime.now(timezone.utc).isoformat()
    state = {"Barber Shops": {
        "status": "pending_confirmation", "subject": "Ghost Site template needed: Barber Shops",
        "thread_message_id": "<x@y>", "requested_at": now, "last_reminded_at": now,
    }}
    run_calls = []
    gtr.check_confirmation_replies(state, [], set(), run_calls)
    assert state["Barber Shops"]["status"] == "pending_confirmation"


# --- list_attachments() / download_html_attachment(): Himalaya command shape ---

def test_list_attachments_parses_json(monkeypatch):
    class FakeResult:
        returncode = 0
        stdout = '{"attachments": [{"id": "1", "filename": "template.html"}]}'

    monkeypatch.setattr(gtr.subprocess, "run", lambda *a, **k: FakeResult())
    result = gtr.list_attachments("msg-1")
    assert result == [{"id": "1", "filename": "template.html"}]


def test_download_html_attachment_finds_html_file(monkeypatch, tmp_path):
    monkeypatch.setattr(gtr, "list_attachments", lambda msg_id: [
        {"id": "1", "filename": "photo.png"}, {"id": "2", "filename": "design.html"},
    ])
    captured = {}

    def fake_run(cmd, capture_output=None, text=None):
        captured["cmd"] = cmd

        class FakeResult:
            returncode = 0

        return FakeResult()

    monkeypatch.setattr(gtr.subprocess, "run", fake_run)
    result = gtr.download_html_attachment("msg-1", str(tmp_path))

    assert result == os.path.join(str(tmp_path), "design.html")
    assert captured["cmd"] == ["himalaya", "attachment", "download", "msg-1", "2", "-d", str(tmp_path)]


def test_download_html_attachment_returns_none_when_no_html(monkeypatch, tmp_path):
    monkeypatch.setattr(gtr, "list_attachments", lambda msg_id: [{"id": "1", "filename": "photo.png"}])
    assert gtr.download_html_attachment("msg-1", str(tmp_path)) is None


# --- load_ask_template() / load_reminder_template(): Sprint 22, file-backed copy ---

def test_load_ask_template_has_placeholders():
    text = gtr.load_ask_template()
    assert "[NICHE]" in text and "[LEAD_COUNT]" in text and "[LEAD_NAMES]" in text


def test_load_ask_template_reflects_file_edits(tmp_path, monkeypatch):
    monkeypatch.setattr(gtr, "EMAIL_TEMPLATES_DIR", str(tmp_path))
    (tmp_path / "template_request_ask.md").write_text("Edited ask for {niche} ({lead_count}): {lead_names}\n")
    assert gtr.load_ask_template() == "Edited ask for {niche} ({lead_count}): {lead_names}"


def test_load_reminder_template_reflects_file_edits(tmp_path, monkeypatch):
    monkeypatch.setattr(gtr, "EMAIL_TEMPLATES_DIR", str(tmp_path))
    (tmp_path / "template_request_reminder.md").write_text("Edited reminder copy.\n")
    assert gtr.load_reminder_template() == "Edited reminder copy."


def test_start_new_requests_body_uses_ask_template(monkeypatch):
    captured = {}
    monkeypatch.setattr(gtr, "send_email", lambda to, subject, body, **k: (captured.setdefault("body", body), (True, ""))[-1])
    state = {}
    gtr.start_new_requests({"Barber Shops": _leads(10, prefix="Elite")}, state, [])
    assert "Barber Shops" in captured["body"]
    assert "10" in captured["body"]
    assert "Elite 0" in captured["body"]
