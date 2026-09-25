#!/usr/bin/env python3
"""
Dashboard Settings Panel backend — SiteScout (Sprint 13a)

Validates and writes live edits to config.yaml (Sprint 12), and records
each change to state.db's settings_history table (Sprint 13a) via
hp_runlog.record_setting_change().

Writes to config.yaml via a targeted single-line replacement, not a full
yaml.safe_dump() rewrite — config.yaml has explanatory comments on their
own lines above each key group, and a full rewrite would silently destroy
all of them. This is safe specifically because config.yaml is flat (no
nesting) and no key shares a line with a comment — confirmed by reading
the file, not assumed.

Only scripts spawned *after* an edit see it (each is a fresh subprocess,
importing hp_config fresh every time) — the dashboard's own already-running
process has hp_config's values cached from its own startup, which is why
the settings panel reads config.yaml fresh off disk for display rather
than trusting its own cached hp_config import.
"""

import os
import re

import yaml

import hp_env
import hp_runlog

CONFIG_FILE = os.path.join(hp_env.PROJECT_ROOT, "config.yaml")

# Google's officially published PageSpeed Insights "Good" cutoffs — shown
# alongside the current value so an edit is a visible, deliberate deviation
# from default, not a silent one. No default exists for the lead-discovery
# quota; it's Cyril's own operating parameter, not an external standard.
GOOGLE_DEFAULTS = {
    "pagespeed_performance_threshold": 0.9,
    "pagespeed_seo_threshold": 0.9,
    "pagespeed_accessibility_threshold": 0.9,
    "pagespeed_best_practices_threshold": 0.9,
    "core_web_vitals_lcp_threshold_ms": 2500,
    "core_web_vitals_cls_threshold": 0.1,
    "core_web_vitals_tbt_threshold_ms": 200,
}

EDITABLE_KEYS = {
    "pagespeed_performance_threshold": {"label": "PageSpeed: Performance", "type": "score"},
    "pagespeed_seo_threshold": {"label": "PageSpeed: SEO", "type": "score"},
    "pagespeed_accessibility_threshold": {"label": "PageSpeed: Accessibility", "type": "score"},
    "pagespeed_best_practices_threshold": {"label": "PageSpeed: Best Practices", "type": "score"},
    "core_web_vitals_lcp_threshold_ms": {"label": "Core Web Vitals: LCP (ms)", "type": "lcp_ms"},
    "core_web_vitals_cls_threshold": {"label": "Core Web Vitals: CLS", "type": "cls"},
    "core_web_vitals_tbt_threshold_ms": {"label": "Core Web Vitals: TBT (ms)", "type": "tbt_ms"},
    "lead_discovery_min_qualifying_leads": {"label": "Lead Discovery Quota", "type": "quota"},
    # --- Sprint 22: the rest of config.yaml's levers, same validated pattern ---
    "daily_send_limit": {"label": "Daily New-Lead Send Limit", "type": "send_limit"},
    "cold_email_target_local_hour": {"label": "Cold Email Target Local Hour", "type": "hour_of_day"},
    "cold_email_send_window_end_hour": {"label": "Cold Email Send Window End Hour", "type": "hour_of_day"},
    "min_signals_failed": {"label": "Min Signals Failed (Box 1 threshold)", "type": "signal_count"},
    "pagespeed_recheck_delay_seconds": {"label": "PageSpeed Recheck Delay (s)", "type": "delay_seconds"},
    "lead_discovery_max_total_checked": {"label": "Lead Discovery Max Checked", "type": "checked_quota"},
    "competitor_rank_threshold": {"label": "Competitor Rank Threshold", "type": "rank_threshold"},
    "competitor_review_delta_threshold": {"label": "Competitor Review Delta Threshold", "type": "review_delta"},
    "orchestrator_lock_max_age_seconds": {"label": "Orchestrator Lock Max Age (s)", "type": "lock_seconds"},
    "operator_email": {"label": "Operator Email", "type": "email"},
    # --- 2026-08-31: grid-based Lead Discovery + the hard Places API budget ---
    "discovery_grid_cell_radius_meters": {"label": "Discovery Grid Cell Radius (m)", "type": "grid_radius_meters"},
    "places_api_monthly_cap": {"label": "Places API Monthly Call Cap", "type": "places_cap"},
}

_INTEGER_TYPES = {"quota", "send_limit", "hour_of_day", "signal_count", "checked_quota", "rank_threshold", "review_delta", "lock_seconds", "grid_radius_meters", "places_cap"}


def read_current_config():
    """Fresh off disk every call — never trust an in-process cached value
    for display, since the dashboard's own process doesn't re-import
    hp_config after an edit."""
    with open(CONFIG_FILE) as f:
        return yaml.safe_load(f)


def validate(key, raw_value):
    """Returns (parsed_value, None) on success, (None, error_message) on
    failure. Never raises — callers always get a clear message back."""
    if key not in EDITABLE_KEYS:
        return None, f"Unknown setting: {key!r}"

    kind = EDITABLE_KEYS[key]["type"]

    if kind == "email":
        value = raw_value.strip()
        # Deliberately loose — this only guards against an obvious typo
        # locking every internal ops email out, not full RFC validation.
        if "@" not in value or value.startswith("@") or value.endswith("@"):
            return None, f"{EDITABLE_KEYS[key]['label']} must look like an email address"
        return value, None

    try:
        if kind in _INTEGER_TYPES:
            value = int(raw_value)
        else:
            value = float(raw_value)
    except (TypeError, ValueError):
        return None, f"{EDITABLE_KEYS[key]['label']} must be a number, got {raw_value!r}"

    if kind == "score":
        if not (0.0 <= value <= 1.0):
            return None, f"{EDITABLE_KEYS[key]['label']} must be between 0 and 1 (PageSpeed scores are 0-1)"
    elif kind == "lcp_ms":
        if not (0 < value <= 20000):
            return None, f"{EDITABLE_KEYS[key]['label']} must be between 0 and 20000ms"
    elif kind == "tbt_ms":
        if not (0 < value <= 5000):
            return None, f"{EDITABLE_KEYS[key]['label']} must be between 0 and 5000ms"
    elif kind == "cls":
        if not (0.0 <= value <= 1.0):
            return None, f"{EDITABLE_KEYS[key]['label']} must be between 0 and 1"
    elif kind == "quota":
        if not (1 <= value <= 200):
            return None, f"{EDITABLE_KEYS[key]['label']} must be a whole number between 1 and 200 (a sensible cap against accidentally quota-bombing the Places API)"
    elif kind == "send_limit":
        if not (1 <= value <= 100):
            return None, f"{EDITABLE_KEYS[key]['label']} must be a whole number between 1 and 100 (a sensible cap on real daily send volume)"
    elif kind == "hour_of_day":
        if not (0 <= value <= 23):
            return None, f"{EDITABLE_KEYS[key]['label']} must be a whole number between 0 and 23 (24-hour local time)"
    elif kind == "signal_count":
        if not (1 <= value <= 7):
            return None, f"{EDITABLE_KEYS[key]['label']} must be a whole number between 1 and 7 (there are 7 PageSpeed/Core Web Vitals signals total)"
    elif kind == "delay_seconds":
        if not (0 <= value <= 300):
            return None, f"{EDITABLE_KEYS[key]['label']} must be between 0 and 300 seconds"
    elif kind == "checked_quota":
        if not (1 <= value <= 500):
            return None, f"{EDITABLE_KEYS[key]['label']} must be a whole number between 1 and 500 (a sensible cap against accidentally quota-bombing the Places API)"
    elif kind == "rank_threshold":
        if not (1 <= value <= 100):
            return None, f"{EDITABLE_KEYS[key]['label']} must be a whole number between 1 and 100 (a Google search results position)"
    elif kind == "review_delta":
        if not (0 <= value <= 10000):
            return None, f"{EDITABLE_KEYS[key]['label']} must be a whole number between 0 and 10000"
    elif kind == "lock_seconds":
        if not (60 <= value <= 86400):
            return None, f"{EDITABLE_KEYS[key]['label']} must be between 60 seconds and 86400 seconds (24 hours)"
    elif kind == "grid_radius_meters":
        if not (500 <= value <= 50000):
            return None, f"{EDITABLE_KEYS[key]['label']} must be a whole number between 500 and 50000 meters"
    elif kind == "places_cap":
        # Deliberately capped below 5000 in the validator itself, not just
        # by convention — Cyril's explicit call (2026-08-31): must never
        # reach the real 5,000/month free-tier boundary, so the dashboard
        # can't accidentally be used to raise it there or past it.
        if not (1 <= value <= 4999):
            return None, f"{EDITABLE_KEYS[key]['label']} must be a whole number between 1 and 4999 (must stay under Google's 5,000/month free-tier boundary)"

    return value, None


def _format_yaml_scalar(key, value):
    kind = EDITABLE_KEYS[key]["type"]
    if kind == "email":
        return str(value)
    if kind in _INTEGER_TYPES:
        return str(int(value))
    return str(float(value))


def _write_config_value(key, value):
    with open(CONFIG_FILE) as f:
        lines = f.readlines()

    pattern = re.compile(rf"^{re.escape(key)}:\s*.*$")
    formatted = _format_yaml_scalar(key, value)
    replaced = False
    for i, line in enumerate(lines):
        if pattern.match(line.rstrip("\n")):
            lines[i] = f"{key}: {formatted}\n"
            replaced = True
            break

    if not replaced:
        raise ValueError(f"Could not find key {key!r} in {CONFIG_FILE} to update")

    with open(CONFIG_FILE, "w") as f:
        f.writelines(lines)


def update_setting(key, raw_value):
    """Returns (success, error_message). On success, config.yaml is
    updated and the change is recorded to the audit trail. On failure,
    config.yaml is left completely untouched."""
    value, error = validate(key, raw_value)
    if error:
        return False, error

    current = read_current_config()
    old_value = current.get(key)

    _write_config_value(key, value)
    hp_runlog.record_setting_change(key, old_value, value)

    return True, None
