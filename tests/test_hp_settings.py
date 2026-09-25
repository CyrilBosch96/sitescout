#!/usr/bin/env python3
"""
Tests for scripts/hp_settings.py — Sprint 13a (Dashboard Settings Panel).

Covers Sprint 13a's AC:
  - Editing a threshold/quota and saving updates config.yaml immediately.
  - Invalid input is rejected with a clear error and config.yaml is left
    completely untouched (not partially written, not silently corrected).
  - Current value and Google's official default are both readable.
  - Every change is recorded to an audit trail (state.db's settings_history).

Also covers the specific implementation risk this sprint calls out: the
targeted single-line write must never destroy config.yaml's explanatory
comments, which a naive full-file yaml.safe_dump() rewrite would.
"""

import os
import sys

import pytest
import yaml

SCRIPTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
sys.path.insert(0, SCRIPTS_DIR)

import hp_settings  # noqa: E402
import hp_runlog  # noqa: E402

FIXTURE_CONFIG = """\
# SiteScout — tunable settings.
# This comment must survive every edit made through the settings panel.

# --- Website Evaluation — 4 PageSpeed category scores ---
pagespeed_performance_threshold: 0.9
pagespeed_seo_threshold: 0.9
pagespeed_accessibility_threshold: 0.9
pagespeed_best_practices_threshold: 0.9

# --- Website Evaluation — 3 Core Web Vitals ---
core_web_vitals_lcp_threshold_ms: 2500
core_web_vitals_cls_threshold: 0.1
core_web_vitals_tbt_threshold_ms: 200

# --- Lead Discovery ---
lead_discovery_min_qualifying_leads: 20

operator_email: operator@example.com
"""


@pytest.fixture(autouse=True)
def isolated_config_and_db(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(FIXTURE_CONFIG)
    monkeypatch.setattr(hp_settings, "CONFIG_FILE", str(config_path))
    monkeypatch.setattr(hp_runlog, "DB_FILE", str(tmp_path / "state.db"))
    return config_path


# --- validate(): range/type checks per field kind ---

@pytest.mark.parametrize("value", ["0.0", "0.5", "1.0"])
def test_validate_score_accepts_valid_range(value):
    parsed, error = hp_settings.validate("pagespeed_performance_threshold", value)
    assert error is None
    assert parsed == float(value)


@pytest.mark.parametrize("value", ["-0.1", "1.1", "5"])
def test_validate_score_rejects_out_of_range(value):
    parsed, error = hp_settings.validate("pagespeed_performance_threshold", value)
    assert parsed is None
    assert "between 0 and 1" in error


def test_validate_score_rejects_non_numeric():
    parsed, error = hp_settings.validate("pagespeed_performance_threshold", "not-a-number")
    assert parsed is None
    assert "must be a number" in error


@pytest.mark.parametrize("value,valid", [("2500", True), ("20000", True), ("20001", False), ("0", False), ("-100", False)])
def test_validate_lcp_ms_range(value, valid):
    parsed, error = hp_settings.validate("core_web_vitals_lcp_threshold_ms", value)
    assert (error is None) == valid


@pytest.mark.parametrize("value,valid", [("200", True), ("5000", True), ("5001", False), ("0", False)])
def test_validate_tbt_ms_range(value, valid):
    parsed, error = hp_settings.validate("core_web_vitals_tbt_threshold_ms", value)
    assert (error is None) == valid


@pytest.mark.parametrize("value,valid", [("0.1", True), ("1.0", True), ("1.1", False), ("-0.1", False)])
def test_validate_cls_range(value, valid):
    parsed, error = hp_settings.validate("core_web_vitals_cls_threshold", value)
    assert (error is None) == valid


@pytest.mark.parametrize("value,valid", [("20", True), ("1", True), ("200", True), ("201", False), ("0", False), ("20000", False)])
def test_validate_quota_range(value, valid):
    parsed, error = hp_settings.validate("lead_discovery_min_qualifying_leads", value)
    assert (error is None) == valid
    if valid:
        assert isinstance(parsed, int)


def test_validate_quota_rejects_non_integer():
    parsed, error = hp_settings.validate("lead_discovery_min_qualifying_leads", "20.5")
    assert parsed is None
    assert error is not None


def test_validate_unknown_key_rejected():
    parsed, error = hp_settings.validate("not_a_real_setting", "5")
    assert parsed is None
    assert "Unknown setting" in error


# --- Sprint 22: the 9 additional config.yaml levers ---

@pytest.mark.parametrize("value,valid", [("1", True), ("10", True), ("100", True), ("101", False), ("0", False)])
def test_validate_send_limit_range(value, valid):
    parsed, error = hp_settings.validate("daily_send_limit", value)
    assert (error is None) == valid


@pytest.mark.parametrize("value,valid", [("0", True), ("8", True), ("23", True), ("24", False), ("-1", False)])
def test_validate_hour_of_day_range(value, valid):
    parsed, error = hp_settings.validate("cold_email_target_local_hour", value)
    assert (error is None) == valid


@pytest.mark.parametrize("value,valid", [("1", True), ("3", True), ("7", True), ("8", False), ("0", False)])
def test_validate_signal_count_range(value, valid):
    parsed, error = hp_settings.validate("min_signals_failed", value)
    assert (error is None) == valid


@pytest.mark.parametrize("value,valid", [("0", True), ("5", True), ("300", True), ("301", False), ("-1", False)])
def test_validate_delay_seconds_range(value, valid):
    parsed, error = hp_settings.validate("pagespeed_recheck_delay_seconds", value)
    assert (error is None) == valid


@pytest.mark.parametrize("value,valid", [("1", True), ("100", True), ("500", True), ("501", False), ("0", False)])
def test_validate_checked_quota_range(value, valid):
    parsed, error = hp_settings.validate("lead_discovery_max_total_checked", value)
    assert (error is None) == valid


@pytest.mark.parametrize("value,valid", [("1", True), ("10", True), ("100", True), ("101", False), ("0", False)])
def test_validate_rank_threshold_range(value, valid):
    parsed, error = hp_settings.validate("competitor_rank_threshold", value)
    assert (error is None) == valid


@pytest.mark.parametrize("value,valid", [("0", True), ("50", True), ("10000", True), ("10001", False), ("-1", False)])
def test_validate_review_delta_range(value, valid):
    parsed, error = hp_settings.validate("competitor_review_delta_threshold", value)
    assert (error is None) == valid


@pytest.mark.parametrize("value,valid", [("60", True), ("1800", True), ("86400", True), ("86401", False), ("59", False)])
def test_validate_lock_seconds_range(value, valid):
    parsed, error = hp_settings.validate("orchestrator_lock_max_age_seconds", value)
    assert (error is None) == valid


@pytest.mark.parametrize("value,valid", [("500", True), ("3000", True), ("50000", True), ("499", False), ("50001", False)])
def test_validate_grid_radius_meters_range(value, valid):
    parsed, error = hp_settings.validate("discovery_grid_cell_radius_meters", value)
    assert (error is None) == valid


@pytest.mark.parametrize("value,valid", [("1", True), ("4999", True), ("5000", False), ("0", False)])
def test_validate_places_cap_range(value, valid):
    """Core regression, Cyril's explicit call (2026-08-31): the dashboard
    itself must never allow raising this to 5,000 or beyond — the
    validator enforces the boundary, not just documentation."""
    parsed, error = hp_settings.validate("places_api_monthly_cap", value)
    assert (error is None) == valid


@pytest.mark.parametrize("value,valid", [
    ("operator@example.com", True), ("a@b.co", True),
    ("not-an-email", False), ("@example.com", False), ("cyril@", False), ("", False),
])
def test_validate_email_type(value, valid):
    parsed, error = hp_settings.validate("operator_email", value)
    assert (error is None) == valid
    if valid:
        assert parsed == value


def test_validate_email_strips_whitespace():
    parsed, error = hp_settings.validate("operator_email", "  operator@example.com  ")
    assert error is None
    assert parsed == "operator@example.com"


# --- update_setting(): the write path itself ---

def test_update_setting_writes_correct_value(isolated_config_and_db):
    success, error = hp_settings.update_setting("pagespeed_performance_threshold", "0.85")
    assert success is True
    assert error is None
    assert hp_settings.read_current_config()["pagespeed_performance_threshold"] == 0.85


def test_update_setting_preserves_every_comment_and_other_key(isolated_config_and_db):
    hp_settings.update_setting("lead_discovery_min_qualifying_leads", "30")
    text = isolated_config_and_db.read_text()

    assert "# SiteScout — tunable settings." in text
    assert "# This comment must survive every edit made through the settings panel." in text
    assert "# --- Lead Discovery ---" in text
    # Every other key's value is untouched.
    config = yaml.safe_load(text)
    assert config["pagespeed_performance_threshold"] == 0.9
    assert config["operator_email"] == "operator@example.com"
    assert config["lead_discovery_min_qualifying_leads"] == 30


def test_update_setting_invalid_input_leaves_file_completely_untouched(isolated_config_and_db):
    original_text = isolated_config_and_db.read_text()

    success, error = hp_settings.update_setting("pagespeed_performance_threshold", "5.0")

    assert success is False
    assert error is not None
    assert isolated_config_and_db.read_text() == original_text


def test_update_setting_records_audit_trail(isolated_config_and_db):
    hp_settings.update_setting("core_web_vitals_lcp_threshold_ms", "3000")

    history = hp_runlog.get_settings_history()
    assert len(history) == 1
    assert history[0]["key"] == "core_web_vitals_lcp_threshold_ms"
    assert history[0]["old_value"] == "2500"
    assert history[0]["new_value"] == "3000.0" or history[0]["new_value"] == "3000"
    assert history[0]["changed_at"] is not None


def test_invalid_update_does_not_record_audit_trail(isolated_config_and_db):
    hp_settings.update_setting("pagespeed_performance_threshold", "invalid")
    assert hp_runlog.get_settings_history() == []


# --- read_current_config(): always fresh off disk, per-call ---

def test_read_current_config_reflects_changes_made_outside_this_process(isolated_config_and_db):
    before = hp_settings.read_current_config()["lead_discovery_min_qualifying_leads"]
    isolated_config_and_db.write_text(
        isolated_config_and_db.read_text().replace(
            "lead_discovery_min_qualifying_leads: 20", "lead_discovery_min_qualifying_leads: 99"
        )
    )
    after = hp_settings.read_current_config()["lead_discovery_min_qualifying_leads"]
    assert before == 20
    assert after == 99
