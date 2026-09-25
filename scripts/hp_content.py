#!/usr/bin/env python3
"""
Dashboard Content Editor backend — SiteScout (Sprint 22)

Lets Cyril edit the fixed email copy from the dashboard instead of
editing Python source and redeploying. Mirrors hp_settings.py's exact
shape (CONFIG_FILE -> registry -> validate -> write -> audit trail) —
same pattern, different backing store: these are plain files under
email_templates/ and ghost_templates/, not config.yaml keys.

Each script that sends this copy loads its file fresh on every call
(load_followup_text(), load_resurrection_template(), etc., and
ghost_site_sequence.py's pre-existing load_template()) — a save here
takes effect on the very next send, no restart needed, same as every
other file-backed template in this project.
"""

import os

import hp_env
import hp_runlog

EMAIL_TEMPLATES_DIR = os.path.join(hp_env.PROJECT_ROOT, "email_templates")
GHOST_TEMPLATES_DIR = os.path.join(hp_env.PROJECT_ROOT, "ghost_templates")

CONTENT_REGISTRY = {
    "cold_subject_broken": {
        "label": "First email — subject (site broken)", "category": "Track A — first email",
        "path": os.path.join(EMAIL_TEMPLATES_DIR, "cold_subject_broken.md"),
        "placeholders": ["[BUSINESS_NAME]"],
    },
    "cold_subject_slow": {
        "label": "First email — subject (site slow, competitor known)", "category": "Track A — first email",
        "path": os.path.join(EMAIL_TEMPLATES_DIR, "cold_subject_slow.md"),
        "placeholders": ["[BUSINESS_NAME]", "[COMPETITOR_NAME]"],
    },
    "cold_subject_slow_no_competitor": {
        "label": "First email — subject (site slow, no competitor data)", "category": "Track A — first email",
        "path": os.path.join(EMAIL_TEMPLATES_DIR, "cold_subject_slow_no_competitor.md"),
        "placeholders": ["[BUSINESS_NAME]"],
    },
    "cold_subject_seo": {
        "label": "First email — subject (hard to find online)", "category": "Track A — first email",
        "path": os.path.join(EMAIL_TEMPLATES_DIR, "cold_subject_seo.md"),
        "placeholders": ["[BUSINESS_NAME]"],
    },
    "cold_subject_quality": {
        "label": "First email — subject (site looks untrustworthy)", "category": "Track A — first email",
        "path": os.path.join(EMAIL_TEMPLATES_DIR, "cold_subject_quality.md"),
        "placeholders": ["[BUSINESS_NAME]"],
    },
    "cold_subject_no_booking": {
        "label": "First email — subject (no online booking)", "category": "Track A — first email",
        "path": os.path.join(EMAIL_TEMPLATES_DIR, "cold_subject_no_booking.md"),
        "placeholders": ["[BUSINESS_NAME]"],
    },
    "cold_subject_no_call_cta": {
        "label": "First email — subject (no click-to-call CTA)", "category": "Track A — first email",
        "path": os.path.join(EMAIL_TEMPLATES_DIR, "cold_subject_no_call_cta.md"),
        "placeholders": ["[BUSINESS_NAME]"],
    },
    "cold_body": {
        "label": "First email — body", "category": "Track A — first email",
        "path": os.path.join(EMAIL_TEMPLATES_DIR, "cold_body.md"),
        "placeholders": ["[NICHE]", "[MIDDLE_CONTINUATION]", "[TRACKING_LINK]"],
    },
    "followup_1": {
        "label": "Follow-up #1 (Day 3)", "category": "Track A follow-ups",
        "path": os.path.join(EMAIL_TEMPLATES_DIR, "followup_1.md"), "placeholders": [],
    },
    "followup_2": {
        "label": "Follow-up #2 (Day 7)", "category": "Track A follow-ups",
        "path": os.path.join(EMAIL_TEMPLATES_DIR, "followup_2.md"), "placeholders": ["[TRACKING_LINK]"],
    },
    "followup_3": {
        "label": "Follow-up #3 (Day 14)", "category": "Track A follow-ups",
        "path": os.path.join(EMAIL_TEMPLATES_DIR, "followup_3.md"), "placeholders": [],
    },
    "followup_4": {
        "label": "Follow-up #4 (Day 28)", "category": "Track A follow-ups",
        "path": os.path.join(EMAIL_TEMPLATES_DIR, "followup_4.md"), "placeholders": [],
    },
    "ghost_day1": {
        "label": "Ghost Site — Day 1", "category": "Track B preview sequence",
        "path": os.path.join(GHOST_TEMPLATES_DIR, "ghost_day1.md"),
        "placeholders": ["[BUSINESS_NAME]", "[URL]", "[EXPIRY_DATE]"],
    },
    "ghost_day4": {
        "label": "Ghost Site — Day 4", "category": "Track B preview sequence",
        "path": os.path.join(GHOST_TEMPLATES_DIR, "ghost_day4.md"),
        "placeholders": ["[BUSINESS_NAME]", "[URL]", "[EXPIRY_DATE]"],
    },
    "ghost_day5": {
        "label": "Ghost Site — Day 5", "category": "Track B preview sequence",
        "path": os.path.join(GHOST_TEMPLATES_DIR, "ghost_day5.md"),
        "placeholders": ["[BUSINESS_NAME]", "[URL]", "[EXPIRY_DATE]"],
    },
    "ghost_day6": {
        "label": "Ghost Site — Day 6", "category": "Track B preview sequence",
        "path": os.path.join(GHOST_TEMPLATES_DIR, "ghost_day6.md"),
        "placeholders": ["[BUSINESS_NAME]", "[URL]", "[EXPIRY_DATE]"],
    },
    "ghost_day7": {
        "label": "Ghost Site — Day 7", "category": "Track B preview sequence",
        "path": os.path.join(GHOST_TEMPLATES_DIR, "ghost_day7.md"),
        "placeholders": ["[BUSINESS_NAME]", "[URL]", "[EXPIRY_DATE]"],
    },
    "resurrection": {
        "label": "Resurrection email", "category": "Track B ops",
        "path": os.path.join(EMAIL_TEMPLATES_DIR, "resurrection.md"), "placeholders": ["[BUSINESS_NAME]"],
    },
    "template_request_ask": {
        "label": "Template request — ask", "category": "Track B ops",
        "path": os.path.join(EMAIL_TEMPLATES_DIR, "template_request_ask.md"),
        "placeholders": ["[NICHE]", "[LEAD_COUNT]", "[LEAD_NAMES]"],
    },
    "template_request_reminder": {
        "label": "Template request — reminder", "category": "Track B ops",
        "path": os.path.join(EMAIL_TEMPLATES_DIR, "template_request_reminder.md"), "placeholders": [],
    },
}


def read_content(key):
    """Returns the current file text, or None if the key is unknown."""
    entry = CONTENT_REGISTRY.get(key)
    if not entry:
        return None
    with open(entry["path"]) as f:
        return f.read()


def write_content(key, new_text):
    """Returns (success, error_message). On success, the file is
    overwritten and the change recorded to the audit trail. Rejects an
    empty save outright — an accidental blank save would otherwise ship
    a silent empty email the next time that template fires."""
    entry = CONTENT_REGISTRY.get(key)
    if not entry:
        return False, f"Unknown content key: {key!r}"

    if not new_text.strip():
        return False, f"{entry['label']} can't be saved empty"

    old_text = read_content(key)
    with open(entry["path"], "w") as f:
        f.write(new_text)

    hp_runlog.record_content_change(key, old_text, new_text)
    return True, None
