#!/usr/bin/env python3
"""
Tests for scripts/cold_email.py — Sprint 5 (Cold Email).

Covers Sprint 5's AC:
  - Email Subject (not Email Draft, not a dumped/embedded subject line) is
    used verbatim as the Subject header when sending.
  - A first-touch send generates a Message-ID and stores it in Thread
    Message ID.
  - A follow-up send sets In-Reply-To to the stored Message-ID.
  - Sends only fire during the hourly run matching 8am local for the
    lead's timezone (PT/MT/CT/ET).
  - --live is locked outside production: DRY_RUN mode never reaches
    Himalaya for a real recipient — "the single most important test in
    the whole rebuild."
  - (Gap 1) Every successful send updates Last Contact Date to today.

Regression test included for a real bug found during Sprint 5 review:
is_local_send_time() previously returned True unconditionally when a
lead had no resolvable Timezone, meaning it would send at ANY hour
rather than being blocked — the opposite of this sprint's stated goal.

This script combines Sprint 5 (Cold Email) and Sprint 7 (Follow-Up) in
one file, same pattern as prior combined scripts. Only Sprint-5-relevant
behavior is tested here (subject/threading/timezone-gate/--live/Gap 1);
follow-up cadence correctness (Day 3/7/14/28, stop-on-reply, Track B
skip-rule) is Sprint 7's job to test when that sprint starts.
"""

import datetime as real_datetime
import os
import sys

import pytest

SCRIPTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
sys.path.insert(0, SCRIPTS_DIR)

import cold_email as ce  # noqa: E402


@pytest.fixture(autouse=True)
def not_paused_by_default(monkeypatch):
    """cold_email_paused is a real config.yaml value (flipped on/off for
    real mailbox warmup periods) — tests must not inherit whatever it
    happens to be set to in the live config, so every test gets a clean
    'not paused' baseline unless it explicitly overrides this itself."""
    monkeypatch.setattr(ce.hp_config, "COLD_EMAIL_PAUSED", False)


def make_lead(**overrides):
    lead = {
        "page_id": "page-123",
        "name": "Test Salon",
        "email": "lead@fakebiz-test.dev",
        "contact_status": "Not Contacted",
        "reply_status": None,
        "last_contact_date": None,
        "emails_sent": 0,
        "email_draft": "Hi there, this is the draft body.",
        "email_subject": "Custom Subject Line",
        "thread_message_id": "",
        "timezone": "PT",
        "ghost_site_status": None,
    }
    lead.update(overrides)
    return lead


# --- is_local_send_time(): AC "sends only fire at local 8am", plus the
# no-timezone regression fix ---

class _FrozenAt8am:
    @classmethod
    def now(cls, tz=None):
        return real_datetime.datetime(2026, 6, 15, 8, 0, tzinfo=tz)


class _FrozenAt2pm:
    @classmethod
    def now(cls, tz=None):
        return real_datetime.datetime(2026, 6, 15, 14, 0, tzinfo=tz)


class _FrozenAt9am:
    @classmethod
    def now(cls, tz=None):
        return real_datetime.datetime(2026, 6, 15, 9, 0, tzinfo=tz)


class _FrozenAt10am:
    @classmethod
    def now(cls, tz=None):
        return real_datetime.datetime(2026, 6, 15, 10, 0, tzinfo=tz)


@pytest.mark.parametrize("tz_abbr", ["PT", "MT", "CT", "ET"])
def test_is_local_send_time_true_at_local_8am(monkeypatch, tz_abbr):
    monkeypatch.setattr(ce, "datetime", _FrozenAt8am)
    assert ce.is_local_send_time(tz_abbr) is True


@pytest.mark.parametrize("tz_abbr", ["PT", "MT", "CT", "ET"])
def test_is_local_send_time_true_within_widened_window(monkeypatch, tz_abbr):
    """Window is now [8am, 10am) local, not just the exact 8am hour —
    the send-timing-gap fix (2026-08-25): a lead no longer needs the
    orchestrator to happen to run during its one exact local hour."""
    monkeypatch.setattr(ce, "datetime", _FrozenAt9am)
    assert ce.is_local_send_time(tz_abbr) is True


@pytest.mark.parametrize("tz_abbr", ["PT", "MT", "CT", "ET"])
def test_is_local_send_time_false_at_window_end_boundary(monkeypatch, tz_abbr):
    """10am is the configured window end and is exclusive."""
    monkeypatch.setattr(ce, "datetime", _FrozenAt10am)
    assert ce.is_local_send_time(tz_abbr) is False


@pytest.mark.parametrize("tz_abbr", ["PT", "MT", "CT", "ET"])
def test_is_local_send_time_false_off_hour(monkeypatch, tz_abbr):
    monkeypatch.setattr(ce, "datetime", _FrozenAt2pm)
    assert ce.is_local_send_time(tz_abbr) is False


def test_is_local_send_time_false_when_no_timezone_set_regression(monkeypatch):
    """Regression: previously returned True unconditionally for a blank
    Timezone, sending at any hour instead of waiting for a known local 8am."""
    monkeypatch.setattr(ce, "datetime", _FrozenAt8am)
    assert ce.is_local_send_time(None) is False
    assert ce.is_local_send_time("") is False


def test_is_local_send_time_false_for_unrecognized_timezone(monkeypatch):
    monkeypatch.setattr(ce, "datetime", _FrozenAt8am)
    assert ce.is_local_send_time("GMT") is False


# --- main() live-path behavior: subject, threading, Gap 1 ---
# DRY_RUN is a module-level constant computed at import time; these tests
# override it directly to exercise the live-send branch's internal logic
# (subject selection, threading, Notion update) without touching the real
# --live/hp_env gating mechanism, which has its own dedicated test below.

def _run_main_with_lead(monkeypatch, lead, capture_send=None, capture_update=None):
    return _run_main_with_leads(monkeypatch, [lead], capture_send=capture_send, capture_update=capture_update)


def _run_main_with_leads(monkeypatch, leads, capture_send=None, capture_update=None):
    monkeypatch.setattr(ce, "DRY_RUN", False)
    monkeypatch.setattr(ce, "get_box1_leads", lambda: list(leads))
    monkeypatch.setattr(ce, "is_local_send_time", lambda tz: True)

    if capture_send is None:
        capture_send = []

    def fake_send_email(to_address, subject, body, message_id=None, in_reply_to=None):
        capture_send.append({
            "to": to_address, "subject": subject, "body": body,
            "message_id": message_id, "in_reply_to": in_reply_to,
        })
        return True, ""

    monkeypatch.setattr(ce, "send_email", fake_send_email)

    if capture_update is None:
        capture_update = []

    def fake_update(page_id, next_status, emails_sent_count, thread_message_id=None):
        capture_update.append({
            "page_id": page_id, "next_status": next_status,
            "emails_sent_count": emails_sent_count, "thread_message_id": thread_message_id,
        })
        return True

    monkeypatch.setattr(ce, "update_notion_after_send", fake_update)
    monkeypatch.setattr(sys, "argv", ["cold_email.py"])

    ce.main()
    return capture_send, capture_update


def test_first_touch_uses_email_subject_verbatim(monkeypatch):
    lead = make_lead(contact_status="Not Contacted", email_subject="Custom Subject Line")
    sent, _ = _run_main_with_lead(monkeypatch, lead)
    assert len(sent) == 1
    assert sent[0]["subject"] == "Custom Subject Line"


def test_first_touch_generates_and_stores_message_id(monkeypatch):
    lead = make_lead(contact_status="Not Contacted")
    sent, updated = _run_main_with_lead(monkeypatch, lead)
    assert sent[0]["message_id"] is not None
    assert updated[0]["thread_message_id"] == sent[0]["message_id"]


def test_first_touch_renders_tracking_link_using_real_page_id(monkeypatch):
    """Email Draft is generated at discovery time, before the Notion page
    (and its ID) exists — it still has a literal [TRACKING_LINK] token in
    it, rendered here at send time now that the real page ID is known."""
    lead = make_lead(
        page_id="page-xyz-789",
        contact_status="Not Contacted",
        email_draft="Take a look: [TRACKING_LINK]",
    )
    sent, _ = _run_main_with_lead(monkeypatch, lead)
    assert sent[0]["body"] == "Take a look: https://track.example.com/c?p=page-xyz-789"


def test_followup_renders_tracking_link_using_real_page_id(monkeypatch):
    lead = make_lead(
        page_id="page-followup-456",
        contact_status="Follow 1",
        last_contact_date=real_datetime.date.today() - real_datetime.timedelta(days=3),
    )
    monkeypatch.setattr(ce, "load_followup_text", lambda n: "Example here: [TRACKING_LINK]")
    sent, _ = _run_main_with_lead(monkeypatch, lead)
    assert sent[0]["body"] == "Example here: https://track.example.com/c?p=page-followup-456"


def test_followup_sets_in_reply_to_stored_thread_message_id(monkeypatch):
    lead = make_lead(
        contact_status="Follow 1",
        thread_message_id="<original-abc123@example.com>",
        last_contact_date=real_datetime.date.today() - real_datetime.timedelta(days=3),
    )
    sent, _ = _run_main_with_lead(monkeypatch, lead)
    assert len(sent) == 1
    assert sent[0]["in_reply_to"] == "<original-abc123@example.com>"
    # A follow-up reuses the existing thread — it must not mint a new Message-ID.
    assert sent[0]["message_id"] is None


def test_gap1_successful_send_updates_last_contact_date_to_today(monkeypatch):
    captured = {}

    class FakeResponse:
        status_code = 200

    def fake_patch(url, headers=None, json=None):
        captured["payload"] = json
        return FakeResponse()

    monkeypatch.setattr(ce.requests, "patch", fake_patch)
    ce.update_notion_after_send("page-123", "Follow 1", 0, thread_message_id="<x@y>")

    assert captured["payload"]["properties"]["Last Contact Date"]["date"]["start"] == real_datetime.date.today().isoformat()


# --- --live lock: "the single most important test in the whole rebuild" ---

def test_dry_run_never_calls_send_email(monkeypatch):
    lead = make_lead(contact_status="Not Contacted")

    monkeypatch.setattr(ce, "DRY_RUN", True)
    monkeypatch.setattr(ce, "get_box1_leads", lambda: [lead])
    monkeypatch.setattr(ce, "is_local_send_time", lambda tz: True)

    def send_email_should_not_be_called(*args, **kwargs):
        raise AssertionError("send_email() was called in DRY_RUN mode — --live lock failed")

    monkeypatch.setattr(ce, "send_email", send_email_should_not_be_called)
    monkeypatch.setattr(sys, "argv", ["cold_email.py"])

    ce.main()  # must not raise


def test_dry_run_never_invokes_himalaya_subprocess(monkeypatch):
    """Even one level deeper: confirm the actual subprocess call inside
    send_email() itself is never reached from a DRY_RUN main() run."""
    lead = make_lead(contact_status="Not Contacted")

    monkeypatch.setattr(ce, "DRY_RUN", True)
    monkeypatch.setattr(ce, "get_box1_leads", lambda: [lead])
    monkeypatch.setattr(ce, "is_local_send_time", lambda tz: True)

    def subprocess_should_not_run(*args, **kwargs):
        raise AssertionError("subprocess.run() (Himalaya) was called in DRY_RUN mode")

    monkeypatch.setattr(ce.subprocess, "run", subprocess_should_not_run)
    monkeypatch.setattr(sys, "argv", ["cold_email.py"])

    ce.main()  # must not raise


# --- Sprint 7 (Follow-Up): cadence, stop-on-reply, Track B skip-rule ---
# cold_email.py combines Sprint 5 + Sprint 7 in one file/loop; these tests
# cover the AC that's specifically Sprint 7's: the Day 3/7/14/28 cadence,
# stop-on-reply, and the Ghost Site Status stub for Track B.

FIXED_TODAY = real_datetime.date(2026, 6, 15)


class _FrozenToday:
    @classmethod
    def today(cls):
        return FIXED_TODAY


@pytest.mark.parametrize("status,delta", [
    ("Follow 1", 3),
    ("Follow 2", 4),
    ("Follow 3", 7),
    ("Follow 4", 14),
])
def test_followup_sends_at_exactly_its_delta(monkeypatch, status, delta):
    monkeypatch.setattr(ce, "date", _FrozenToday)
    lead = make_lead(
        contact_status=status,
        last_contact_date=FIXED_TODAY - real_datetime.timedelta(days=delta),
    )
    sent, _ = _run_main_with_lead(monkeypatch, lead)
    assert len(sent) == 1


@pytest.mark.parametrize("status,delta", [
    ("Follow 1", 3),
    ("Follow 2", 4),
    ("Follow 3", 7),
    ("Follow 4", 14),
])
def test_followup_does_not_send_one_day_short_of_its_delta(monkeypatch, status, delta):
    monkeypatch.setattr(ce, "date", _FrozenToday)
    lead = make_lead(
        contact_status=status,
        last_contact_date=FIXED_TODAY - real_datetime.timedelta(days=delta - 1),
    )
    sent, _ = _run_main_with_lead(monkeypatch, lead)
    assert len(sent) == 0


@pytest.mark.parametrize("reply_status", ["Interested", "Not Interested", "Unsubscribed"])
def test_stop_on_reply_blocks_followup_even_when_date_qualifies(monkeypatch, reply_status):
    monkeypatch.setattr(ce, "date", _FrozenToday)
    lead = make_lead(
        contact_status="Follow 2",
        reply_status=reply_status,
        last_contact_date=FIXED_TODAY - real_datetime.timedelta(days=30),  # well past due
    )
    sent, _ = _run_main_with_lead(monkeypatch, lead)
    assert len(sent) == 0


def test_ghost_site_status_set_skips_lead_entirely(monkeypatch):
    """Track B stub: a lead already in the Ghost Site sequence is owned by
    that sequence, not Track A follow-up, even when otherwise due to send."""
    monkeypatch.setattr(ce, "date", _FrozenToday)
    lead = make_lead(
        contact_status="Follow 1",
        ghost_site_status="Selected",
        last_contact_date=FIXED_TODAY - real_datetime.timedelta(days=3),
    )
    sent, _ = _run_main_with_lead(monkeypatch, lead)
    assert len(sent) == 0


def test_ghost_site_status_set_skips_first_touch_too(monkeypatch):
    """Skipped "entirely" per the AC — not just for follow-ups."""
    lead = make_lead(contact_status="Not Contacted", ghost_site_status="Live")
    sent, _ = _run_main_with_lead(monkeypatch, lead)
    assert len(sent) == 0


def test_no_ghost_site_status_still_sends_normally(monkeypatch):
    """Regression guard: the new field read shouldn't block ordinary leads."""
    lead = make_lead(contact_status="Not Contacted", ghost_site_status=None)
    sent, _ = _run_main_with_lead(monkeypatch, lead)
    assert len(sent) == 1


# --- Sprint 8 (Orchestrator): two-bucket quota model ---
# Follow-ups are never capped; new/first-touch leads are capped at
# MAX_NEW_SENDS (parsed from --max-new-sends, passed by the orchestrator as
# the day's remaining new-lead quota). Follow-ups are always processed
# before new leads within a single run.

def _followup_lead(name, thread_id):
    return make_lead(
        page_id=f"page-{name}",
        name=name,
        email=f"{name.lower()}@fakebiz-test.dev",
        contact_status="Follow 1",
        thread_message_id=thread_id,
        last_contact_date=real_datetime.date.today() - real_datetime.timedelta(days=3),
    )


def _new_lead(name):
    return make_lead(
        page_id=f"page-{name}",
        name=name,
        email=f"{name.lower()}@fakebiz-test.dev",
        contact_status="Not Contacted",
    )


def test_max_new_sends_caps_new_leads_but_not_followups(monkeypatch):
    leads = [_new_lead("Alpha"), _new_lead("Beta"), _followup_lead("Gamma", "<g@x>")]
    monkeypatch.setattr(sys, "argv", ["cold_email.py", "--max-new-sends", "1"])
    monkeypatch.setattr(ce, "MAX_NEW_SENDS", 1)

    sent, _ = _run_main_with_leads(monkeypatch, leads)

    assert len(sent) == 2  # 1 new lead + the 1 follow-up, uncapped
    subjects_and_bodies = [(s["subject"], s["in_reply_to"]) for s in sent]
    # The follow-up (Gamma) must be among those sent regardless of the new-lead cap.
    assert any(in_reply_to == "<g@x>" for _subject, in_reply_to in subjects_and_bodies)


def test_all_due_followups_send_even_when_new_quota_is_zero(monkeypatch):
    leads = [
        _followup_lead("Gamma", "<g@x>"),
        _followup_lead("Delta", "<d@x>"),
        _followup_lead("Epsilon", "<e@x>"),
        _new_lead("Alpha"),
    ]
    monkeypatch.setattr(ce, "MAX_NEW_SENDS", 0)

    sent, _ = _run_main_with_leads(monkeypatch, leads)

    assert len(sent) == 3  # all 3 follow-ups sent, the new lead skipped
    in_reply_tos = {s["in_reply_to"] for s in sent}
    assert in_reply_tos == {"<g@x>", "<d@x>", "<e@x>"}


def test_followups_are_sent_before_new_leads_run_out_the_cap(monkeypatch):
    """If new leads were processed first, a cap of 1 would consume the quota
    on a fresh lead before the follow-up ever got a chance in the same run
    (they don't compete for the same quota, but this confirms follow-ups
    aren't starved by list order — they always go first)."""
    leads = [_new_lead("Alpha"), _new_lead("Beta"), _followup_lead("Gamma", "<g@x>")]
    monkeypatch.setattr(ce, "MAX_NEW_SENDS", 1)

    sent, _ = _run_main_with_leads(monkeypatch, leads)

    assert sent[0]["in_reply_to"] == "<g@x>"  # follow-up processed first


def test_standalone_run_without_orchestrator_defaults_to_daily_send_limit(monkeypatch):
    """No --max-new-sends passed (e.g. run by hand) -> falls back to the
    full DAILY_SEND_LIMIT, same as before Sprint 8."""
    assert ce._parse_max_new_sends(["cold_email.py"]) == ce.DAILY_SEND_LIMIT


def test_parse_max_new_sends_reads_the_flag():
    assert ce._parse_max_new_sends(["cold_email.py", "--max-new-sends", "3"]) == 3


# --- cold_email_paused: full stop for mailbox warmup periods, added
# 2026-09-10 after confirming real cold sends were landing in spam ---

def test_paused_config_skips_entire_run_without_touching_notion(monkeypatch):
    """daily_send_limit alone can't fully pause sending — follow-ups are
    deliberately uncapped — so a real warmup needs a harder stop. Must not
    even query Notion for leads when paused, not just skip sending."""
    monkeypatch.setattr(ce.hp_config, "COLD_EMAIL_PAUSED", True)

    def should_not_be_called():
        raise AssertionError("get_box1_leads() called despite cold_email_paused being set")

    monkeypatch.setattr(ce, "get_box1_leads", should_not_be_called)
    result = ce.main()
    assert "paused" in result["notes"]


def test_not_paused_by_default_runs_normally(monkeypatch):
    monkeypatch.setattr(ce.hp_config, "COLD_EMAIL_PAUSED", False)
    monkeypatch.setattr(ce, "get_box1_leads", lambda: [])
    result = ce.main()
    assert "paused" not in result["notes"]


def test_parse_max_new_sends_never_negative():
    assert ce._parse_max_new_sends(["cold_email.py", "--max-new-sends", "-5"]) == 0


# --- load_followup_text(): Sprint 22, file-backed follow-up copy ---

@pytest.mark.parametrize("n", [1, 2, 3, 4])
def test_load_followup_text_reads_real_files(n):
    text = ce.load_followup_text(n)
    assert text  # non-empty, matches the real email_templates/followup_{n}.md content
    assert "\n\n" not in text[-2:]  # trailing blank lines stripped, not sent as-is


def test_load_followup_text_reflects_file_edits(tmp_path, monkeypatch):
    monkeypatch.setattr(ce, "FOLLOWUP_TEMPLATES_DIR", str(tmp_path))
    (tmp_path / "followup_1.md").write_text("Edited follow-up copy.\n")
    assert ce.load_followup_text(1) == "Edited follow-up copy."


# --- preview_upcoming_sends(): dashboard "what's about to happen" panel
# (2026-08-17) — must mirror main()'s exact eligibility order without
# actually sending, since it's read-only and called on every dashboard
# page load. ---

def _preview_with(monkeypatch, leads, todays_new_sends=0):
    monkeypatch.setattr(ce, "get_box1_leads", lambda: list(leads))
    monkeypatch.setattr(ce, "count_todays_new_sends", lambda: todays_new_sends)
    return ce.preview_upcoming_sends()


def test_preview_counts_ready_new_lead_at_local_8am(monkeypatch):
    monkeypatch.setattr(ce, "datetime", _FrozenAt8am)
    lead = _new_lead("Alpha")
    result = _preview_with(monkeypatch, [lead])
    assert result["stages"]["Not Contacted"]["ready"] == 1
    assert result["stages"]["Not Contacted"]["waiting_timezones"] == []


def test_preview_counts_new_lead_waiting_for_local_morning(monkeypatch):
    monkeypatch.setattr(ce, "datetime", _FrozenAt2pm)
    lead = _new_lead("Alpha")
    result = _preview_with(monkeypatch, [lead])
    assert result["stages"]["Not Contacted"]["ready"] == 0
    assert result["stages"]["Not Contacted"]["waiting_timezones"] == ["PT"]


def test_preview_due_followup_ready_at_local_8am(monkeypatch):
    monkeypatch.setattr(ce, "datetime", _FrozenAt8am)
    lead = _followup_lead("Gamma", "<g@x>")  # Follow 1, due (3 days elapsed)
    result = _preview_with(monkeypatch, [lead])
    assert result["stages"]["Follow 1"]["ready"] == 1


def test_preview_followup_not_yet_due(monkeypatch):
    monkeypatch.setattr(ce, "datetime", _FrozenAt8am)
    lead = make_lead(
        contact_status="Follow 1",
        last_contact_date=real_datetime.date.today() - real_datetime.timedelta(days=1),
    )
    result = _preview_with(monkeypatch, [lead])
    assert result["stages"]["Follow 1"]["ready"] == 0
    assert result["stages"]["Follow 1"]["not_yet_due"] == 1


def test_preview_new_lead_beyond_quota_marked_waiting_for_quota(monkeypatch):
    monkeypatch.setattr(ce, "datetime", _FrozenAt8am)
    monkeypatch.setattr(ce, "DAILY_SEND_LIMIT", 1)
    leads = [_new_lead("Alpha"), _new_lead("Beta")]
    result = _preview_with(monkeypatch, leads, todays_new_sends=0)
    assert result["stages"]["Not Contacted"]["ready"] == 1
    assert result["stages"]["Not Contacted"]["waiting_for_quota"] == 1
    assert result["new_quota_remaining"] == 0


def test_preview_quota_gate_does_not_apply_to_followups(monkeypatch):
    monkeypatch.setattr(ce, "datetime", _FrozenAt8am)
    monkeypatch.setattr(ce, "DAILY_SEND_LIMIT", 0)
    lead = _followup_lead("Gamma", "<g@x>")
    result = _preview_with(monkeypatch, [lead], todays_new_sends=0)
    assert result["stages"]["Follow 1"]["ready"] == 1


def test_preview_ghost_site_leads_excluded(monkeypatch):
    monkeypatch.setattr(ce, "datetime", _FrozenAt8am)
    lead = _new_lead("Alpha")
    lead["ghost_site_status"] = "Selected"
    result = _preview_with(monkeypatch, [lead])
    assert result["stages"] == {}


def test_preview_stopped_reply_status_excluded(monkeypatch):
    monkeypatch.setattr(ce, "datetime", _FrozenAt8am)
    lead = _new_lead("Alpha")
    lead["reply_status"] = "Not Interested"
    result = _preview_with(monkeypatch, [lead])
    assert result["stages"] == {}


def test_next_local_send_window_rolls_to_tomorrow_after_target_hour(monkeypatch):
    monkeypatch.setattr(ce, "datetime", _FrozenAt2pm)
    window = ce.next_local_send_window("PT")
    assert window.day == 16  # _FrozenAt2pm is June 15 2026, so next 8am is the 16th
    assert window.hour == ce.TARGET_LOCAL_HOUR


def test_next_local_send_window_unresolvable_timezone_returns_none():
    assert ce.next_local_send_window(None) is None
    assert ce.next_local_send_window("bogus") is None
