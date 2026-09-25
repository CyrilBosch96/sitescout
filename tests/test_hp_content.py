#!/usr/bin/env python3
"""
Tests for scripts/hp_content.py — Sprint 22 (Mission Control Dashboard).

Covers:
  - read_content()/write_content() round-trip against real files.
  - An empty save is rejected outright (would otherwise ship a silent
    blank email the next time that template fires).
  - Every save is recorded to the audit trail (state.db's content_history).
  - An unknown key is rejected with a clear error, same pattern as
    hp_settings.validate()'s "Unknown setting" case.
"""

import os
import sys

import pytest

SCRIPTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
sys.path.insert(0, SCRIPTS_DIR)

import hp_content  # noqa: E402
import hp_runlog  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_registry_and_db(tmp_path, monkeypatch):
    path = tmp_path / "followup_1.md"
    path.write_text("Original text.\n")
    registry = {
        "followup_1": {
            "label": "Follow-up #1", "category": "Track A follow-ups",
            "path": str(path), "placeholders": [],
        },
    }
    monkeypatch.setattr(hp_content, "CONTENT_REGISTRY", registry)
    monkeypatch.setattr(hp_runlog, "DB_FILE", str(tmp_path / "state.db"))
    return path


# --- read_content() / write_content(): the round-trip ---

def test_read_content_returns_current_file_text(isolated_registry_and_db):
    assert hp_content.read_content("followup_1") == "Original text.\n"


def test_read_content_unknown_key_returns_none():
    assert hp_content.read_content("not_a_real_key") is None


def test_write_content_updates_the_file(isolated_registry_and_db):
    success, error = hp_content.write_content("followup_1", "New text.")
    assert success is True
    assert error is None
    assert hp_content.read_content("followup_1") == "New text."


def test_write_content_unknown_key_rejected():
    success, error = hp_content.write_content("not_a_real_key", "text")
    assert success is False
    assert "Unknown content key" in error


def test_write_content_empty_text_rejected(isolated_registry_and_db):
    success, error = hp_content.write_content("followup_1", "   \n  ")
    assert success is False
    assert "can't be saved empty" in error
    assert hp_content.read_content("followup_1") == "Original text.\n"  # untouched


# --- audit trail ---

def test_write_content_records_audit_trail(isolated_registry_and_db):
    hp_content.write_content("followup_1", "Edited text.")
    history = hp_runlog.get_content_history()
    assert len(history) == 1
    assert history[0]["key"] == "followup_1"
    assert history[0]["old_text"] == "Original text.\n"
    assert history[0]["new_text"] == "Edited text."


def test_invalid_write_does_not_record_audit_trail(isolated_registry_and_db):
    hp_content.write_content("followup_1", "")
    assert hp_runlog.get_content_history() == []


# --- CONTENT_REGISTRY shape (real module, not the isolated fixture) ---

def test_real_registry_covers_all_confirmed_categories():
    import importlib
    importlib.reload(hp_content)
    categories = {meta["category"] for meta in hp_content.CONTENT_REGISTRY.values()}
    assert categories == {"Track A — first email", "Track A follow-ups", "Track B preview sequence", "Track B ops"}


def test_real_registry_files_all_exist_on_disk():
    import importlib
    importlib.reload(hp_content)
    for key, meta in hp_content.CONTENT_REGISTRY.items():
        assert os.path.exists(meta["path"]), f"{key} points to a missing file: {meta['path']}"
