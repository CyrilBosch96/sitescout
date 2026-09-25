#!/usr/bin/env python3
"""
lead.json generator — SiteScout (Sprint 17 prep, Track B)

Reads a lead's existing CRM row and formats it into the JSON structure a
ghost-site template consumes — business name, city, phone, hero line,
services, hours, review count/rating. All of this data was already
captured at Lead Discovery time (Sprint 2) or Outreach Generation (Sprint
4's Hero Line) — nothing here re-scrapes Google, it just reads what's
already in Notion and reshapes it.

This is independent of which template/hosting Sprint 17 ends up using —
built ahead of both being ready, since this half doesn't depend on
either. The exact field names below may need adjusting once Cyril's
actual Paper AI template's data-slot contract is known; this produces a
clean superset of what the CRM has today, not a guess at his template's
literal field names.

"Services List" is deliberately never populated by Lead Discovery — no
reliable Google Places source for it (Sprint 2/4 decision) — so it
always comes back as an empty list here. Not a bug; matches the rest of
the codebase's no-fabricated-facts rule.
"""

import requests

import hp_env

NOTION_API_KEY = hp_env.NOTION_API_KEY
NOTION_DATA_SOURCE_ID = hp_env.NOTION_DATA_SOURCE_ID


def _get_title(props, key):
    t = props.get(key, {}).get("title", [])
    return t[0]["text"]["content"] if t else ""

def _get_rich_text(props, key):
    t = props.get(key, {}).get("rich_text", [])
    return t[0]["text"]["content"] if t else ""

def _get_phone(props, key):
    return props.get(key, {}).get("phone_number")

def _get_number(props, key):
    return props.get(key, {}).get("number")


def parse_hours(business_hours_text):
    """Business Hours is stored as one semicolon-joined string
    ("Monday: 9 AM-5 PM; Tuesday: ...") — see format_business_hours() in
    the discovery script. Split back into a list, one entry per day, for
    the template to render as rows."""
    if not business_hours_text:
        return []
    return [line.strip() for line in business_hours_text.split(";") if line.strip()]


def extract_city(source_niche_location):
    """Source Niche/Location is stored as "{niche} / {location}" — see
    write_to_notion() in the discovery script. The location half (e.g.
    "Ann Arbor, Michigan") is the closest thing to a "city" slot the CRM
    has; no separate clean city-only field exists (a known, accepted
    limitation flagged back in the original cross-sprint dependency
    audit, not something to guess around here)."""
    if not source_niche_location or " / " not in source_niche_location:
        return ""
    return source_niche_location.split(" / ", 1)[1].strip()


def fetch_lead_page(page_id):
    url = f"https://api.notion.com/v1/pages/{page_id}"
    headers = {
        "Authorization": f"Bearer {NOTION_API_KEY}",
        "Notion-Version": "2025-09-03",
    }
    resp = requests.get(url, headers=headers)
    if resp.status_code != 200:
        return None
    return resp.json()


def build_lead_json(page_id):
    """Returns the lead.json dict for this lead, or None if the page
    can't be fetched."""
    page = fetch_lead_page(page_id)
    if not page:
        return None

    props = page.get("properties", {})
    return {
        "business_name": _get_title(props, "Business Name"),
        "city": extract_city(_get_rich_text(props, "Source Niche/Location")),
        "phone": _get_phone(props, "Phone"),
        "hero_line": _get_rich_text(props, "Hero Line"),
        "services": [],  # never populated by Lead Discovery — no reliable source
        "hours": parse_hours(_get_rich_text(props, "Business Hours")),
        "review_count": _get_number(props, "Review Count"),
        "review_rating": _get_number(props, "Review Rating"),
    }
