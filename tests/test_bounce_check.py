#!/usr/bin/env python3
"""
Tests for scripts/bounce_check.py.

A real cold-email follow-up to a real lead (Example Barber Shop)
genuinely bounced (their mail server timed out), but cold_email.py has no
way to know that — it advances Contact Status the moment send_email()
returns success from the SMTP handoff, which says nothing about whether
the recipient's server ever accepted it. This script reads Gmail's own
bounce notices and marks the affected lead Lead Lost instead of letting it
silently continue through the whole follow-up cadence as if delivered.

Covers:
  - extract_bounced_recipient() pulls the failed address out of Gmail's
    real bounce wording, verified against the exact raw text captured
    from a real bounce.
  - Only "(Failure)" bounces (not "(Delay)") from the real
    mailer-daemon sender are acted on.
  - Dedup by RFC Message-ID (same fragility class already fixed in
    inbox_monitoring.py 2026-08-26 — himalaya's envelope "id" is a
    folder-scoped IMAP UID, not stable identity).
  - A bounce for an unknown email, or a lead already Lead Lost, is a
    no-op.
  - A bounce for a real, active lead marks it Lead Lost and notifies
    Cyril (dev/test prints instead of sending, same pattern as every
    other notification in this codebase).
"""

import os
import sys

import pytest

SCRIPTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
sys.path.insert(0, SCRIPTS_DIR)

import bounce_check as bc  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_processed_file(tmp_path, monkeypatch):
    monkeypatch.setattr(bc, "PROCESSED_FILE", str(tmp_path / "processed_bounces.json"))


# --- extract_bounced_recipient(): real Gmail bounce wording ---

REAL_BOUNCE_RAW = (
    "Date: Wed, 26 Aug 2026 10:06:41 -0700\n"
    "From: Mail Delivery Subsystem <mailer-daemon@googlemail.com>\n"
    "To: operator@example.com\n"
    "Subject: Delivery Status Notification (Failure)\n"
    "\n"
    "[4] text/plain (603 B)\n"
    "    Content-Type: text/plain; charset=UTF-8\n"
    "\n"
    "** Message not delivered **\n"
    "\n"
    "There was a problem delivering your message to info@examplebarbershop.test. "
    "See the technical details below.\n"
    "\n"
    "Learn more here: https://support.google.com/mail/answer/7720\n"
    "\n"
    "The response was:\n"
    "\n"
    "The recipient server did not accept our requests to connect.\n"
)


def test_extract_bounced_recipient_from_real_gmail_wording():
    assert bc.extract_bounced_recipient(REAL_BOUNCE_RAW) == "info@examplebarbershop.test"


def test_extract_bounced_recipient_returns_none_when_no_match():
    assert bc.extract_bounced_recipient("nothing relevant here") is None


def test_extract_bounced_recipient_handles_none_input():
    assert bc.extract_bounced_recipient(None) is None


# --- main(): sender/subject filtering, dedup, and the Lead Lost + notify path ---

def _envelope(msg_id, sender_email, subject, message_id=None):
    env = {"id": msg_id, "from": [{"email": sender_email}], "subject": subject}
    if message_id:
        env["message-id"] = message_id
    return env


def test_main_skips_non_bounce_sender(monkeypatch):
    monkeypatch.setattr(bc, "list_inbox", lambda: [
        _envelope("1", "someone@fakebiz-test.dev", "Delivery Status Notification (Failure)"),
    ])

    def read_should_not_be_called(local_id):
        raise AssertionError("read_message_raw() called for a non-bounce-sender message")

    monkeypatch.setattr(bc, "read_message_raw", read_should_not_be_called)
    result = bc.main()
    assert result["notes"] == "1 inbox messages checked, 0 new bounce(s) processed"


def test_main_skips_delay_subject_from_real_bounce_sender(monkeypatch):
    monkeypatch.setattr(bc, "list_inbox", lambda: [
        _envelope("2", "mailer-daemon@googlemail.com", "Delivery Status Notification (Delay)"),
    ])

    def read_should_not_be_called(local_id):
        raise AssertionError("read_message_raw() called for a Delay notification")

    monkeypatch.setattr(bc, "read_message_raw", read_should_not_be_called)
    result = bc.main()
    assert result["notes"] == "1 inbox messages checked, 0 new bounce(s) processed"


def test_main_dedups_by_rfc_message_id_not_local_uid(monkeypatch):
    bc.save_processed_ids({"stable-rfc-id@mx.google.com"})
    monkeypatch.setattr(bc, "list_inbox", lambda: [
        _envelope("999-new-uid", "mailer-daemon@googlemail.com",
                  "Delivery Status Notification (Failure)",
                  message_id="stable-rfc-id@mx.google.com"),
    ])

    def read_should_not_be_called(local_id):
        raise AssertionError("read_message_raw() called for a Message-ID already processed")

    monkeypatch.setattr(bc, "read_message_raw", read_should_not_be_called)
    bc.main()  # must not raise


def test_main_skips_bounce_for_unknown_email(monkeypatch):
    monkeypatch.setattr(bc, "list_inbox", lambda: [
        _envelope("3", "mailer-daemon@googlemail.com", "Delivery Status Notification (Failure)",
                  message_id="msg-3@mx.google.com"),
    ])
    monkeypatch.setattr(bc, "read_message_raw", lambda local_id: REAL_BOUNCE_RAW)
    monkeypatch.setattr(bc, "find_lead_by_email", lambda email: None)

    def mark_should_not_be_called(page_id):
        raise AssertionError("mark_lead_lost() called for an unknown email")

    monkeypatch.setattr(bc, "mark_lead_lost", mark_should_not_be_called)
    result = bc.main()
    assert result["notes"] == "1 inbox messages checked, 0 new bounce(s) processed"


def test_main_skips_lead_already_lost(monkeypatch):
    monkeypatch.setattr(bc, "list_inbox", lambda: [
        _envelope("4", "mailer-daemon@googlemail.com", "Delivery Status Notification (Failure)",
                  message_id="msg-4@mx.google.com"),
    ])
    monkeypatch.setattr(bc, "read_message_raw", lambda local_id: REAL_BOUNCE_RAW)
    monkeypatch.setattr(bc, "find_lead_by_email", lambda email: {
        "page_id": "page-1", "name": "Example Barber Shop", "contact_status": "Lead Lost",
    })

    def mark_should_not_be_called(page_id):
        raise AssertionError("mark_lead_lost() called for an already-Lead-Lost lead")

    monkeypatch.setattr(bc, "mark_lead_lost", mark_should_not_be_called)
    result = bc.main()
    assert result["notes"] == "1 inbox messages checked, 0 new bounce(s) processed"


def test_main_marks_active_lead_lost_and_notifies(monkeypatch):
    monkeypatch.setattr(bc, "list_inbox", lambda: [
        _envelope("5", "mailer-daemon@googlemail.com", "Delivery Status Notification (Failure)",
                  message_id="msg-5@mx.google.com"),
    ])
    monkeypatch.setattr(bc, "read_message_raw", lambda local_id: REAL_BOUNCE_RAW)
    monkeypatch.setattr(bc, "find_lead_by_email", lambda email: {
        "page_id": "page-1", "name": "Example Barber Shop", "contact_status": "Follow 3",
    })

    marked = []
    monkeypatch.setattr(bc, "mark_lead_lost", lambda page_id: marked.append(page_id) or True)

    notified = []
    monkeypatch.setattr(bc, "send_bounce_notification", lambda name, email: notified.append((name, email)) or True)

    result = bc.main()
    assert marked == ["page-1"]
    assert notified == [("Example Barber Shop", "info@examplebarbershop.test")]
    assert result["notes"] == "1 inbox messages checked, 1 new bounce(s) processed"


def test_main_saves_rfc_message_id_as_the_dedup_key(monkeypatch):
    monkeypatch.setattr(bc, "list_inbox", lambda: [
        _envelope("6", "mailer-daemon@googlemail.com", "Delivery Status Notification (Failure)",
                  message_id="real-message-id@mx.google.com"),
    ])
    monkeypatch.setattr(bc, "read_message_raw", lambda local_id: REAL_BOUNCE_RAW)
    monkeypatch.setattr(bc, "find_lead_by_email", lambda email: None)

    bc.main()
    assert bc.load_processed_ids() == {"real-message-id@mx.google.com"}


# --- send_bounce_notification(): dev/test prints, production sends for real ---

def test_send_bounce_notification_prints_only_outside_production(monkeypatch, capsys):
    monkeypatch.setattr(bc.hp_env, "IS_PRODUCTION", False)

    def subprocess_should_not_be_called(*a, **k):
        raise AssertionError("himalaya invoked outside production")

    monkeypatch.setattr(bc.subprocess, "run", subprocess_should_not_be_called)
    result = bc.send_bounce_notification("Test Biz", "owner@fakebiz-test.dev")
    assert result is True
    assert "WOULD NOTIFY" in capsys.readouterr().out


def test_send_bounce_notification_sends_real_email_in_production(monkeypatch):
    monkeypatch.setattr(bc.hp_env, "IS_PRODUCTION", True)
    captured = {}

    class FakeResult:
        returncode = 0

    def fake_run(cmd, input=None, text=None, capture_output=None):
        captured["cmd"] = cmd
        captured["input"] = input
        return FakeResult()

    monkeypatch.setattr(bc.subprocess, "run", fake_run)
    result = bc.send_bounce_notification("Test Biz", "owner@fakebiz-test.dev")
    assert result is True
    assert captured["cmd"] == ["himalaya", "message", "send"]
    assert "EMAIL BOUNCED: Test Biz" in captured["input"]
    assert "owner@fakebiz-test.dev" in captured["input"]


# --- find_lead_by_email() / mark_lead_lost(): request shape ---

def test_find_lead_by_email_queries_by_email_property(monkeypatch):
    captured = {}

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"results": [{
                "id": "page-abc",
                "properties": {
                    "Business Name": {"title": [{"text": {"content": "Example Barber Shop"}}]},
                    "Contact Status": {"select": {"name": "Follow 3"}},
                },
            }]}

    def fake_post(url, headers=None, json=None):
        captured["payload"] = json
        return FakeResponse()

    monkeypatch.setattr(bc.requests, "post", fake_post)
    lead = bc.find_lead_by_email("info@examplebarbershop.test")
    assert lead == {"page_id": "page-abc", "name": "Example Barber Shop", "contact_status": "Follow 3"}
    assert captured["payload"]["filter"] == {"property": "Email", "email": {"equals": "info@examplebarbershop.test"}}


def test_mark_lead_lost_sets_contact_status(monkeypatch):
    captured = {}

    class FakeResponse:
        status_code = 200

    def fake_patch(url, headers=None, json=None):
        captured["payload"] = json
        return FakeResponse()

    monkeypatch.setattr(bc.requests, "patch", fake_patch)
    assert bc.mark_lead_lost("page-abc") is True
    assert captured["payload"]["properties"]["Contact Status"]["select"]["name"] == "Lead Lost"
