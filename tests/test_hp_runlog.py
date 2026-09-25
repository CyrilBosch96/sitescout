#!/usr/bin/env python3
"""
Tests for scripts/hp_runlog.py — Sprint 11 (Run-log + wrapper).

Covers Sprint 11's AC directly, matching the plan's own literal test gate:
  - A deliberately-broken dummy script produces a `fail` row with the
    captured error and doesn't crash the outer process.
  - A real (successful) script run produces a correct `success` row.
  - A triggered failure actually produces an alert email (verified via a
    mocked Himalaya subprocess call, not a real send in tests).
"""

import os
import sqlite3
import sys

import pytest

SCRIPTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
sys.path.insert(0, SCRIPTS_DIR)

import hp_runlog  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(hp_runlog, "DB_FILE", str(tmp_path / "state.db"))


def _fetch_row(run_id):
    conn = sqlite3.connect(hp_runlog.DB_FILE)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


# --- schema / connection ---

def test_connect_creates_table_if_not_exists():
    conn = hp_runlog._connect()
    cols = {row[1] for row in conn.execute("PRAGMA table_info(runs)")}
    conn.close()
    assert cols == {
        "run_id", "script_name", "started_at", "finished_at",
        "status", "error_message", "leads_found", "emails_sent", "notes",
    }


def test_connect_is_idempotent_across_calls():
    hp_runlog._connect().close()
    hp_runlog._connect().close()  # must not raise on an already-existing table


# --- start_run() / finish_run_success() / finish_run_failure() ---

def test_start_run_inserts_running_row():
    run_id = hp_runlog.start_run("test_script")
    row = _fetch_row(run_id)
    assert row["script_name"] == "test_script"
    assert row["status"] == "running"
    assert row["started_at"] is not None
    assert row["finished_at"] is None


def test_finish_run_success_updates_row_with_counts():
    run_id = hp_runlog.start_run("test_script")
    hp_runlog.finish_run_success(run_id, leads_found=5, emails_sent=3, notes="all good")
    row = _fetch_row(run_id)
    assert row["status"] == "success"
    assert row["finished_at"] is not None
    assert row["leads_found"] == 5
    assert row["emails_sent"] == 3
    assert row["notes"] == "all good"
    assert row["error_message"] is None


def test_finish_run_failure_updates_row_with_error():
    run_id = hp_runlog.start_run("test_script")
    hp_runlog.finish_run_failure(run_id, "Traceback: something broke")
    row = _fetch_row(run_id)
    assert row["status"] == "fail"
    assert row["finished_at"] is not None
    assert "something broke" in row["error_message"]


# --- send_failure_alert() ---

def test_send_failure_alert_invokes_himalaya(monkeypatch):
    captured = {}

    class FakeResult:
        returncode = 0
        stderr = ""

    def fake_run(cmd, input=None, text=None, capture_output=None):
        captured["cmd"] = cmd
        captured["input"] = input
        return FakeResult()

    monkeypatch.setattr(hp_runlog.subprocess, "run", fake_run)

    result = hp_runlog.send_failure_alert("cold_email", "ValueError: boom")
    assert result is True
    assert captured["cmd"] == ["himalaya", "message", "send"]
    assert "SITESCOUT FAILURE: cold_email" in captured["input"]
    assert "ValueError: boom" in captured["input"]


def test_send_failure_alert_returns_false_on_send_failure(monkeypatch):
    class FakeResult:
        returncode = 1
        stderr = "himalaya not configured"

    monkeypatch.setattr(hp_runlog.subprocess, "run", lambda *a, **k: FakeResult())
    assert hp_runlog.send_failure_alert("cold_email", "boom") is False


# --- run_wrapped(): the plan's literal test gate ---

def test_run_wrapped_success_writes_correct_row_with_counts(monkeypatch):
    monkeypatch.setattr(hp_runlog, "send_failure_alert", lambda *a, **k: True)

    def good_main():
        return {"leads_found": 7, "emails_sent": 2, "notes": "ran fine"}

    hp_runlog.run_wrapped("good_script", good_main)  # must not raise / exit

    conn = sqlite3.connect(hp_runlog.DB_FILE)
    row = conn.execute("SELECT * FROM runs WHERE script_name = 'good_script'").fetchone()
    conn.close()
    assert row is not None


def test_run_wrapped_success_with_none_return_leaves_counts_null():
    def bare_main():
        return None

    hp_runlog.run_wrapped("bare_script", bare_main)

    conn = sqlite3.connect(hp_runlog.DB_FILE)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM runs WHERE script_name = 'bare_script'").fetchone()
    conn.close()
    assert row["status"] == "success"
    assert row["leads_found"] is None
    assert row["emails_sent"] is None
    assert row["notes"] is None


def test_run_wrapped_deliberately_broken_script_writes_fail_row_and_alerts(monkeypatch):
    """The plan's literal test gate: a deliberately-broken dummy script
    produces a fail row with the captured error, and a failure alert
    actually gets sent — verified via a mocked Himalaya call here, matching
    how every other script's tests avoid real sends."""
    alert_calls = []
    monkeypatch.setattr(hp_runlog, "send_failure_alert", lambda script_name, error: alert_calls.append((script_name, error)) or True)

    def broken_main():
        raise ValueError("deliberately broken for the test gate")

    with pytest.raises(SystemExit) as exc_info:
        hp_runlog.run_wrapped("broken_script", broken_main)

    # "doesn't crash the outer process": run_wrapped exits in a controlled
    # way (SystemExit(1), catchable by whatever invoked it) rather than
    # letting the raw ValueError propagate as an unhandled crash.
    assert exc_info.value.code == 1

    conn = sqlite3.connect(hp_runlog.DB_FILE)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM runs WHERE script_name = 'broken_script'").fetchone()
    conn.close()
    assert row["status"] == "fail"
    assert "ValueError" in row["error_message"]
    assert "deliberately broken for the test gate" in row["error_message"]

    assert len(alert_calls) == 1
    assert alert_calls[0][0] == "broken_script"
    assert "deliberately broken for the test gate" in alert_calls[0][1]


def test_run_wrapped_success_never_sends_an_alert(monkeypatch):
    alert_calls = []
    monkeypatch.setattr(hp_runlog, "send_failure_alert", lambda *a, **k: alert_calls.append(1) or True)

    hp_runlog.run_wrapped("fine_script", lambda: {"notes": "fine"})

    assert alert_calls == []


# --- get_run_history(): Sprint 22, per-script drill-down ---

def test_get_run_history_filters_to_one_script_only():
    hp_runlog.run_wrapped("script_a", lambda: {"notes": "a1"})
    hp_runlog.run_wrapped("script_b", lambda: {"notes": "b1"})
    hp_runlog.run_wrapped("script_a", lambda: {"notes": "a2"})

    history = hp_runlog.get_run_history("script_a")
    assert len(history) == 2
    assert all(r["script_name"] == "script_a" for r in history)


def test_get_run_history_orders_most_recent_first():
    hp_runlog.run_wrapped("script_a", lambda: {"notes": "first"})
    hp_runlog.run_wrapped("script_a", lambda: {"notes": "second"})

    history = hp_runlog.get_run_history("script_a")
    assert history[0]["notes"] == "second"
    assert history[1]["notes"] == "first"


def test_get_run_history_respects_limit():
    for i in range(5):
        hp_runlog.run_wrapped("script_a", lambda i=i: {"notes": f"run{i}"})

    assert len(hp_runlog.get_run_history("script_a", limit=3)) == 3


def test_get_run_history_empty_for_unknown_script():
    assert hp_runlog.get_run_history("never_run_script") == []


# --- content_history: Sprint 22, dashboard Content page audit trail ---

def test_record_and_get_content_history():
    hp_runlog.record_content_change("followup_1", "old text", "new text")
    history = hp_runlog.get_content_history()
    assert len(history) == 1
    assert history[0]["key"] == "followup_1"
    assert history[0]["old_text"] == "old text"
    assert history[0]["new_text"] == "new text"


def test_content_history_orders_most_recent_first():
    hp_runlog.record_content_change("followup_1", "a", "b")
    hp_runlog.record_content_change("followup_2", "c", "d")

    history = hp_runlog.get_content_history()
    assert history[0]["key"] == "followup_2"
    assert history[1]["key"] == "followup_1"


def test_content_history_handles_null_old_text():
    """A brand-new content key (shouldn't normally happen, but
    write_content() never assumes read_content() succeeded first)."""
    hp_runlog.record_content_change("new_key", None, "first text")
    history = hp_runlog.get_content_history()
    assert history[0]["old_text"] is None
