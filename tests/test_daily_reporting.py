#!/usr/bin/env python3
"""
Tests for scripts/daily_reporting.py — Sprint 9 (Daily Reporting).

Covers Sprint 9's AC:
  - Given a known dataset, every count (sent, follow-ups by stage, replies
    needing review) exactly matches the underlying data.
  - (Gap 7) previews-activated-this-week and conversions-this-week are
    included alongside Track A's counts.

Design decisions confirmed with Cyril (2026-08-12):
  - Time window: yesterday (the full previous calendar day), not "today
    so far" — a morning digest about what already happened.
  - "Replies needing review" = Interested only. Not Interested/Unsubscribed
    are already fully handled by Sprint 6's classification.
  - No Notion dashboard write — Sprint 13 builds the real dashboard later;
    an interim Notion structure now would likely just get thrown away.
  - New "Reply Received Date" field added to the CRM schema (Sprint 6 had
    no timestamp for replies at all) so "Interested replies from
    yesterday" is actually answerable, not a same-lead-every-day snapshot.
"""

import os
import sys
from datetime import date, timedelta

import pytest

SCRIPTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
sys.path.insert(0, SCRIPTS_DIR)

import daily_reporting as dr  # noqa: E402


def _fake_query(monkeypatch, pages_of_results):
    """pages_of_results: list of result-lists, one per simulated page."""
    calls = []
    state = {"i": 0}

    class FakeResponse:
        status_code = 200

        def __init__(self, results, has_more):
            self._results = results
            self._has_more = has_more

        def json(self):
            return {"results": self._results, "has_more": self._has_more, "next_cursor": "next" if self._has_more else None}

    def fake_post(url, headers=None, json=None):
        calls.append(json)
        i = state["i"]
        results = pages_of_results[i] if i < len(pages_of_results) else []
        has_more = i < len(pages_of_results) - 1
        state["i"] += 1
        return FakeResponse(results, has_more)

    monkeypatch.setattr(dr.requests, "post", fake_post)
    return calls


# --- _query_count(): pagination correctness ---

def test_query_count_sums_across_pages(monkeypatch):
    _fake_query(monkeypatch, [[{"id": "a"}, {"id": "b"}], [{"id": "c"}]])
    assert dr._query_count({"property": "x"}) == 3


def test_query_count_single_page(monkeypatch):
    _fake_query(monkeypatch, [[{"id": "a"}]])
    assert dr._query_count({"property": "x"}) == 1


def test_query_count_zero_results(monkeypatch):
    _fake_query(monkeypatch, [[]])
    assert dr._query_count({"property": "x"}) == 0


# --- Filter shape correctness for each counting function ---

def test_count_status_on_date_filter_shape(monkeypatch):
    calls = _fake_query(monkeypatch, [[]])
    dr.count_status_on_date("Follow 2", date(2026, 8, 11))
    conditions = calls[0]["filter"]["and"]
    assert {"property": "Contact Status", "select": {"equals": "Follow 2"}} in conditions
    assert {"property": "Last Contact Date", "date": {"equals": "2026-08-11"}} in conditions


def test_count_interested_replies_on_date_filter_shape(monkeypatch):
    calls = _fake_query(monkeypatch, [[]])
    dr.count_interested_replies_on_date(date(2026, 8, 11))
    conditions = calls[0]["filter"]["and"]
    assert {"property": "Reply Status", "select": {"equals": "Interested"}} in conditions
    assert {"property": "Reply Received Date", "date": {"equals": "2026-08-11"}} in conditions


def test_count_previews_activated_since_filter_shape(monkeypatch):
    calls = _fake_query(monkeypatch, [[]])
    dr.count_previews_activated_since(date(2026, 8, 5))
    assert calls[0]["filter"] == {"property": "Preview Sent Date", "date": {"on_or_after": "2026-08-05"}}


def test_count_conversions_since_filter_shape(monkeypatch):
    calls = _fake_query(monkeypatch, [[]])
    dr.count_conversions_since(date(2026, 8, 5))
    assert calls[0]["filter"] == {"property": "Converted At", "date": {"on_or_after": "2026-08-05"}}


# --- build_report(): AC "every count exactly matches a known dataset" ---

def test_build_report_matches_known_dataset(monkeypatch):
    known = {
        ("count_status_on_date", "Follow 1"): 10,
        ("count_status_on_date", "Follow 2"): 8,
        ("count_status_on_date", "Follow 3"): 5,
        ("count_status_on_date", "Follow 4"): 2,
        ("count_status_on_date", "Lead Lost"): 1,
    }

    def fake_count_status(status, target_date):
        return known[("count_status_on_date", status)]

    monkeypatch.setattr(dr, "count_status_on_date", fake_count_status)
    monkeypatch.setattr(dr, "count_interested_replies_on_date", lambda target_date: 4)
    monkeypatch.setattr(dr, "count_previews_activated_since", lambda since_date: 6)
    monkeypatch.setattr(dr, "count_conversions_since", lambda since_date: 1)

    report = dr.build_report(date(2026, 8, 11))

    assert report["sent"] == 10
    assert report["followups"] == {1: 8, 2: 5, 3: 2, 4: 1}
    assert report["replies_needing_review"] == 4
    assert report["previews_activated_this_week"] == 6
    assert report["conversions_this_week"] == 1


def test_build_report_uses_a_7_day_inclusive_window_for_previews(monkeypatch):
    captured = {}
    monkeypatch.setattr(dr, "count_status_on_date", lambda status, target_date: 0)
    monkeypatch.setattr(dr, "count_interested_replies_on_date", lambda target_date: 0)
    monkeypatch.setattr(dr, "count_previews_activated_since", lambda since_date: captured.setdefault("since", since_date) or 0)
    monkeypatch.setattr(dr, "count_conversions_since", lambda since_date: 0)

    dr.build_report(date(2026, 8, 11))

    assert captured["since"] == date(2026, 8, 5)  # 7 days inclusive of the 11th


# --- format_report(): human-readable output includes every number ---

def test_format_report_includes_all_counts():
    report = {
        "date": date(2026, 8, 11),
        "sent": 10,
        "followups": {1: 8, 2: 5, 3: 2, 4: 1},
        "replies_needing_review": 4,
        "previews_activated_this_week": 6,
        "conversions_this_week": 1,
    }
    text = dr.format_report(report)
    assert "New leads emailed: 10" in text
    assert "Follow-up 1: 8" in text
    assert "Follow-up 2: 5" in text
    assert "Follow-up 3: 2" in text
    assert "Follow-up 4: 1" in text
    assert "Replies needing review (Interested): 4" in text
    assert "Previews activated: 6" in text
    assert "Conversions: 1" in text


# --- main(): reports on yesterday, sends via hp_env-resolved recipient ---

def test_main_reports_on_yesterday_not_today(monkeypatch):
    captured = {}

    def fake_build_report(target_date):
        captured["target_date"] = target_date
        return {
            "date": target_date, "sent": 0, "followups": {1: 0, 2: 0, 3: 0, 4: 0},
            "replies_needing_review": 0, "previews_activated_this_week": 0, "conversions_this_week": 0,
        }

    monkeypatch.setattr(dr, "build_report", fake_build_report)
    monkeypatch.setattr(dr, "send_email", lambda to, subject, body: (True, ""))

    dr.main()

    assert captured["target_date"] == date.today() - timedelta(days=1)


def test_main_sends_via_resolved_recipient(monkeypatch):
    sent = {}
    monkeypatch.setattr(dr, "build_report", lambda target_date: {
        "date": target_date, "sent": 0, "followups": {1: 0, 2: 0, 3: 0, 4: 0},
        "replies_needing_review": 0, "previews_activated_this_week": 0, "conversions_this_week": 0,
    })

    def fake_send_email(to_address, subject, body):
        sent["to"] = to_address
        sent["subject"] = subject
        return True, ""

    monkeypatch.setattr(dr, "send_email", fake_send_email)

    dr.main()

    assert sent["to"] == dr.hp_env.resolve_recipient(dr.RECIPIENT_ADDRESS)
    assert "Daily Pipeline Report" in sent["subject"]
