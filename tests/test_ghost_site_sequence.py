#!/usr/bin/env python3
"""
Tests for scripts/ghost_site_sequence.py — Sprint 18 (Preview Sequence Sender).

Covers Sprint 18's AC:
  - A TEST lead with a live site gets Sequence Day incremented and the
    correct day's template sent when one exists.
  - A reply mid-sequence is handled by inbox_monitoring.py (Sprint 6) —
    tested there, not here; this file covers this script's own half:
    it must stop touching a lead the moment Ghost Site Status is no
    longer Selected/Live (i.e. once inbox_monitoring.py has set it to
    Replied, this script's query naturally excludes it).
  - The full 7-day window: days 1/4/5/6/7 send, days 2/3 increment
    silently with no template.

Design decisions confirmed with Cyril (2026-08-12):
  - This script itself performs the Selected -> Live transition and sets
    Site Expiry Date = today+7 when it starts a new lead's sequence,
    since Sprint 17's builder automation doesn't exist yet to do it.
  - Day 4's copy has no "pick 2 features" upsell menu — everything shown
    is already included free.
"""

import os
import sys
from datetime import date, timedelta

import pytest

SCRIPTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
sys.path.insert(0, SCRIPTS_DIR)

import ghost_site_sequence as gs  # noqa: E402


def make_lead(**overrides):
    lead = {
        "page_id": "page-123",
        "name": "Test Barber Co",
        "email": "lead@fakebiz-test.dev",
        "ghost_site_status": "Selected",
        "ghost_site_url": "https://testbarberco-fake.example.com",
        "sequence_day": 0,
        "site_expiry_date": None,
        "thread_message_id": "<original-abc123@example.com>",
        "email_subject": "Test Barber Co's website",
        "last_sequence_action_date": None,
    }
    lead.update(overrides)
    return lead


# --- load_template() / format_template(): the 5-day content contract ---

@pytest.mark.parametrize("day", [1, 4, 5, 6, 7])
def test_load_template_exists_for_sequence_days(day):
    assert gs.load_template(day) is not None


@pytest.mark.parametrize("day", [2, 3])
def test_load_template_missing_for_non_sequence_days(day):
    assert gs.load_template(day) is None


def test_format_template_substitutes_all_placeholders():
    text = "Built [BUSINESS_NAME] this: [URL], live until [EXPIRY_DATE]"
    result = gs.format_template(text, "Test Co", "https://test.example", date(2026, 8, 19))
    assert result == "Built Test Co this: https://test.example, live until August 19"


def test_day4_template_has_no_pick_two_upsell_language():
    """Cyril's call: no paid feature-menu upsell — everything's included free."""
    text = gs.load_template(4)
    lowered = text.lower()
    assert "pick 2" not in lowered
    assert "choose 2" not in lowered
    assert "already included" in lowered


# --- get_active_leads(): filter shape ---

def test_get_active_leads_filters_selected_or_live(monkeypatch):
    captured = {}

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"results": [], "has_more": False, "next_cursor": None}

    def fake_post(url, headers=None, json=None):
        captured["payload"] = json
        return FakeResponse()

    monkeypatch.setattr(gs.requests, "post", fake_post)
    gs.get_active_leads()

    conditions = captured["payload"]["filter"]["or"]
    assert {"property": "Ghost Site Status", "select": {"equals": "Selected"}} in conditions
    assert {"property": "Ghost Site Status", "select": {"equals": "Live"}} in conditions


# --- main(): starting a new sequence ---

def _run_main_with_leads(monkeypatch, leads, dry_run=False):
    monkeypatch.setattr(gs, "DRY_RUN", dry_run)
    monkeypatch.setattr(gs, "get_active_leads", lambda: list(leads))

    sent = []

    def fake_send_email(to_address, subject, body, in_reply_to=None):
        sent.append({"to": to_address, "subject": subject, "body": body, "in_reply_to": in_reply_to})
        return True, ""

    monkeypatch.setattr(gs, "send_email", fake_send_email)

    patched = []

    def fake_patch(url, headers=None, json=None):
        patched.append({"url": url, "properties": json["properties"]})

        class FakeResponse:
            status_code = 200

        return FakeResponse()

    monkeypatch.setattr(gs.requests, "patch", fake_patch)

    gs.main()
    return sent, patched


def test_new_selected_lead_with_url_starts_sequence(monkeypatch):
    lead = make_lead(ghost_site_status="Selected", ghost_site_url="https://testbarberco-fake.example.com")
    sent, patched = _run_main_with_leads(monkeypatch, [lead])

    assert len(sent) == 1
    assert "testbarberco-fake.example.com" in sent[0]["body"]
    assert sent[0]["in_reply_to"] == "<original-abc123@example.com>"

    props = patched[0]["properties"]
    assert props["Ghost Site Status"]["select"]["name"] == "Live"
    assert props["Sequence Day"]["number"] == 1
    assert props["Site Expiry Date"]["date"]["start"] == (date.today() + timedelta(days=7)).isoformat()


def test_new_selected_lead_starting_sequence_sets_last_action_date(monkeypatch):
    lead = make_lead(ghost_site_status="Selected", ghost_site_url="https://testbarberco-fake.example.com")
    sent, patched = _run_main_with_leads(monkeypatch, [lead])

    props = patched[0]["properties"]
    assert props["Last Sequence Action Date"]["date"]["start"] == date.today().isoformat()


# --- daily self-gate (2026-08-17): safe to call hourly via orchestrator.py ---

def test_lead_already_advanced_today_is_skipped_entirely(monkeypatch):
    """The real reason this is safe to call every hour, not just once a
    day — without this, a Live lead would run through its whole 7-day
    sequence in 7 hours instead of 7 days."""
    lead = make_lead(
        ghost_site_status="Live", sequence_day=1,
        last_sequence_action_date=date.today(),
    )
    sent, patched = _run_main_with_leads(monkeypatch, [lead])
    assert sent == []
    assert patched == []


def test_lead_advanced_yesterday_is_processed_today(monkeypatch):
    lead = make_lead(
        ghost_site_status="Live", sequence_day=1,
        last_sequence_action_date=date.today() - timedelta(days=1),
    )
    sent, patched = _run_main_with_leads(monkeypatch, [lead])
    assert len(patched) == 1
    assert patched[0]["properties"]["Sequence Day"]["number"] == 2


def test_lead_never_advanced_before_is_processed(monkeypatch):
    lead = make_lead(ghost_site_status="Selected", last_sequence_action_date=None)
    sent, patched = _run_main_with_leads(monkeypatch, [lead])
    assert len(patched) == 1


def test_advance_sequence_sets_last_action_date_even_on_silent_days(monkeypatch):
    """Days 2/3 have no template and send nothing, but the gate still
    needs to record "touched today" or the counter would advance
    multiple times within the same day too."""
    lead = make_lead(ghost_site_status="Live", sequence_day=1, last_sequence_action_date=date.today() - timedelta(days=1))
    sent, patched = _run_main_with_leads(monkeypatch, [lead])
    props = patched[0]["properties"]
    assert props["Sequence Day"]["number"] == 2  # day 2 has no template
    assert props["Last Sequence Action Date"]["date"]["start"] == date.today().isoformat()


def test_selected_lead_without_url_is_skipped(monkeypatch):
    lead = make_lead(ghost_site_status="Selected", ghost_site_url=None)
    sent, patched = _run_main_with_leads(monkeypatch, [lead])
    assert sent == []
    assert patched == []


def test_dry_run_never_sends_or_writes(monkeypatch):
    lead = make_lead(ghost_site_status="Selected", ghost_site_url="https://testbarberco-fake.example.com")

    def send_should_not_be_called(*a, **k):
        raise AssertionError("send_email() called in DRY_RUN mode")

    monkeypatch.setattr(gs, "DRY_RUN", True)
    monkeypatch.setattr(gs, "get_active_leads", lambda: [lead])
    monkeypatch.setattr(gs, "send_email", send_should_not_be_called)

    def patch_should_not_be_called(*a, **k):
        raise AssertionError("Notion patch called in DRY_RUN mode")

    monkeypatch.setattr(gs.requests, "patch", patch_should_not_be_called)

    gs.main()  # must not raise


# --- main(): mid-sequence advancement ---

def test_live_lead_advances_and_sends_on_a_template_day(monkeypatch):
    lead = make_lead(ghost_site_status="Live", sequence_day=3, site_expiry_date=date(2026, 8, 19))
    sent, patched = _run_main_with_leads(monkeypatch, [lead])

    assert len(sent) == 1  # day 4 has a template
    props = patched[0]["properties"]
    assert props["Sequence Day"]["number"] == 4


def test_live_lead_advances_silently_on_a_non_template_day(monkeypatch):
    lead = make_lead(ghost_site_status="Live", sequence_day=1, site_expiry_date=date(2026, 8, 19))
    sent, patched = _run_main_with_leads(monkeypatch, [lead])

    assert sent == []  # day 2 has no template
    props = patched[0]["properties"]
    assert props["Sequence Day"]["number"] == 2


def test_live_lead_uses_stored_expiry_date_not_recomputed(monkeypatch):
    """The expiry date shown in Day 6 (the only mid-sequence template that
    references it) must be the real fixed date set on Day 1, not a
    freshly recomputed today+N that could drift."""
    stored_expiry = date(2026, 8, 25)  # deliberately NOT what today+1 would compute to
    lead = make_lead(ghost_site_status="Live", sequence_day=5, site_expiry_date=stored_expiry)
    sent, _patched = _run_main_with_leads(monkeypatch, [lead])

    assert stored_expiry.strftime("%B %-d") in sent[0]["body"]


def test_replied_lead_is_never_processed(monkeypatch):
    """Once inbox_monitoring.py sets Ghost Site Status to Replied, this
    script's own query (Selected/Live only) naturally excludes it — this
    test confirms main() doesn't touch a lead that slips through with
    that status regardless."""
    lead = make_lead(ghost_site_status="Replied")
    sent, patched = _run_main_with_leads(monkeypatch, [lead])
    assert sent == []
    assert patched == []
