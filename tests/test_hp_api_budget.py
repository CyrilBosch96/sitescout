#!/usr/bin/env python3
"""
Tests for scripts/hp_api_budget.py — the hard monthly Places API call
cap (Cyril's explicit call, 2026-08-31): never reach Google's real
5,000/month free-tier boundary, no matter how many qualifying leads
have or haven't been found. Discovery must hard-stop at the configured
cap (4,999) and resume automatically on the next calendar month.
"""

import os
import sys

SCRIPTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
sys.path.insert(0, SCRIPTS_DIR)

import hp_api_budget  # noqa: E402


import pytest


@pytest.fixture(autouse=True)
def isolated_budget_file(tmp_path, monkeypatch):
    monkeypatch.setattr(hp_api_budget, "BUDGET_FILE", str(tmp_path / "places_api_budget.json"))


def test_calls_made_this_month_zero_when_no_file():
    assert hp_api_budget.calls_made_this_month() == 0


def test_can_make_call_true_when_under_cap(monkeypatch):
    monkeypatch.setattr(hp_api_budget, "MONTHLY_CAP", 5)
    for _ in range(4):
        hp_api_budget.record_call()
    assert hp_api_budget.can_make_call() is True


def test_can_make_call_false_at_cap(monkeypatch):
    """Core regression: the cap must actually stop calls, not just warn.
    Cyril's explicit rule: never reach 5,000 — a cap of 4999 means the
    4999th call is the last one allowed."""
    monkeypatch.setattr(hp_api_budget, "MONTHLY_CAP", 5)
    for _ in range(5):
        hp_api_budget.record_call()
    assert hp_api_budget.can_make_call() is False


def test_record_call_increments_persisted_count():
    hp_api_budget.record_call()
    hp_api_budget.record_call()
    assert hp_api_budget.calls_made_this_month() == 2


def test_budget_resets_automatically_on_new_month(monkeypatch):
    """Regression: 'stop, then continue next month' — Cyril's own words.
    The counter must not need any manual reset; a stored month different
    from the real current month is treated as a fresh start."""
    monkeypatch.setattr(hp_api_budget, "MONTHLY_CAP", 5)
    for _ in range(5):
        hp_api_budget.record_call()
    assert hp_api_budget.can_make_call() is False

    monkeypatch.setattr(hp_api_budget, "_current_month_key", lambda: "2099-01")

    assert hp_api_budget.can_make_call() is True
    assert hp_api_budget.calls_made_this_month() == 0


def test_record_call_after_month_rollover_starts_counting_fresh(monkeypatch):
    monkeypatch.setattr(hp_api_budget, "MONTHLY_CAP", 5)
    for _ in range(5):
        hp_api_budget.record_call()

    monkeypatch.setattr(hp_api_budget, "_current_month_key", lambda: "2099-01")
    hp_api_budget.record_call()

    assert hp_api_budget.calls_made_this_month() == 1
