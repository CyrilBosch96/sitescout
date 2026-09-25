#!/usr/bin/env python3
"""
Tests for scripts/queue_check.py — Sprint 1.

Covers every AC line from PLAN.md's Sprint 1 entry, plus regression tests
for the two bugs found during Sprint 1 review (not in the original AC,
but real correctness bugs in the entry-point script):

  - Stale-reply reprocessing: without dedup, a job that exhausts again
    weeks later would find and reprocess the *old* already-answered
    reply, since nothing else distinguishes it from a new one.
  - Trailing punctuation: "City, State, Niche." left a stray period in
    the parsed niche.

Runs in-process (not subprocess, unlike test_hp_env.py) because these
tests need to mock subprocess.run for himalaya calls and redirect
STATE_FILE/PROCESSED_FILE per test — cheaper and more precise than
spinning up a real subprocess per case for this many scenarios.
"""

import json
import os
import subprocess
import sys

import pytest

SCRIPTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
sys.path.insert(0, SCRIPTS_DIR)

import queue_check  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_files(tmp_path, monkeypatch):
    """Every test gets its own state.json / processed_queue_replies.json —
    never the real dev files."""
    monkeypatch.setattr(queue_check, "STATE_FILE", str(tmp_path / "state.json"))
    monkeypatch.setattr(queue_check, "PROCESSED_FILE", str(tmp_path / "processed_queue_replies.json"))


def make_fake_run(monkeypatch, envelopes=None, message_bodies=None):
    """Replaces queue_check's subprocess.run with a fake that answers
    himalaya envelope-list/message-read calls and captures any send
    instead of executing it. Returns the list of captured raw messages."""
    sent = []

    def fake_run(args, **kwargs):
        if args[:3] == ["himalaya", "envelope", "list"]:
            return subprocess.CompletedProcess(args, 0, stdout=json.dumps({"envelopes": envelopes or []}), stderr="")
        if args[:3] == ["himalaya", "message", "read"]:
            msg_id = args[3]
            body = (message_bodies or {}).get(msg_id, "")
            return subprocess.CompletedProcess(args, 0, stdout=f"Headers-Go-Here\n\n{body}", stderr="")
        if args[:2] == ["himalaya", "message"] and args[2] == "send":
            sent.append(kwargs.get("input"))
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        raise AssertionError(f"Unexpected subprocess call in this test: {args}")

    monkeypatch.setattr(queue_check.subprocess, "run", fake_run)
    return sent


# --- parse_reply(): AC "reply City, State, Niche" / "only two parts rejected"
# / "whitespace/punctuation still extracts correctly" ---

# --- hours_since(): reminder-cadence gate for the ask email ---

def test_hours_since_none_is_treated_as_overdue():
    """No prior ask recorded -> due immediately, not blocked."""
    assert queue_check.hours_since(None) > queue_check.REMINDER_INTERVAL_HOURS


def test_hours_since_computes_real_elapsed_hours():
    import datetime as real_datetime
    two_hours_ago = (real_datetime.datetime.now(real_datetime.timezone.utc) - real_datetime.timedelta(hours=2)).isoformat()
    elapsed = queue_check.hours_since(two_hours_ago)
    assert 1.9 < elapsed < 2.1


def test_parse_reply_correct_three_part_input():
    assert queue_check.parse_reply("Wichita, Kansas, Hair Salons") == ("Wichita, Kansas", "Hair Salons")


def test_parse_reply_missing_state_rejected():
    assert queue_check.parse_reply("Wichita, Hair Salons") is None


def test_parse_reply_extra_whitespace_still_extracts():
    assert queue_check.parse_reply("  Wichita ,  Kansas ,  Hair Salons  ") == ("Wichita, Kansas", "Hair Salons")


def test_parse_reply_trailing_punctuation_stripped():
    assert queue_check.parse_reply("Los Angeles, CA, Barber Shops.") == ("Los Angeles, CA", "Barber Shops")


def test_parse_reply_internal_punctuation_preserved():
    # "St. Louis" has a meaningful internal period — must survive.
    assert queue_check.parse_reply("St. Louis, MO, Hair Salons") == ("St. Louis, MO", "Hair Salons")


def test_parse_reply_wrong_delimiter_rejected():
    assert queue_check.parse_reply("Wichita; Kansas; Hair Salons") is None


def test_parse_reply_empty_reply_rejected():
    assert queue_check.parse_reply("") is None


def test_parse_reply_blank_segment_rejected():
    assert queue_check.parse_reply("Wichita, , Hair Salons") is None


def test_parse_reply_too_many_parts_rejected():
    # Strict 3-part format is a deliberate tradeoff (documented in the
    # docstring) — a niche containing a comma isn't supported, and that's
    # expected behavior, not a bug.
    assert queue_check.parse_reply("Wichita, Kansas, Hair, Nail Salons") is None


def test_parse_reply_niche_first_order():
    """Regression, found live 2026-09-07: the original parser required a
    rigid 'City, State, Niche' order. Cyril replied 'Home inspectors,
    Tulsa, OK' — the natural order, matching how every OTHER convention
    in this project (the Notion 'Source Niche/Location' field, config)
    puts niche first — and the old parser silently misread it as
    city='Home inspectors', state='Tulsa', niche='OK', corrupting
    state.json and causing two days of cold emails to random unrelated
    Tulsa businesses. The parser must accept this order too."""
    assert queue_check.parse_reply("Hair Salons, Wichita, Kansas") == ("Wichita, Kansas", "Hair Salons")


def test_parse_reply_niche_first_order_with_abbreviation():
    assert queue_check.parse_reply("Home inspectors, Tulsa, OK") == ("Tulsa, OK", "Home inspectors")


def test_parse_reply_state_in_first_position_rejected_as_ambiguous():
    """A state in the first position doesn't unambiguously match either
    'City, State, Niche' or 'Niche, City, State' — better to ask again
    than guess wrong a second time."""
    assert queue_check.parse_reply("Kansas, Wichita, Hair Salons") is None


def test_parse_reply_no_state_present_rejected():
    assert queue_check.parse_reply("Wichita, Hair Salons, Barbershop") is None


def test_parse_reply_niche_that_looks_like_a_state_name_still_ambiguous():
    """Edge case: if two parts both look like a US state, position alone
    can't disambiguate safely — reject rather than guess."""
    assert queue_check.parse_reply("Georgia, Atlanta, Georgia") is None


def test_parse_reply_only_reads_first_line_ignores_signature():
    body = "Wichita, Kansas, Hair Salons\n\nSent from my iPhone"
    assert queue_check.parse_reply(body) == ("Wichita, Kansas", "Hair Salons")


# --- extract_reply_text(): himalaya's MIME part marker line regression ---

def test_extract_reply_text_skips_himalaya_mime_part_marker():
    """Regression, found live 2026-08-27: a real reply ("Wichita, Kansas,
    Hair Salons") was rejected as "unparseable" and queue_check silently
    sent another ask email instead of accepting it. Root cause: himalaya's
    real `message read` output prints a MIME part marker line ("[2]
    text/plain (192 B)") plus its own indented Content-Type /
    Content-Transfer-Encoding lines before the actual body, separated by
    another blank line — extract_reply_text()'s single split("\n\n", 1)
    only skipped the email headers, leaving the marker line as what
    parse_reply() read as the reply's first line. This is the exact raw
    stdout captured from a real `himalaya message read` call."""
    raw_output = (
        "Date: Thu, 27 Aug 2026 22:47:12 +0530\n"
        "From: Cyril Bosch <operator@example.com>\n"
        "To: operator@example.com\n"
        "Subject: Re: What's the next city + niche?\n"
        "\n"
        "[2] text/plain (192 B)\n"
        "    Content-Type: text/plain; charset=UTF-8\n"
        "    Content-Transfer-Encoding: quoted-printable\n"
        "\n"
        "Wichita, Kansas, Hair Salons\n"
        "\n"
        "\n"
        "On Thu, Aug 27, 2026 at 10:46 PM <operator@example.com> wrote:\n"
        "\n"
        "> Reply with exactly: City, State, Niche\n"
        ">\n"
        "> Example: Wichita, Kansas, Hair Salons\n"
        ">\n"
    )
    body = queue_check.extract_reply_text(raw_output)
    assert queue_check.parse_reply(body) == ("Wichita, Kansas", "Hair Salons")


def test_extract_reply_text_unaffected_when_no_mime_marker_present():
    """Not every raw_output necessarily has the marker line (e.g. tests
    elsewhere in this suite construct simplified bodies directly) — must
    not break the plain "headers, blank line, body" case."""
    raw_output = "Subject: Re: test\n\nWichita, Kansas, Hair Salons"
    assert queue_check.extract_reply_text(raw_output) == "Wichita, Kansas, Hair Salons"


# --- get_unread_reply(): AC "reply in unrelated thread not mistaken" +
# stale-reply regression test ---

ASKED_AT = queue_check.datetime.fromisoformat("2026-08-17T00:00:00+00:00")
AFTER_ASK = "2026-08-17T12:00:00+00:00"   # a reply that came after the ask — valid
BEFORE_ASK = "2026-08-11T23:51:43+05:30"  # a reply that predates the ask — must be rejected


def test_get_unread_reply_matches_subject_and_real_name(monkeypatch):
    make_fake_run(monkeypatch, envelopes=[
        {"id": 101, "subject": "Re: What's the next city + niche?", "from": [{"name": "Cyril Bosch"}], "date": AFTER_ASK},
    ])
    assert queue_check.get_unread_reply(set(), sent_after=ASKED_AT) == 101


def test_get_unread_reply_ignores_unrelated_thread(monkeypatch):
    make_fake_run(monkeypatch, envelopes=[
        {"id": 303, "subject": "Re: Invoice for August", "from": [{"name": "Some Client"}], "date": AFTER_ASK},
    ])
    assert queue_check.get_unread_reply(set(), sent_after=ASKED_AT) is None


def test_get_unread_reply_ignores_auto_sent_message_with_no_display_name(monkeypatch):
    # Our own auto-sent messages never set a display name — this is the
    # existing mechanism for telling them apart from a genuine reply.
    make_fake_run(monkeypatch, envelopes=[
        {"id": 404, "subject": "Re: What's the next city + niche?", "from": [{"name": "", "email": "operator@example.com"}], "date": AFTER_ASK},
    ])
    assert queue_check.get_unread_reply(set(), sent_after=ASKED_AT) is None


def test_get_unread_reply_skips_already_processed_id_regression(monkeypatch):
    # Regression test for the stale-reply bug found in Sprint 1 review:
    # the exact old reply must not be found again.
    make_fake_run(monkeypatch, envelopes=[
        {"id": 101, "subject": "Re: What's the next city + niche?", "from": [{"name": "Cyril Bosch"}], "date": AFTER_ASK},
    ])
    assert queue_check.get_unread_reply({"101"}, sent_after=ASKED_AT) is None


def test_get_unread_reply_finds_genuinely_new_reply_alongside_old_processed_one(monkeypatch):
    make_fake_run(monkeypatch, envelopes=[
        {"id": 101, "subject": "Re: What's the next city + niche?", "from": [{"name": "Cyril Bosch"}], "date": AFTER_ASK},
        {"id": 202, "subject": "Re: What's the next city + niche?", "from": [{"name": "Cyril Bosch"}], "date": AFTER_ASK},
    ])
    assert queue_check.get_unread_reply({"101"}, sent_after=ASKED_AT) == 202


# --- get_unread_reply() date gate: regression for the real 2026-08-18
# incident — a week-old reply ("Detroit, Michigan, coffee shops", sent
# 2026-08-11) got replayed as a fresh answer after processed_queue_replies
# .json was wiped by a reset, since dedup was the only thing preventing
# replay. A reply strictly older than the last ask we actually sent can
# never be a genuine answer to it, regardless of dedup file state. ---

def test_get_unread_reply_rejects_reply_older_than_last_ask(monkeypatch):
    make_fake_run(monkeypatch, envelopes=[
        {"id": 264, "subject": "Re: What's the next city + niche?", "from": [{"name": "Cyril Bosch"}], "date": BEFORE_ASK},
    ])
    # Empty processed_ids simulates exactly what happened live: the dedup
    # file got wiped, so this old reply looks "new" by that measure alone.
    assert queue_check.get_unread_reply(set(), sent_after=ASKED_AT) is None


def test_get_unread_reply_never_asked_returns_none_even_with_valid_reply_present(monkeypatch):
    # sent_after=None means "we've never sent an ask" (fresh reset) — no
    # existing reply can be a genuine answer to a question we never asked.
    make_fake_run(monkeypatch, envelopes=[
        {"id": 555, "subject": "Re: What's the next city + niche?", "from": [{"name": "Cyril Bosch"}], "date": AFTER_ASK},
    ])
    assert queue_check.get_unread_reply(set(), sent_after=None) is None


def test_get_unread_reply_missing_date_rejected(monkeypatch):
    make_fake_run(monkeypatch, envelopes=[
        {"id": 606, "subject": "Re: What's the next city + niche?", "from": [{"name": "Cyril Bosch"}]},  # no "date" key
    ])
    assert queue_check.get_unread_reply(set(), sent_after=ASKED_AT) is None


# --- main(): AC "no active job -> ask email" / "job active -> no duplicate
# ask" / state written only after valid parse, plus both stale-reply and
# malformed-reply regression tests at the full integration level ---

def test_main_sends_ask_email_when_no_active_job(monkeypatch):
    queue_check.save_state({"active_niche": None, "active_location": None, "exhausted": False, "last_asked_at": None})
    sent = make_fake_run(monkeypatch, envelopes=[])

    queue_check.main()

    assert len(sent) == 1
    assert "next city" in sent[0].lower()
    assert "operator@example.com" in sent[0]  # dev-mode recipient override still holds here too
    state = queue_check.load_state()
    assert state["last_asked_at"] is not None


def test_main_does_not_resend_ask_email_within_reminder_window(monkeypatch):
    """Regression, found live 2026-08-28: the "no reply found -> send ask
    email" branch had no cooldown at all, so every 10-minute cron cycle
    overnight with nothing to catch it on resent the exact same ask
    email — 79 copies in one night. The cooldown is now one cron cycle
    (2026-09-19, Cyril's call: keep asking every cycle until he replies),
    but an ask sent moments ago must still not be duplicated within the
    same cycle."""
    import datetime as real_datetime
    recently = (real_datetime.datetime.now(real_datetime.timezone.utc) - real_datetime.timedelta(minutes=2)).isoformat()
    queue_check.save_state({"active_niche": None, "active_location": None, "exhausted": False, "last_asked_at": recently})
    sent = make_fake_run(monkeypatch, envelopes=[])

    queue_check.main()

    assert sent == []
    assert queue_check.load_state()["last_asked_at"] == recently  # unchanged — no new send happened


def test_main_resends_ask_email_after_reminder_window_elapses(monkeypatch):
    import datetime as real_datetime
    long_ago = (real_datetime.datetime.now(real_datetime.timezone.utc) - real_datetime.timedelta(hours=13)).isoformat()
    queue_check.save_state({"active_niche": None, "active_location": None, "exhausted": False, "last_asked_at": long_ago})
    sent = make_fake_run(monkeypatch, envelopes=[])

    queue_check.main()

    assert len(sent) == 1
    assert queue_check.load_state()["last_asked_at"] != long_ago  # updated to the new send time


def test_main_asks_again_next_cycle_when_no_reply(monkeypatch):
    """Cyril's rule (2026-09-19): ask every ~10-minute cycle, around the
    clock, until he replies — not once every 12 hours."""
    import datetime as real_datetime
    ten_min_ago = (real_datetime.datetime.now(real_datetime.timezone.utc) - real_datetime.timedelta(minutes=10)).isoformat()
    queue_check.save_state({"active_niche": None, "active_location": None, "exhausted": False, "last_asked_at": ten_min_ago})
    sent = make_fake_run(monkeypatch, envelopes=[])

    queue_check.main()

    assert len(sent) == 1


def test_low_supply_mode_asks_even_with_active_job(monkeypatch):
    """--low-supply: fresh stock is below the daily limit but a job is
    still active (not exhausted) — must still ask for the next city."""
    monkeypatch.setattr(queue_check.sys, "argv", ["queue_check.py", "--low-supply"])
    queue_check.save_state({"active_niche": "Home Inspectors", "active_location": "Frisco, Texas", "exhausted": False, "last_asked_at": None})
    sent = make_fake_run(monkeypatch, envelopes=[])

    queue_check.main()

    assert len(sent) == 1
    state = queue_check.load_state()
    assert state["active_location"] == "Frisco, Texas"  # asking never changes the job by itself


def test_ask_email_includes_a_mid_tier_city_suggestion():
    body = queue_check.build_ask_body({"active_niche": "Home Inspectors", "active_location": "Frisco, Texas"})
    assert "Suggested next city" in body
    assert "Home Inspectors, Boise, ID" in body


def test_suggestion_skips_cities_already_worked_and_the_active_one():
    state = {"active_niche": "Home Inspectors", "active_location": "Boise, ID", "used_locations": ["Chattanooga, TN"]}
    suggestion = queue_check.suggest_city(state)
    assert suggestion == ("Knoxville", "TN")


def test_suggestion_none_when_list_exhausted():
    used = [f"{c}, {s}" for c, s in queue_check.MID_TIER_CITY_SUGGESTIONS]
    assert queue_check.suggest_city({"used_locations": used}) is None
    assert "Suggested next city" not in queue_check.build_ask_body({"used_locations": used})


def test_accepted_reply_records_location_as_used(monkeypatch):
    queue_check.save_state({"active_niche": None, "active_location": None, "exhausted": True, "last_asked_at": "2026-08-01T00:00:00+00:00"})
    make_fake_run(
        monkeypatch,
        envelopes=[{"id": 777, "subject": "Re: What's the next city + niche?", "from": [{"name": "Cyril Bosch"}], "date": "2026-08-02T00:00:00+00:00"}],
        message_bodies={"777": "Home Inspectors, Boise, ID"},
    )
    queue_check.main()
    assert "Boise, ID" in queue_check.load_state()["used_locations"]


def test_main_no_duplicate_ask_when_job_already_active(monkeypatch):
    queue_check.save_state({"active_niche": "Hair Salons", "active_location": "Wichita, KS", "exhausted": False, "last_asked_at": None})
    sent = make_fake_run(monkeypatch, envelopes=[])

    queue_check.main()

    assert sent == []


def test_main_processes_valid_reply_and_writes_state(monkeypatch):
    queue_check.save_state({"active_niche": None, "active_location": None, "exhausted": True, "last_asked_at": "2026-08-01T00:00:00+00:00"})
    sent = make_fake_run(
        monkeypatch,
        envelopes=[{"id": 555, "subject": "Re: What's the next city + niche?", "from": [{"name": "Cyril Bosch"}], "date": "2026-08-02T00:00:00+00:00"}],
        message_bodies={"555": "Wichita, Kansas, Hair Salons"},
    )

    queue_check.main()

    state = queue_check.load_state()
    assert state["active_niche"] == "Hair Salons"
    assert state["active_location"] == "Wichita, Kansas"
    assert state["exhausted"] is False
    assert sent == []  # valid parse — no retry email needed
    assert "555" in queue_check.load_processed_ids()


def test_main_sends_retry_email_on_malformed_reply_without_writing_state(monkeypatch):
    queue_check.save_state({"active_niche": None, "active_location": None, "exhausted": True, "last_asked_at": "2026-08-01T00:00:00+00:00"})
    sent = make_fake_run(
        monkeypatch,
        envelopes=[{"id": 666, "subject": "Re: What's the next city + niche?", "from": [{"name": "Cyril Bosch"}], "date": "2026-08-02T00:00:00+00:00"}],
        message_bodies={"666": "Wichita, Hair Salons"},  # only 2 parts
    )

    queue_check.main()

    assert len(sent) == 1
    assert "resend" in sent[0].lower()
    state = queue_check.load_state()
    assert state["active_niche"] is None  # state.json only written after a *valid* parse
    assert "666" in queue_check.load_processed_ids()  # marked handled so it doesn't retry-spam every run


def test_main_does_not_reprocess_stale_reply_after_rexhaustion_regression(monkeypatch):
    """
    The exact bug found during Sprint 1 review: a job answered once,
    worked, and exhausted again weeks later — without dedup, Queue Check
    would find this same old reply and silently re-set the identical job
    instead of asking for (and waiting on) a genuinely new answer.
    """
    queue_check.save_state({"active_niche": None, "active_location": None, "exhausted": True, "last_asked_at": None})
    queue_check.save_processed_ids({"777"})
    sent = make_fake_run(
        monkeypatch,
        envelopes=[{"id": 777, "subject": "Re: What's the next city + niche?", "from": [{"name": "Cyril Bosch"}]}],
        message_bodies={"777": "Wichita, Kansas, Hair Salons"},
    )

    queue_check.main()

    state = queue_check.load_state()
    assert state["active_niche"] is None  # not silently re-set from the stale reply
    assert len(sent) == 1
    assert "reply as: city" in sent[0].lower()  # fell through to the fresh "ask again" path


def test_main_does_not_reprocess_stale_reply_when_dedup_file_was_wiped_regression(monkeypatch):
    """
    The real 2026-08-18 incident, reproduced exactly: production's
    processed_queue_replies.json got wiped by a reset, so a genuinely
    week-old reply ("Detroit, Michigan, coffee shops", sent 2026-08-11)
    had no dedup record and looked brand new. state.json's last_asked_at
    is the real anchor Cyril actually needs — a reply predating the most
    recent ask can never be its answer, regardless of what
    processed_queue_replies.json does or doesn't remember.
    """
    queue_check.save_state({
        "active_niche": None, "active_location": None, "exhausted": True,
        "last_asked_at": "2026-08-17T00:00:00+00:00",
    })
    queue_check.save_processed_ids(set())  # dedup file wiped — empty, not seeded
    sent = make_fake_run(
        monkeypatch,
        envelopes=[{
            "id": "264", "subject": "Re: What's the next city + niche?",
            "from": [{"name": "Cyril Bosch"}], "date": "2026-08-11T23:51:43+05:30",
        }],
        message_bodies={"264": "Detroit, Michigan, coffee shops"},
    )

    queue_check.main()

    state = queue_check.load_state()
    assert state["active_niche"] is None  # the stale reply must NOT set an active job
    assert len(sent) == 1
    assert "reply as: city" in sent[0].lower()  # fresh ask sent instead


def test_main_ignores_unrelated_thread_reply_and_asks_again(monkeypatch):
    queue_check.save_state({"active_niche": None, "active_location": None, "exhausted": True, "last_asked_at": None})
    sent = make_fake_run(
        monkeypatch,
        envelopes=[{"id": 888, "subject": "Re: Invoice question", "from": [{"name": "Some Client"}]}],
        message_bodies={},
    )

    queue_check.main()

    state = queue_check.load_state()
    assert state["active_niche"] is None
    assert len(sent) == 1
    assert "reply as: city" in sent[0].lower()
