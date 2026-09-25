#!/usr/bin/env python3
"""
Tests for scripts/check_opens_reminder.py.

Covers: fires only at the target Pacific hour (3h after PT's 8am send
window), sends at most once per day even if called again the same hour,
and always routes the send through hp_env.resolve_recipient() like every
other internal ops email in this codebase.
"""

import os
import sys
from datetime import date, datetime

import pytest

SCRIPTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
sys.path.insert(0, SCRIPTS_DIR)

import check_opens_reminder as cor  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(cor, "STATE_FILE", str(tmp_path / "check_opens_reminder_state.json"))


class _FrozenAtTargetHour:
    @classmethod
    def now(cls, tz=None):
        return datetime(2026, 8, 13, cor.TARGET_PT_HOUR, 0, tzinfo=tz)


class _FrozenOffHour:
    @classmethod
    def now(cls, tz=None):
        return datetime(2026, 8, 13, 14, 0, tzinfo=tz)


def _capture_send(monkeypatch):
    sent = []

    def fake_run(cmd, input=None, text=None, capture_output=None):
        sent.append(input)

        class FakeResult:
            returncode = 0
            stderr = ""

        return FakeResult()

    monkeypatch.setattr(cor.subprocess, "run", fake_run)
    return sent


def test_does_not_send_outside_target_hour(monkeypatch):
    monkeypatch.setattr(cor, "datetime", _FrozenOffHour)
    sent = _capture_send(monkeypatch)
    cor.main()
    assert sent == []


def test_sends_at_target_hour(monkeypatch):
    monkeypatch.setattr(cor, "datetime", _FrozenAtTargetHour)
    sent = _capture_send(monkeypatch)
    cor.main()
    assert len(sent) == 1
    assert "Check your email opens" in sent[0] or "check" in sent[0].lower()


def test_does_not_send_twice_same_day(monkeypatch):
    monkeypatch.setattr(cor, "datetime", _FrozenAtTargetHour)
    sent = _capture_send(monkeypatch)
    cor.main()
    cor.main()
    assert len(sent) == 1


def test_sends_again_on_a_new_day(monkeypatch):
    monkeypatch.setattr(cor, "datetime", _FrozenAtTargetHour)
    sent = _capture_send(monkeypatch)
    cor.main()
    cor.save_state({"last_sent_date": (date(2026, 8, 12)).isoformat()})
    cor.main()
    assert len(sent) == 2


def test_send_uses_resolved_recipient(monkeypatch):
    monkeypatch.setattr(cor, "datetime", _FrozenAtTargetHour)
    captured = {}

    def fake_send_email(to_address, subject, body):
        captured["to"] = to_address
        return True, ""

    monkeypatch.setattr(cor, "send_email", fake_send_email)
    cor.main()
    assert captured["to"] == cor.hp_env.resolve_recipient(cor.RECIPIENT_ADDRESS)
