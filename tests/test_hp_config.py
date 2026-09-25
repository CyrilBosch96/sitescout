#!/usr/bin/env python3
"""
Tests for scripts/hp_config.py — Sprint 12 (Config consolidation).

Covers Sprint 12's AC:
  - config.yaml loads and every expected key is exposed with the correct
    value (the values this repo's config.yaml actually ships with — the
    same numbers every script used to hardcode independently, migrated
    without changing behavior).
  - A missing required key fails loudly (SystemExit), matching hp_env.py's
    established pattern, rather than silently running with a None/default.

Gap 5: the 7 PageSpeed/Core Web Vitals thresholds and the lead-discovery
quota are asserted as separate top-level keys here specifically, since
that flat shape is what Sprint 13a's settings panel needs to bind to.
"""

import os
import sys

import pytest

SCRIPTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
sys.path.insert(0, SCRIPTS_DIR)

import hp_config  # noqa: E402


def test_config_file_exists_and_is_the_real_project_config():
    assert os.path.exists(hp_config.CONFIG_FILE)
    assert hp_config.CONFIG_FILE.endswith("config.yaml")


# --- Gap 5: 7 separate PageSpeed/CWV threshold keys, lead-discovery quota ---

def test_seven_pagespeed_and_core_web_vitals_thresholds_are_separate_keys():
    assert hp_config.PAGESPEED_PERFORMANCE_THRESHOLD == 0.9
    assert hp_config.PAGESPEED_SEO_THRESHOLD == 0.9
    assert hp_config.PAGESPEED_ACCESSIBILITY_THRESHOLD == 0.9
    assert hp_config.PAGESPEED_BEST_PRACTICES_THRESHOLD == 0.9
    assert hp_config.CORE_WEB_VITALS_LCP_THRESHOLD_MS == 2500
    assert hp_config.CORE_WEB_VITALS_CLS_THRESHOLD == 0.1
    assert hp_config.CORE_WEB_VITALS_TBT_THRESHOLD_MS == 200


def test_lead_discovery_quota_is_its_own_key():
    assert hp_config.LEAD_DISCOVERY_MIN_QUALIFYING_LEADS == 10
    assert hp_config.LEAD_DISCOVERY_MAX_TOTAL_CHECKED == 500


# --- Every other migrated value matches what scripts used to hardcode ---

def test_all_other_migrated_values_match_original_hardcoded_values():
    assert hp_config.MIN_SIGNALS_FAILED == 3
    assert hp_config.PAGESPEED_RECHECK_DELAY_SECONDS == 5
    assert hp_config.COMPETITOR_RANK_THRESHOLD == 10
    assert hp_config.COMPETITOR_REVIEW_DELTA_THRESHOLD == 50
    assert hp_config.DAILY_SEND_LIMIT == 10
    assert hp_config.COLD_EMAIL_TARGET_LOCAL_HOUR == 8
    assert hp_config.ORCHESTRATOR_LOCK_MAX_AGE_SECONDS == 1800
    assert hp_config.OPERATOR_EMAIL == "operator@example.com"


# --- Missing key fails loudly, same pattern as hp_env.py ---

def test_get_raises_system_exit_on_missing_key(monkeypatch):
    monkeypatch.setattr(hp_config, "_config", {"some_other_key": 1})
    with pytest.raises(SystemExit):
        hp_config._get("a_key_that_does_not_exist")


def test_get_returns_the_value_when_key_present(monkeypatch):
    monkeypatch.setattr(hp_config, "_config", {"my_key": 42})
    assert hp_config._get("my_key") == 42
