#!/usr/bin/env python3
"""
Tests for scripts/hp_status.py — dashboard "current status" + rejected-
leads cache support (2026-08-17), built alongside the "what's about to
happen" panel.

Covers:
  - get_current_status() reads state.db's `runs` table for an open
    ("running") row and translates it to an operator-facing label.
  - A "running" row older than STALE_AFTER_SECONDS is treated as an
    orphan (a crash run_wrapped() couldn't catch) rather than shown as
    still in progress.
  - get_checked_places_cache_info() / clear_checked_places_cache()
    against the real cache file shape used by
    lead_discovery_and_evaluation_Contactsearch.py.
"""

import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

import pytest

SCRIPTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
sys.path.insert(0, SCRIPTS_DIR)

import hp_status  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_files(tmp_path, monkeypatch):
    monkeypatch.setattr(hp_status, "DB_FILE", str(tmp_path / "state.db"))
    monkeypatch.setattr(hp_status, "CACHE_FILE", str(tmp_path / "checked_places_cache.json"))


def _insert_run(script_name, started_at, status="running"):
    conn = sqlite3.connect(hp_status.DB_FILE, timeout=10)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS runs (
            run_id INTEGER PRIMARY KEY AUTOINCREMENT,
            script_name TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            status TEXT NOT NULL,
            error_message TEXT,
            leads_found INTEGER,
            emails_sent INTEGER,
            notes TEXT
        )
    """)
    conn.execute(
        "INSERT INTO runs (script_name, started_at, status) VALUES (?, ?, ?)",
        (script_name, started_at, status),
    )
    conn.commit()
    conn.close()


def _iso(dt):
    return dt.astimezone(timezone.utc).isoformat()


# --- get_current_status() ---

def test_no_runs_at_all_is_idle():
    result = hp_status.get_current_status()
    assert result["running"] is False
    assert "Idle" in result["label"]


def test_fresh_running_row_reports_running_with_friendly_label():
    _insert_run("lead_discovery_and_evaluation_Contactsearch", _iso(datetime.now(timezone.utc)))
    result = hp_status.get_current_status()
    assert result["running"] is True
    assert result["script_name"] == "lead_discovery_and_evaluation_Contactsearch"
    assert "Searching" in result["label"]


def test_unknown_script_name_falls_back_to_generic_label():
    _insert_run("some_future_script", _iso(datetime.now(timezone.utc)))
    result = hp_status.get_current_status()
    assert result["running"] is True
    assert "some_future_script" in result["label"]


def test_stale_running_row_treated_as_idle_not_stuck():
    old = datetime.now(timezone.utc) - timedelta(seconds=hp_status.STALE_AFTER_SECONDS + 300)
    _insert_run("cold_email", _iso(old))
    result = hp_status.get_current_status()
    assert result["running"] is False
    assert "Idle" in result["label"]


def test_completed_run_is_not_reported_as_running():
    conn = sqlite3.connect(hp_status.DB_FILE, timeout=10)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS runs (
            run_id INTEGER PRIMARY KEY AUTOINCREMENT,
            script_name TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            status TEXT NOT NULL,
            error_message TEXT,
            leads_found INTEGER,
            emails_sent INTEGER,
            notes TEXT
        )
    """)
    conn.execute(
        "INSERT INTO runs (script_name, started_at, finished_at, status) VALUES (?, ?, ?, ?)",
        ("cold_email", _iso(datetime.now(timezone.utc)), _iso(datetime.now(timezone.utc)), "success"),
    )
    conn.commit()
    conn.close()
    result = hp_status.get_current_status()
    assert result["running"] is False


def test_most_recent_running_row_wins_when_multiple_exist():
    _insert_run("queue_check", _iso(datetime.now(timezone.utc) - timedelta(minutes=5)))
    _insert_run("cold_email", _iso(datetime.now(timezone.utc)))
    result = hp_status.get_current_status()
    assert result["script_name"] == "cold_email"


# --- get_checked_places_cache_info() / clear_checked_places_cache() ---

def test_cache_info_missing_file_reports_zero():
    result = hp_status.get_checked_places_cache_info()
    assert result["count"] == 0


def test_cache_info_counts_real_shape():
    with open(hp_status.CACHE_FILE, "w") as f:
        json.dump({"place-1": "not_box1", "place-2": "no_email", "place-3": "written"}, f)
    result = hp_status.get_checked_places_cache_info()
    assert result["count"] == 3


def test_cache_info_corrupted_file_reports_zero_not_crash():
    with open(hp_status.CACHE_FILE, "w") as f:
        f.write("not valid json {{{")
    result = hp_status.get_checked_places_cache_info()
    assert result["count"] == 0


def test_clear_checked_places_cache_empties_real_populated_file():
    with open(hp_status.CACHE_FILE, "w") as f:
        json.dump({"place-1": "not_box1"}, f)
    hp_status.clear_checked_places_cache()
    with open(hp_status.CACHE_FILE) as f:
        assert json.load(f) == {}


def test_clear_checked_places_cache_works_when_file_absent():
    hp_status.clear_checked_places_cache()  # must not raise
    with open(hp_status.CACHE_FILE) as f:
        assert json.load(f) == {}


# --- to_ist(): dashboard-wide UTC -> IST display conversion (2026-08-18) ---

def test_to_ist_converts_utc_iso_string():
    # 2026-08-17T19:17:54 UTC -> 00:47 IST the next day (UTC+5:30)
    result = hp_status.to_ist("2026-08-17T19:17:54.398210+00:00")
    assert result == "12:47 AM, Aug 18 IST"


def test_to_ist_treats_naive_timestamp_as_utc():
    # No offset in the string -> must assume UTC, not local wall-clock, same
    # reasoning as get_current_status()'s age calculation.
    with_offset = hp_status.to_ist("2026-08-17T19:17:54+00:00")
    naive = hp_status.to_ist("2026-08-17T19:17:54")
    assert with_offset == naive


def test_to_ist_falsy_input_returns_em_dash():
    assert hp_status.to_ist(None) == "—"
    assert hp_status.to_ist("") == "—"


def test_to_ist_unparseable_string_passes_through_unchanged():
    assert hp_status.to_ist("not a real timestamp") == "not a real timestamp"


def test_to_ist_midnight_rollover_shows_correct_date():
    # 2026-08-17T20:00:00 UTC -> 01:30 IST on the 18th, not the 17th.
    result = hp_status.to_ist("2026-08-17T20:00:00+00:00")
    assert "1:30 AM" in result
    assert "Aug 18" in result
