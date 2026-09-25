#!/usr/bin/env python3
"""
Tests for scripts/hp_lock.py — the shared run-lock.

Extracted from test_orchestrator.py (2026-08-17) when the lock itself
moved out of orchestrator.py into hp_lock.py, so the dashboard's manual
"Trigger" buttons could share the exact same lock and stop racing the
hourly orchestrator (or each other) — the real cause of a duplicate-leads
bug where two concurrent manual lead_discovery runs both wrote the same
qualifying businesses to Notion.

Covers Sprint 8's original AC, unchanged: a lock file <30 min old blocks
a concurrent run; >30 min old is treated as stale and the run proceeds.
"""

import os
import sys
import time

import pytest

SCRIPTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
sys.path.insert(0, SCRIPTS_DIR)

import hp_lock  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_lock_file(tmp_path, monkeypatch):
    monkeypatch.setattr(hp_lock, "LOCK_FILE", str(tmp_path / "orchestrator.lock"))


def test_acquire_lock_succeeds_when_no_lock_exists():
    assert hp_lock.acquire_lock() is True
    assert os.path.exists(hp_lock.LOCK_FILE)


def test_acquire_lock_blocked_by_fresh_lock():
    with open(hp_lock.LOCK_FILE, "w") as f:
        f.write(str(time.time()))
    assert hp_lock.acquire_lock() is False


def test_acquire_lock_proceeds_past_stale_lock():
    with open(hp_lock.LOCK_FILE, "w") as f:
        f.write(str(time.time()))
    stale_time = time.time() - (hp_lock.LOCK_MAX_AGE_SECONDS + 60)
    os.utime(hp_lock.LOCK_FILE, (stale_time, stale_time))

    assert hp_lock.acquire_lock() is True
    # A fresh lock should now be written (mtime updated, not left stale).
    assert (time.time() - os.path.getmtime(hp_lock.LOCK_FILE)) < 5


def test_acquire_lock_blocked_at_exactly_29_minutes():
    with open(hp_lock.LOCK_FILE, "w") as f:
        f.write(str(time.time()))
    almost_stale = time.time() - (hp_lock.LOCK_MAX_AGE_SECONDS - 60)
    os.utime(hp_lock.LOCK_FILE, (almost_stale, almost_stale))
    assert hp_lock.acquire_lock() is False


def test_release_lock_removes_file():
    with open(hp_lock.LOCK_FILE, "w") as f:
        f.write(str(time.time()))
    hp_lock.release_lock()
    assert not os.path.exists(hp_lock.LOCK_FILE)


def test_release_lock_no_error_when_file_absent():
    hp_lock.release_lock()  # must not raise


# --- refresh_lock(): heartbeat for long-running cycles, added 2026-08-31 ---

def test_refresh_lock_updates_mtime_of_existing_lock():
    """Regression, found live 2026-08-31: a real Lead Discovery run
    against a large market legitimately took over 30 minutes, so a
    second orchestrator cycle treated the still-active lock as stale
    and started a genuinely concurrent duplicate run — the exact
    double-write scenario this lock exists to prevent."""
    with open(hp_lock.LOCK_FILE, "w") as f:
        f.write(str(time.time()))
    stale_time = time.time() - (hp_lock.LOCK_MAX_AGE_SECONDS + 60)
    os.utime(hp_lock.LOCK_FILE, (stale_time, stale_time))

    hp_lock.refresh_lock()

    assert (time.time() - os.path.getmtime(hp_lock.LOCK_FILE)) < 5


def test_refresh_lock_no_error_when_file_absent():
    hp_lock.refresh_lock()  # must not raise — safe no-op when no lock held
    assert not os.path.exists(hp_lock.LOCK_FILE)


def test_refreshed_lock_still_blocks_a_concurrent_run():
    """The actual point of refreshing: a lock kept fresh by the loop
    must continue to block a would-be-concurrent second run, not just
    avoid an error."""
    with open(hp_lock.LOCK_FILE, "w") as f:
        f.write(str(time.time()))
    stale_time = time.time() - (hp_lock.LOCK_MAX_AGE_SECONDS + 60)
    os.utime(hp_lock.LOCK_FILE, (stale_time, stale_time))

    hp_lock.refresh_lock()

    assert hp_lock.acquire_lock() is False
