#!/usr/bin/env python3
"""
Tests for scripts/ghost_site_lead_data.py — Sprint 17 prep (Track B).

Covers: parsing Business Hours' semicolon-joined string back into a list,
extracting the city half out of Source Niche/Location's combined
"{niche} / {location}" format, and assembling the full lead.json dict
from a mocked Notion page response. Built ahead of Sprint 17's actual
template/hosting choice — this piece only depends on data already in the
CRM from Sprints 2 and 4, nothing new.
"""

import os
import sys

import pytest

SCRIPTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
sys.path.insert(0, SCRIPTS_DIR)

import ghost_site_lead_data as gld  # noqa: E402


# --- parse_hours() ---

def test_parse_hours_splits_semicolon_joined_string():
    text = "Monday: 9:00 AM - 5:00 PM; Tuesday: 9:00 AM - 5:00 PM"
    assert gld.parse_hours(text) == ["Monday: 9:00 AM - 5:00 PM", "Tuesday: 9:00 AM - 5:00 PM"]


def test_parse_hours_empty_string_returns_empty_list():
    assert gld.parse_hours("") == []
    assert gld.parse_hours(None) == []


def test_parse_hours_strips_whitespace_per_entry():
    text = "Monday: 9-5;  Tuesday: 9-5  "
    assert gld.parse_hours(text) == ["Monday: 9-5", "Tuesday: 9-5"]


# --- extract_city() ---

def test_extract_city_splits_niche_and_location():
    assert gld.extract_city("Barber Shops / Ann Arbor, Michigan") == "Ann Arbor, Michigan"


def test_extract_city_handles_missing_separator():
    assert gld.extract_city("just a niche, no separator") == ""


def test_extract_city_handles_empty_input():
    assert gld.extract_city("") == ""
    assert gld.extract_city(None) == ""


# --- build_lead_json(): full assembly against a mocked Notion response ---

def _fake_notion_page(properties):
    class FakeResponse:
        status_code = 200

        def json(self):
            return {"properties": properties}

    return FakeResponse()


def test_build_lead_json_assembles_all_fields(monkeypatch):
    properties = {
        "Business Name": {"title": [{"text": {"content": "Test Barber Co"}}]},
        "Source Niche/Location": {"rich_text": [{"text": {"content": "Barber Shops / Wichita, Kansas"}}]},
        "Phone": {"phone_number": "(316) 555-0100"},
        "Hero Line": {"rich_text": [{"text": {"content": "Your neighborhood barbershop."}}]},
        "Business Hours": {"rich_text": [{"text": {"content": "Monday: 9-5; Tuesday: 9-5"}}]},
        "Review Count": {"number": 88},
        "Review Rating": {"number": 4.6},
    }
    monkeypatch.setattr(gld.requests, "get", lambda url, headers=None: _fake_notion_page(properties))

    result = gld.build_lead_json("page-123")

    assert result == {
        "business_name": "Test Barber Co",
        "city": "Wichita, Kansas",
        "phone": "(316) 555-0100",
        "hero_line": "Your neighborhood barbershop.",
        "services": [],
        "hours": ["Monday: 9-5", "Tuesday: 9-5"],
        "review_count": 88,
        "review_rating": 4.6,
    }


def test_build_lead_json_handles_missing_optional_fields_gracefully(monkeypatch):
    """A lead with no Hero Line, no hours, no reviews yet shouldn't crash —
    just comes back with empty/None values for what's missing."""
    properties = {
        "Business Name": {"title": [{"text": {"content": "Bare Bones Barber"}}]},
    }
    monkeypatch.setattr(gld.requests, "get", lambda url, headers=None: _fake_notion_page(properties))

    result = gld.build_lead_json("page-456")

    assert result["business_name"] == "Bare Bones Barber"
    assert result["city"] == ""
    assert result["phone"] is None
    assert result["hero_line"] == ""
    assert result["services"] == []
    assert result["hours"] == []
    assert result["review_count"] is None
    assert result["review_rating"] is None


def test_build_lead_json_returns_none_when_page_fetch_fails(monkeypatch):
    class FakeErrorResponse:
        status_code = 404

    monkeypatch.setattr(gld.requests, "get", lambda url, headers=None: FakeErrorResponse())
    assert gld.build_lead_json("page-does-not-exist") is None


def test_fetch_lead_page_sends_correct_request(monkeypatch):
    captured = {}

    def fake_get(url, headers=None):
        captured["url"] = url
        captured["headers"] = headers
        return _fake_notion_page({})

    monkeypatch.setattr(gld.requests, "get", fake_get)
    gld.fetch_lead_page("page-789")

    assert captured["url"].endswith("/pages/page-789")
    assert "Bearer" in captured["headers"]["Authorization"]
