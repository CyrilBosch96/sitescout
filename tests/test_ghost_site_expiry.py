#!/usr/bin/env python3
"""
Tests for scripts/ghost_site_expiry.py — Sprint 19 (Expiry + Resurrection).

Covers Sprint 19's AC and Cyril's confirmed design (2026-08-14):
  - A lapsed Live lead (Site Expiry Date passed) gets its Cloudflare
    Pages project torn down for real (detach domain -> delete DNS
    record -> delete project) and Notion set to Expired with the URL
    cleared. Not gated by --live (infra teardown, no lead-facing send).
  - Teardown failure stops before the Notion write — never mark a lead
    Expired if its site might still actually be live.
  - A resurrection email goes out exactly once, only to an Expired lead
    that hasn't had one yet and expired at least a full day ago — and
    IS gated by --live/DRY_RUN like every other lead-facing send.
  - project_slug_from_url() correctly extracts the project name from the
    stored Ghost Site URL (the subdomain label).
"""

import os
import sys
from datetime import date, timedelta

import pytest

SCRIPTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
sys.path.insert(0, SCRIPTS_DIR)

import ghost_site_expiry as gse  # noqa: E402


# --- project_slug_from_url() ---

def test_project_slug_from_url_extracts_subdomain_label():
    assert gse.project_slug_from_url("https://westgate-barber-3b92797c.example.com") == "westgate-barber-3b92797c"


# --- teardown_site(): detach -> delete DNS -> delete project, in order ---

def test_teardown_site_success_calls_in_order(monkeypatch):
    calls = []
    monkeypatch.setattr(gse.cf, "detach_custom_domain", lambda project, domain: (calls.append(("detach", project, domain)), (True, ""))[-1])
    monkeypatch.setattr(gse.cf, "delete_dns_record_for", lambda domain: (calls.append(("dns", domain)), (True, ""))[-1])
    monkeypatch.setattr(gse.cf, "delete_project", lambda project: (calls.append(("delete", project)), (True, ""))[-1])

    ok, err = gse.teardown_site("https://myproj.example.com")

    assert ok is True
    assert err is None
    assert [c[0] for c in calls] == ["detach", "dns", "delete"]


def test_teardown_site_stops_when_detach_fails(monkeypatch):
    monkeypatch.setattr(gse.cf, "detach_custom_domain", lambda project, domain: (False, "boom"))

    def should_not_delete_project(*a, **k):
        raise AssertionError("delete_project should not run after detach failure")
    monkeypatch.setattr(gse.cf, "delete_project", should_not_delete_project)

    ok, err = gse.teardown_site("https://myproj.example.com")
    assert ok is False
    assert "detach_custom_domain failed" in err


def test_teardown_site_stops_when_delete_project_fails(monkeypatch):
    monkeypatch.setattr(gse.cf, "detach_custom_domain", lambda project, domain: (True, ""))
    monkeypatch.setattr(gse.cf, "delete_dns_record_for", lambda domain: (True, ""))
    monkeypatch.setattr(gse.cf, "delete_project", lambda project: (False, "still has deployments"))

    ok, err = gse.teardown_site("https://myproj.example.com")
    assert ok is False
    assert "delete_project failed" in err


# --- expire_lapsed_sites(): teardown failure never reaches the Notion write ---

def _lapsed_lead(page_id="page-1", name="Lapsed Barber", ghost_site_url="https://lapsed-barber-abc123.example.com"):
    return {"page_id": page_id, "name": name, "ghost_site_url": ghost_site_url}


def test_expire_lapsed_sites_marks_expired_on_success(monkeypatch):
    monkeypatch.setattr(gse, "get_lapsed_live_leads", lambda: [_lapsed_lead()])
    monkeypatch.setattr(gse, "teardown_site", lambda url: (True, None))

    marked = []
    monkeypatch.setattr(gse, "mark_expired", lambda page_id: marked.append(page_id) or True)

    run_calls = []
    gse.expire_lapsed_sites(run_calls)

    assert marked == ["page-1"]
    assert ("expired", "page-1", True) in run_calls


def test_expire_lapsed_sites_does_not_mark_expired_when_teardown_fails(monkeypatch):
    monkeypatch.setattr(gse, "get_lapsed_live_leads", lambda: [_lapsed_lead()])
    monkeypatch.setattr(gse, "teardown_site", lambda url: (False, "boom"))

    def should_not_mark(*a, **k):
        raise AssertionError("mark_expired should not run after a teardown failure")
    monkeypatch.setattr(gse, "mark_expired", should_not_mark)

    run_calls = []
    gse.expire_lapsed_sites(run_calls)

    assert ("teardown_failed", "page-1", False) in run_calls


def test_expire_lapsed_sites_skips_lead_with_no_url(monkeypatch):
    """Defensive: a Live lead should always have a URL, but never call
    teardown_site() with nothing to tear down."""
    monkeypatch.setattr(gse, "get_lapsed_live_leads", lambda: [_lapsed_lead(ghost_site_url=None)])

    def should_not_teardown(*a, **k):
        raise AssertionError("teardown_site should not be called when Ghost Site URL is empty")
    monkeypatch.setattr(gse, "teardown_site", should_not_teardown)

    run_calls = []
    gse.expire_lapsed_sites(run_calls)
    assert run_calls == []


# --- send_resurrection_emails(): --live gating, one-shot semantics ---

def _resurrection_lead(page_id="page-2", name="Ready Barber", email="lead@fakebiz-test.dev"):
    return {
        "page_id": page_id, "name": name, "email": email,
        "email_subject": "Ready Barber's website",
        "thread_message_id": "<orig@x>",
        "site_expiry_date": date.today() - timedelta(days=2),
    }


def test_send_resurrection_emails_dry_run_does_not_send(monkeypatch):
    monkeypatch.setattr(gse, "DRY_RUN", True)
    monkeypatch.setattr(gse, "get_resurrection_candidates", lambda: [_resurrection_lead()])

    def should_not_send(*a, **k):
        raise AssertionError("send_email should not be called in dry run")
    monkeypatch.setattr(gse, "send_email", should_not_send)

    def should_not_mark(*a, **k):
        raise AssertionError("mark_resurrection_sent should not be called in dry run")
    monkeypatch.setattr(gse, "mark_resurrection_sent", should_not_mark)

    run_calls = []
    leads = gse.send_resurrection_emails(run_calls)
    assert len(leads) == 1
    assert ("resurrection_dry_run", "page-2", True) in run_calls


def test_send_resurrection_emails_live_sends_and_marks_sent(monkeypatch):
    monkeypatch.setattr(gse, "DRY_RUN", False)
    monkeypatch.setattr(gse, "get_resurrection_candidates", lambda: [_resurrection_lead()])
    monkeypatch.setattr(gse, "send_email", lambda *a, **k: (True, ""))

    marked = []
    monkeypatch.setattr(gse, "mark_resurrection_sent", lambda page_id: marked.append(page_id) or True)

    run_calls = []
    gse.send_resurrection_emails(run_calls)

    assert marked == ["page-2"]
    assert ("resurrection_sent", "page-2", True) in run_calls


def test_send_resurrection_emails_skips_lead_with_no_email(monkeypatch):
    monkeypatch.setattr(gse, "DRY_RUN", False)
    lead = _resurrection_lead(email=None)
    monkeypatch.setattr(gse, "get_resurrection_candidates", lambda: [lead])

    def should_not_send(*a, **k):
        raise AssertionError("send_email should not be called for a lead with no email")
    monkeypatch.setattr(gse, "send_email", should_not_send)

    run_calls = []
    gse.send_resurrection_emails(run_calls)
    assert run_calls == []


def test_send_resurrection_emails_send_failure_does_not_mark_sent(monkeypatch):
    monkeypatch.setattr(gse, "DRY_RUN", False)
    monkeypatch.setattr(gse, "get_resurrection_candidates", lambda: [_resurrection_lead()])
    monkeypatch.setattr(gse, "send_email", lambda *a, **k: (False, "smtp error"))

    def should_not_mark(*a, **k):
        raise AssertionError("mark_resurrection_sent should not run after a send failure")
    monkeypatch.setattr(gse, "mark_resurrection_sent", should_not_mark)

    run_calls = []
    gse.send_resurrection_emails(run_calls)
    assert ("resurrection_send_failed", "page-2", False) in run_calls


# --- get_lapsed_live_leads() / get_resurrection_candidates(): Notion filter shape ---

def test_get_lapsed_live_leads_queries_live_and_expiry_before_today(monkeypatch):
    captured = {}

    class FakeResponse:
        status_code = 200
        def json(self):
            return {"results": [], "has_more": False, "next_cursor": None}

    def fake_post(url, headers=None, json=None):
        captured["body"] = json
        return FakeResponse()

    monkeypatch.setattr(gse.requests, "post", fake_post)
    gse.get_lapsed_live_leads()

    conditions = captured["body"]["filter"]["and"]
    assert {"property": "Ghost Site Status", "select": {"equals": "Live"}} in conditions
    assert any(c.get("property") == "Site Expiry Date" and "on_or_before" in c.get("date", {}) for c in conditions)


def test_get_resurrection_candidates_queries_expired_unsent_and_stale(monkeypatch):
    captured = {}

    class FakeResponse:
        status_code = 200
        def json(self):
            return {"results": [], "has_more": False, "next_cursor": None}

    def fake_post(url, headers=None, json=None):
        captured["body"] = json
        return FakeResponse()

    monkeypatch.setattr(gse.requests, "post", fake_post)
    gse.get_resurrection_candidates()

    conditions = captured["body"]["filter"]["and"]
    assert {"property": "Ghost Site Status", "select": {"equals": "Expired"}} in conditions
    assert {"property": "Resurrection Sent Date", "date": {"is_empty": True}} in conditions
    assert any(c.get("property") == "Site Expiry Date" and "before" in c.get("date", {}) for c in conditions)


# --- mark_expired() / mark_resurrection_sent(): Notion write shape ---

def test_mark_expired_clears_url_and_sets_status(monkeypatch):
    captured = {}

    class FakeResponse:
        status_code = 200

    def fake_patch(url, headers=None, json=None):
        captured["url"] = url
        captured["payload"] = json
        return FakeResponse()

    monkeypatch.setattr(gse.requests, "patch", fake_patch)
    result = gse.mark_expired("page-1")

    assert result is True
    assert captured["url"].endswith("/pages/page-1")
    props = captured["payload"]["properties"]
    assert props["Ghost Site Status"]["select"]["name"] == "Expired"
    assert props["Ghost Site URL"]["url"] is None


def test_mark_resurrection_sent_sets_todays_date(monkeypatch):
    captured = {}

    class FakeResponse:
        status_code = 200

    def fake_patch(url, headers=None, json=None):
        captured["payload"] = json
        return FakeResponse()

    monkeypatch.setattr(gse.requests, "patch", fake_patch)
    gse.mark_resurrection_sent("page-2")

    assert captured["payload"]["properties"]["Resurrection Sent Date"]["date"]["start"] == date.today().isoformat()


# --- load_resurrection_template(): Sprint 22, file-backed copy ---

def test_load_resurrection_template_reads_real_file():
    text = gse.load_resurrection_template()
    assert "[BUSINESS_NAME]" in text


def test_load_resurrection_template_reflects_file_edits(tmp_path, monkeypatch):
    monkeypatch.setattr(gse, "EMAIL_TEMPLATES_DIR", str(tmp_path))
    (tmp_path / "resurrection.md").write_text("Edited resurrection copy for {business_name}.\n")
    assert gse.load_resurrection_template() == "Edited resurrection copy for {business_name}."


def test_send_resurrection_emails_formats_business_name(monkeypatch):
    monkeypatch.setattr(gse, "DRY_RUN", False)
    monkeypatch.setattr(gse, "get_resurrection_candidates", lambda: [_resurrection_lead(name="Formatted Barber Co")])
    monkeypatch.setattr(gse, "mark_resurrection_sent", lambda page_id: True)

    captured = {}
    monkeypatch.setattr(gse, "send_email", lambda to, subject, body, in_reply_to=None: (captured.setdefault("body", body), (True, ""))[-1])

    gse.send_resurrection_emails([])
    assert "Formatted Barber Co" in captured["body"]
