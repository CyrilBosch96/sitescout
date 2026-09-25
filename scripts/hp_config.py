#!/usr/bin/env python3
"""
Shared tunable-settings loader — SiteScout scripts.

Numbers and shared operational values live in config.yaml (project root);
secrets stay in .env via hp_env.py — see that module's docstring for why
the split exists. One load, one source of truth: Sprint 12's whole point
is that changing a threshold or the operator's alert address means
editing this one file, not hunting across every script that used to
hardcode its own copy.

Sprint 13a's settings panel binds to these keys individually, so they stay
flat top-level keys here — never nest them into a combined blob.
"""

import os

import yaml

import hp_env

CONFIG_FILE = os.path.join(hp_env.PROJECT_ROOT, "config.yaml")

with open(CONFIG_FILE) as f:
    _config = yaml.safe_load(f)


def _get(key):
    if key not in _config:
        raise SystemExit(f"ERROR: Missing required config.yaml key: {key!r}")
    return _config[key]


PAGESPEED_PERFORMANCE_THRESHOLD = _get("pagespeed_performance_threshold")
PAGESPEED_SEO_THRESHOLD = _get("pagespeed_seo_threshold")
PAGESPEED_ACCESSIBILITY_THRESHOLD = _get("pagespeed_accessibility_threshold")
PAGESPEED_BEST_PRACTICES_THRESHOLD = _get("pagespeed_best_practices_threshold")

CORE_WEB_VITALS_LCP_THRESHOLD_MS = _get("core_web_vitals_lcp_threshold_ms")
CORE_WEB_VITALS_CLS_THRESHOLD = _get("core_web_vitals_cls_threshold")
CORE_WEB_VITALS_TBT_THRESHOLD_MS = _get("core_web_vitals_tbt_threshold_ms")

MIN_SIGNALS_FAILED = _get("min_signals_failed")
PAGESPEED_RECHECK_DELAY_SECONDS = _get("pagespeed_recheck_delay_seconds")

LEAD_DISCOVERY_MIN_QUALIFYING_LEADS = _get("lead_discovery_min_qualifying_leads")
LEAD_DISCOVERY_MAX_TOTAL_CHECKED = _get("lead_discovery_max_total_checked")

DISCOVERY_GRID_CELL_RADIUS_METERS = _get("discovery_grid_cell_radius_meters")
PLACES_API_MONTHLY_CAP = _get("places_api_monthly_cap")

COMPETITOR_RANK_THRESHOLD = _get("competitor_rank_threshold")
COMPETITOR_REVIEW_DELTA_THRESHOLD = _get("competitor_review_delta_threshold")

DAILY_SEND_LIMIT = _get("daily_send_limit")
COLD_EMAIL_PAUSED = _get("cold_email_paused")
COLD_EMAIL_TARGET_LOCAL_HOUR = _get("cold_email_target_local_hour")
COLD_EMAIL_SEND_WINDOW_END_HOUR = _get("cold_email_send_window_end_hour")

ORCHESTRATOR_LOCK_MAX_AGE_SECONDS = _get("orchestrator_lock_max_age_seconds")

OPERATOR_EMAIL = _get("operator_email")
