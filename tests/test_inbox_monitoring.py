#!/usr/bin/env python3
"""
Tests for scripts/inbox_monitoring.py — Sprint 6 (Inbox Monitoring).

Covers Sprint 6's AC:
  - classify_reply() matches expectation against known sample reply texts
    (Interested / Not Interested / Unsubscribed), and defaults to
    Interested when ambiguous or on error — "safer to over-flag than
    miss one."
  - A reply's ID already in processed_replies.json is not reprocessed or
    re-notified on the next run.
  - A reply's sender matches a CRM lead -> that lead's row updates
    correctly; an unmatched sender is skipped.
  - A positive (Interested) reply triggers an immediate flagged
    notification — this didn't exist at all before this sprint, built
    fresh as send_notification(). Gated on HP_ENV==production (Cyril's
    call): dev/test runs print instead of firing a real email, so
    processing the synthetic TEST lead's replies never spams a real inbox.
"""

import os
import sys

import pytest

SCRIPTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
sys.path.insert(0, SCRIPTS_DIR)

import inbox_monitoring as im  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_processed_file(tmp_path, monkeypatch):
    monkeypatch.setattr(im, "PROCESSED_FILE", str(tmp_path / "processed_replies.json"))


# --- extract_reply_text(): himalaya's MIME part marker line regression ---

def test_extract_reply_text_skips_himalaya_mime_part_marker():
    """Regression, found live 2026-08-27 in queue_check.py's identical copy
    of this function (a real reply was rejected as "unparseable"): every
    real inbound reply this whole pipeline has ever classified actually had
    himalaya's MIME part marker line ("[2] text/plain (192 B)") prepended
    to the text handed to classify_reply(), since extract_reply_text()'s
    single split("\n\n", 1) only skips the email headers, not the marker
    line + its own indented Content-Type/Content-Transfer-Encoding lines
    that himalaya prints before the actual body."""
    raw_output = (
        "Date: Thu, 27 Aug 2026 22:47:12 +0530\n"
        "From: Someone <owner@fakebiz-test.dev>\n"
        "To: operator@example.com\n"
        "Subject: Re: Quick question about your website\n"
        "\n"
        "[2] text/plain (192 B)\n"
        "    Content-Type: text/plain; charset=UTF-8\n"
        "    Content-Transfer-Encoding: quoted-printable\n"
        "\n"
        "Sure, tell me more!\n"
        "\n"
        "\n"
        "On Thu, Aug 27, 2026 at 10:46 PM <operator@example.com> wrote:\n"
        "\n"
        "> Quick question about your website\n"
        ">\n"
    )
    assert im.extract_reply_text(raw_output) == "Sure, tell me more!"


def _fake_llm_response(monkeypatch, text):
    # hp_llm.generate() already collapses network errors/non-200/malformed
    # responses down to None — classify_reply() only ever sees text or
    # None, so tests mock at that boundary rather than a specific HTTP
    # provider's request/response shape (2026-08-20: Ollama -> Gemini).
    monkeypatch.setattr(im.hp_llm, "generate", lambda prompt, timeout=None: text)


# --- classify_reply(): AC "matches expectation against known sample texts" ---

def test_classify_reply_unsubscribed(monkeypatch):
    _fake_llm_response(monkeypatch, "UNSUBSCRIBED")
    assert im.classify_reply("Please remove me from this list, stop emailing me.") == "Unsubscribed"


def test_classify_reply_not_interested(monkeypatch):
    _fake_llm_response(monkeypatch, "NOT_INTERESTED")
    assert im.classify_reply("No thanks, not interested.") == "Not Interested"


def test_classify_reply_interested(monkeypatch):
    _fake_llm_response(monkeypatch, "INTERESTED")
    assert im.classify_reply("Sure, tell me more!") == "Interested"


def test_classify_reply_ambiguous_defaults_to_interested(monkeypatch):
    _fake_llm_response(monkeypatch, "hmm, unclear rambling output")
    assert im.classify_reply("What is this about exactly?") == "Interested"


def test_classify_reply_defaults_to_interested_on_http_error(monkeypatch):
    _fake_llm_response(monkeypatch, None)  # hp_llm.generate() returns None on any non-200
    assert im.classify_reply("anything") == "Interested"


def test_classify_reply_defaults_to_interested_on_exception(monkeypatch):
    _fake_llm_response(monkeypatch, None)  # hp_llm.generate() returns None on any exception too
    assert im.classify_reply("anything") == "Interested"


# --- send_notification(): the new AC #4 behavior ---

def test_send_notification_prints_only_outside_production(monkeypatch, capsys):
    monkeypatch.setattr(im.hp_env, "IS_PRODUCTION", False)

    def subprocess_should_not_run(*args, **kwargs):
        raise AssertionError("subprocess.run() was called outside production")

    monkeypatch.setattr(im.subprocess, "run", subprocess_should_not_run)

    result = im.send_notification("Test Salon", "Sure, tell me more!")
    assert result is True
    assert "WOULD NOTIFY" in capsys.readouterr().out


def test_send_notification_sends_real_email_in_production(monkeypatch):
    monkeypatch.setattr(im.hp_env, "IS_PRODUCTION", True)
    captured = {}

    class FakeResult:
        returncode = 0
        stderr = ""

    def fake_run(cmd, input=None, text=None, capture_output=None):
        captured["cmd"] = cmd
        captured["input"] = input
        return FakeResult()

    monkeypatch.setattr(im.subprocess, "run", fake_run)

    result = im.send_notification("Test Salon", "Sure, tell me more!")
    assert result is True
    assert captured["cmd"] == ["himalaya", "message", "send"]
    assert "INTERESTED REPLY: Test Salon" in captured["input"]
    assert "Sure, tell me more!" in captured["input"]


# --- main(): dedup, sender-matching, and wiring notification to Interested only ---

def _envelope(msg_id, sender_email, message_id=None):
    env = {"id": msg_id, "from": [{"email": sender_email}]}
    if message_id:
        env["message-id"] = message_id
    return env


def test_main_skips_already_processed_reply(monkeypatch):
    monkeypatch.setattr(im, "load_processed_ids", lambda: {"msg-1"})
    monkeypatch.setattr(im, "list_inbox", lambda: [_envelope("msg-1", "lead@fakebiz-test.dev")])

    def find_lead_should_not_be_called(email):
        raise AssertionError("find_lead_by_email() called for an already-processed message")

    monkeypatch.setattr(im, "find_lead_by_email", find_lead_should_not_be_called)
    monkeypatch.setattr(im, "save_processed_ids", lambda ids: None)

    im.main()  # must not raise


def test_main_saves_each_message_as_processed_immediately(monkeypatch):
    """Regression, found 2026-08-18: processed_replies.json used to be
    written once, only after the whole loop finished — a crash on ANY
    message meant nothing from that run persisted, so already-handled
    messages (already resurrected, already notified) got fully
    reprocessed on the next run. This uses the REAL save_processed_ids
    (only PROCESSED_FILE is patched, by the isolated_processed_file
    fixture) so it verifies the actual file on disk, not a mock."""
    # load_processed_ids/save_processed_ids are NOT mocked here (unlike
    # every other main() test) — the isolated_processed_file fixture points
    # PROCESSED_FILE at a fresh tmp path, so this exercises the real
    # read/write round-trip the fix depends on.
    monkeypatch.setattr(im, "list_inbox", lambda: [
        _envelope("msg-1", "owner@fakebiz-test.dev"),
        _envelope("msg-2", "owner2@fakebiz-test.dev"),
    ])
    monkeypatch.setattr(im, "read_message_body", lambda msg_id: "No thanks.")
    monkeypatch.setattr(im, "classify_reply", lambda body: "Not Interested")

    # msg-1 processes cleanly; msg-2 blows up mid-loop (simulating any real
    # crash — a Notion timeout, a bad reply body, anything).
    def update_reply_status(page_id, status):
        if page_id == "page-1":
            return True
        raise RuntimeError("simulated crash processing the second message")

    calls = {"n": 0}

    def find_lead(email):
        calls["n"] += 1
        return {"page_id": "page-1" if calls["n"] == 1 else "page-2", "name": "Fake Biz", "ghost_site_status": None}

    monkeypatch.setattr(im, "find_lead_by_email", find_lead)
    monkeypatch.setattr(im, "update_reply_status", update_reply_status)

    with pytest.raises(RuntimeError):
        im.main()

    # msg-1 was fully handled before the crash — it must already be on disk,
    # not lost because the run never reached the end of the loop.
    assert im.load_processed_ids() == {"msg-1"}


def test_main_dedups_by_rfc_message_id_not_local_uid(monkeypatch):
    """Regression, found live 2026-08-26: a stale Track B resurrection reply
    kept re-triggering resurrect_ghost_site() and a fresh notification
    indefinitely. Root cause: dedup used himalaya's envelope "id", which is
    an IMAP UID scoped to whatever folder currently lists the message — when
    Gmail auto-restores an archived thread back to Inbox (which it does the
    moment any new message, like our own notification, lands in that same
    thread), the same physical reply reappears under a brand-new UID. The
    RFC Message-ID header is assigned once by the sending server and never
    changes across folder moves, so a message already marked processed under
    its Message-ID must be skipped even if its local UID is now different
    (simulating exactly the Gmail-restore scenario)."""
    monkeypatch.setattr(im, "load_processed_ids", lambda: {"stable-rfc-id@mx.google.com"})
    monkeypatch.setattr(im, "list_inbox", lambda: [
        _envelope("999-a-new-uid", "owner@fakebiz-test.dev", message_id="stable-rfc-id@mx.google.com"),
    ])

    def find_lead_should_not_be_called(email):
        raise AssertionError("find_lead_by_email() called for a Message-ID already marked processed")

    monkeypatch.setattr(im, "find_lead_by_email", find_lead_should_not_be_called)
    monkeypatch.setattr(im, "save_processed_ids", lambda ids: None)

    im.main()  # must not raise, must not reprocess


def test_main_saves_rfc_message_id_as_the_dedup_key(monkeypatch):
    monkeypatch.setattr(im, "load_processed_ids", lambda: set())
    monkeypatch.setattr(im, "list_inbox", lambda: [
        _envelope("local-uid-5", "owner@fakebiz-test.dev", message_id="real-message-id@mx.google.com"),
    ])
    monkeypatch.setattr(im, "find_lead_by_email", lambda email: {"page_id": "page-1", "name": "Fake Biz", "ghost_site_status": None})
    monkeypatch.setattr(im, "read_message_body", lambda local_id: "No thanks.")
    monkeypatch.setattr(im, "classify_reply", lambda body: "Not Interested")
    monkeypatch.setattr(im, "update_reply_status", lambda page_id, status: True)

    saved = {}
    monkeypatch.setattr(im, "save_processed_ids", lambda ids: saved.update(ids=set(ids)))

    im.main()
    assert saved["ids"] == {"real-message-id@mx.google.com"}


def test_main_skips_reply_from_unknown_sender(monkeypatch):
    monkeypatch.setattr(im, "load_processed_ids", lambda: set())
    monkeypatch.setattr(im, "list_inbox", lambda: [_envelope("msg-2", "stranger@nowhere-fake.dev")])
    monkeypatch.setattr(im, "find_lead_by_email", lambda email: None)

    def classify_should_not_be_called(body):
        raise AssertionError("classify_reply() called for an unmatched sender")

    monkeypatch.setattr(im, "classify_reply", classify_should_not_be_called)

    saved = {}
    monkeypatch.setattr(im, "save_processed_ids", lambda ids: saved.update(ids=ids))

    im.main()
    assert "msg-2" in saved["ids"]


def test_main_matches_sender_and_updates_lead(monkeypatch):
    monkeypatch.setattr(im, "load_processed_ids", lambda: set())
    monkeypatch.setattr(im, "list_inbox", lambda: [_envelope("msg-3", "owner@fakebiz-test.dev")])
    monkeypatch.setattr(im, "find_lead_by_email", lambda email: {"page_id": "page-abc", "name": "Fake Biz", "ghost_site_status": None})
    monkeypatch.setattr(im, "read_message_body", lambda msg_id: "No thanks, not interested.")
    monkeypatch.setattr(im, "classify_reply", lambda body: "Not Interested")

    updated = {}
    monkeypatch.setattr(im, "update_reply_status", lambda page_id, status: updated.update(page_id=page_id, status=status) or True)
    monkeypatch.setattr(im, "save_processed_ids", lambda ids: None)

    def notify_should_not_be_called(name, body):
        raise AssertionError("send_notification() called for a Not Interested reply")

    monkeypatch.setattr(im, "send_notification", notify_should_not_be_called)

    im.main()
    assert updated == {"page_id": "page-abc", "status": "Not Interested"}


def test_main_notifies_only_for_interested_reply(monkeypatch):
    monkeypatch.setattr(im, "load_processed_ids", lambda: set())
    monkeypatch.setattr(im, "list_inbox", lambda: [_envelope("msg-4", "owner@fakebiz-test.dev")])
    monkeypatch.setattr(im, "find_lead_by_email", lambda email: {"page_id": "page-xyz", "name": "Fake Biz", "ghost_site_status": None})
    monkeypatch.setattr(im, "read_message_body", lambda msg_id: "Sure, tell me more!")
    monkeypatch.setattr(im, "classify_reply", lambda body: "Interested")
    monkeypatch.setattr(im, "update_reply_status", lambda page_id, status: True)
    monkeypatch.setattr(im, "save_processed_ids", lambda ids: None)

    notified = []
    monkeypatch.setattr(im, "send_notification", lambda name, body: notified.append((name, body)))

    im.main()
    assert notified == [("Fake Biz", "Sure, tell me more!")]


# --- update_reply_status(): Sprint 9 dependency, Reply Received Date ---

def test_update_reply_status_writes_reply_received_date(monkeypatch):
    """Sprint 9's daily report needs to scope "Interested replies" to a
    specific day — Reply Status alone has no timestamp, so this field is
    what makes that filtering possible."""
    import datetime as real_datetime
    captured = {}

    class FakeResponse:
        status_code = 200

    def fake_patch(url, headers=None, json=None):
        captured["payload"] = json
        return FakeResponse()

    monkeypatch.setattr(im.requests, "patch", fake_patch)
    im.update_reply_status("page-123", "Interested")

    props = captured["payload"]["properties"]
    assert props["Reply Status"]["select"]["name"] == "Interested"
    assert props["Reply Received Date"]["date"]["start"] == real_datetime.date.today().isoformat()


# --- Sprint 18: any reply from a Track-B-active lead stops the sequence ---

def test_find_lead_by_email_returns_ghost_site_status(monkeypatch):
    class FakeResponse:
        status_code = 200

        def json(self):
            return {
                "results": [{
                    "id": "page-xyz",
                    "properties": {
                        "Business Name": {"title": [{"text": {"content": "Test Salon"}}]},
                        "Ghost Site Status": {"select": {"name": "Live"}},
                    },
                }],
            }

    monkeypatch.setattr(im.requests, "post", lambda url, headers=None, json=None: FakeResponse())
    lead = im.find_lead_by_email("owner@fakebiz-test.dev")
    assert lead["ghost_site_status"] == "Live"


def test_find_lead_by_email_ghost_site_status_none_when_not_set(monkeypatch):
    class FakeResponse:
        status_code = 200

        def json(self):
            return {
                "results": [{
                    "id": "page-xyz",
                    "properties": {
                        "Business Name": {"title": [{"text": {"content": "Test Salon"}}]},
                        "Ghost Site Status": {"select": None},
                    },
                }],
            }

    monkeypatch.setattr(im.requests, "post", lambda url, headers=None, json=None: FakeResponse())
    lead = im.find_lead_by_email("owner@fakebiz-test.dev")
    assert lead["ghost_site_status"] is None


@pytest.mark.parametrize("ghost_status", ["Selected", "Live"])
@pytest.mark.parametrize("classification", ["Interested", "Not Interested", "Unsubscribed"])
def test_any_reply_from_track_b_active_lead_marks_replied_and_notifies(monkeypatch, ghost_status, classification):
    """Cyril's call, 2026-08-12: ANY reply during an active ghost site
    sequence stops it and notifies immediately — not gated on Qwen
    classifying it Interested, unlike Track A's notification."""
    monkeypatch.setattr(im, "load_processed_ids", lambda: set())
    monkeypatch.setattr(im, "list_inbox", lambda: [_envelope("msg-tb", "owner@fakebiz-test.dev")])
    monkeypatch.setattr(im, "find_lead_by_email", lambda email: {
        "page_id": "page-tb", "name": "Ghost Site Biz", "ghost_site_status": ghost_status,
    })
    monkeypatch.setattr(im, "read_message_body", lambda msg_id: "some reply text")
    monkeypatch.setattr(im, "classify_reply", lambda body: classification)
    monkeypatch.setattr(im, "update_reply_status", lambda page_id, status: True)
    monkeypatch.setattr(im, "save_processed_ids", lambda ids: None)

    marked_replied = []
    monkeypatch.setattr(im, "mark_ghost_site_replied", lambda page_id: marked_replied.append(page_id) or True)

    notified = []
    monkeypatch.setattr(im, "send_ghost_site_reply_notification", lambda name, body: notified.append((name, body)))

    def track_a_notify_should_not_fire(name, body):
        raise AssertionError("Track A's send_notification() fired for a Track-B-active lead")

    monkeypatch.setattr(im, "send_notification", track_a_notify_should_not_fire)

    im.main()

    assert marked_replied == ["page-tb"]
    assert notified == [("Ghost Site Biz", "some reply text")]


def test_reply_from_non_track_b_lead_unaffected(monkeypatch):
    """A lead with no Ghost Site Status set keeps behaving exactly like
    before Sprint 18 — Track A's Interested-only notification path."""
    monkeypatch.setattr(im, "load_processed_ids", lambda: set())
    monkeypatch.setattr(im, "list_inbox", lambda: [_envelope("msg-ta", "owner@fakebiz-test.dev")])
    monkeypatch.setattr(im, "find_lead_by_email", lambda email: {
        "page_id": "page-ta", "name": "Track A Biz", "ghost_site_status": None,
    })
    monkeypatch.setattr(im, "read_message_body", lambda msg_id: "Sure, tell me more!")
    monkeypatch.setattr(im, "classify_reply", lambda body: "Interested")
    monkeypatch.setattr(im, "update_reply_status", lambda page_id, status: True)
    monkeypatch.setattr(im, "save_processed_ids", lambda ids: None)

    def ghost_mark_should_not_fire(page_id):
        raise AssertionError("mark_ghost_site_replied() called for a non-Track-B lead")

    monkeypatch.setattr(im, "mark_ghost_site_replied", ghost_mark_should_not_fire)

    notified = []
    monkeypatch.setattr(im, "send_notification", lambda name, body: notified.append((name, body)))

    im.main()
    assert notified == [("Track A Biz", "Sure, tell me more!")]


def test_mark_ghost_site_replied_patches_correct_status(monkeypatch):
    captured = {}

    class FakeResponse:
        status_code = 200

    def fake_patch(url, headers=None, json=None):
        captured["url"] = url
        captured["payload"] = json
        return FakeResponse()

    monkeypatch.setattr(im.requests, "patch", fake_patch)
    result = im.mark_ghost_site_replied("page-tb")

    assert result is True
    assert captured["url"].endswith("/pages/page-tb")
    assert captured["payload"]["properties"]["Ghost Site Status"]["select"]["name"] == "Replied"


def test_send_ghost_site_reply_notification_prints_only_outside_production(monkeypatch, capsys):
    monkeypatch.setattr(im.hp_env, "IS_PRODUCTION", False)

    def subprocess_should_not_run(*args, **kwargs):
        raise AssertionError("subprocess.run() called outside production")

    monkeypatch.setattr(im.subprocess, "run", subprocess_should_not_run)

    result = im.send_ghost_site_reply_notification("Ghost Site Biz", "some reply")
    assert result is True
    assert "WOULD NOTIFY" in capsys.readouterr().out


# --- Sprint 19: a reply from an Expired lead auto-resurrects, doesn't mark "Replied" ---

def test_reply_from_expired_lead_resurrects_not_marks_replied(monkeypatch):
    """Cyril's call, 2026-08-14: a reply to the one-shot resurrection email
    auto-rebuilds (flips back to Selected) rather than marking Replied —
    Replied is for an active-sequence reply, this is a different signal."""
    monkeypatch.setattr(im, "load_processed_ids", lambda: set())
    monkeypatch.setattr(im, "list_inbox", lambda: [_envelope("msg-res", "owner@fakebiz-test.dev")])
    monkeypatch.setattr(im, "find_lead_by_email", lambda email: {
        "page_id": "page-res", "name": "Resurrected Biz", "ghost_site_status": "Expired",
    })
    monkeypatch.setattr(im, "read_message_body", lambda msg_id: "yes bring it back!")
    monkeypatch.setattr(im, "classify_reply", lambda body: "Interested")
    monkeypatch.setattr(im, "update_reply_status", lambda page_id, status: True)
    monkeypatch.setattr(im, "save_processed_ids", lambda ids: None)

    def mark_replied_should_not_fire(page_id):
        raise AssertionError("mark_ghost_site_replied() called for an Expired lead's reply")

    monkeypatch.setattr(im, "mark_ghost_site_replied", mark_replied_should_not_fire)

    resurrected = []
    monkeypatch.setattr(im, "resurrect_ghost_site", lambda page_id: resurrected.append(page_id) or True)

    notified = []
    monkeypatch.setattr(im, "send_resurrection_reply_notification", lambda name, body: notified.append((name, body)))

    im.main()

    assert resurrected == ["page-res"]
    assert notified == [("Resurrected Biz", "yes bring it back!")]


def test_resurrect_ghost_site_sets_selected_and_clears_resurrection_date(monkeypatch):
    captured = {}

    class FakeResponse:
        status_code = 200

    def fake_patch(url, headers=None, json=None):
        captured["url"] = url
        captured["payload"] = json
        return FakeResponse()

    monkeypatch.setattr(im.requests, "patch", fake_patch)
    result = im.resurrect_ghost_site("page-res")

    assert result is True
    assert captured["url"].endswith("/pages/page-res")
    props = captured["payload"]["properties"]
    assert props["Ghost Site Status"]["select"]["name"] == "Selected"
    assert props["Resurrection Sent Date"]["date"] is None


def test_send_resurrection_reply_notification_prints_only_outside_production(monkeypatch, capsys):
    monkeypatch.setattr(im.hp_env, "IS_PRODUCTION", False)

    def subprocess_should_not_run(*args, **kwargs):
        raise AssertionError("subprocess.run() called outside production")

    monkeypatch.setattr(im.subprocess, "run", subprocess_should_not_run)

    result = im.send_resurrection_reply_notification("Resurrected Biz", "yes bring it back!")
    assert result is True
    assert "WOULD NOTIFY" in capsys.readouterr().out
