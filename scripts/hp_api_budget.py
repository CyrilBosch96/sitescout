#!/usr/bin/env python3
"""
Places API monthly call budget — SiteScout (2026-08-31)

Google's Places API (New) Text Search gives 5,000 free calls/month at the
tier this project uses (rating/opening-hours fields push it to Pro SKU),
then bills $32 per 1,000 calls after that. Cyril's explicit call
(2026-08-31), once discussing scaling Lead Discovery up via grid-based
city search (to get real coverage past Google's ~60-result-per-query
cap): hard-cap total Places calls at 4,999/month — never 5,000 or more,
no matter how many qualifying leads have or haven't been found yet. If
the cap is hit, discovery simply stops for the rest of the calendar
month and resumes automatically on the 1st.

Persisted as a small JSON file (same pattern as state.json/
checked_places_cache.json) rather than state.db, since this is a single
running counter keyed by month, not an append-only log.
"""

import json
import os
from datetime import datetime, timezone

import hp_env
import hp_config

BUDGET_FILE = os.path.join(hp_env.PROJECT_ROOT, "places_api_budget.json")
MONTHLY_CAP = hp_config.PLACES_API_MONTHLY_CAP


def _current_month_key():
    return datetime.now(timezone.utc).strftime("%Y-%m")


def _load():
    if not os.path.exists(BUDGET_FILE):
        return {"month": _current_month_key(), "calls": 0}
    with open(BUDGET_FILE) as f:
        data = json.load(f)
    # Calendar month rolled over since the last recorded call — the
    # counter resets automatically, no manual reset needed.
    if data.get("month") != _current_month_key():
        return {"month": _current_month_key(), "calls": 0}
    return data


def _save(data):
    with open(BUDGET_FILE, "w") as f:
        json.dump(data, f, indent=2)


def calls_made_this_month():
    return _load()["calls"]


def can_make_call():
    """True if another Places API call would stay under the hard monthly
    cap. Must be checked BEFORE every real call — this function alone
    never makes or counts a call, see record_call()."""
    return calls_made_this_month() < MONTHLY_CAP


def record_call():
    """Call exactly once per real Places API request actually made, right
    after it succeeds. Never called speculatively — an uncounted call
    would let the tracker under-report real usage."""
    data = _load()
    data["calls"] += 1
    _save(data)
