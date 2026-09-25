#!/usr/bin/env python3
"""
Tests for scripts/lead_discovery_and_evaluation_Contactsearch.py — Sprint 2
(Lead Discovery portion only). This file also contains Website Evaluation
(Sprint 3) and Outreach Generation (Sprint 4) logic — per Cyril's call,
kept combined rather than split, but only the Lead-Discovery-relevant
functions are tested here; classify_lead/find_contact_info/
generate_middle_continuation internals belong to Sprints 3/4.

Covers Sprint 2's AC:
  - Exhausted-flag regression test — the plan's named original bug: a
    manual run with mismatched niche/location must not write the flag
    to the wrong active job.
  - Cache hit/miss and CRM dedup (via the extracted determine_skip_reason()).
  - New business -> written to both cache and CRM.
  - Cross-sprint amendment (Gap 6): Review Count/Rating/Business Hours
    captured from the Places API response when present, gracefully
    omitted when absent.
  - Services List confirmed to stay unwritten (Cyril's call: no reliable
    Places API source, deferred to Sprint 16/17's niche-template
    defaults — this test guards against silent scope creep later).
"""

import os
import sys

import pytest

SCRIPTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
sys.path.insert(0, SCRIPTS_DIR)

import lead_discovery_and_evaluation_Contactsearch as ld  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_files(tmp_path, monkeypatch):
    monkeypatch.setattr(ld, "STATE_FILE", str(tmp_path / "state.json"))
    monkeypatch.setattr(ld, "CACHE_FILE", str(tmp_path / "checked_places_cache.json"))
    # Never let a test touch the real monthly Places API call budget file.
    monkeypatch.setattr(ld.hp_api_budget, "BUDGET_FILE", str(tmp_path / "places_api_budget.json"))
    # Never let a test touch the real shared orchestrator.lock file — main()
    # now acquires this itself (2026-09-01 fix) unless HELD_BY_PARENT_ENV_VAR
    # is set, so every plain ld.main() call in these tests goes through a
    # real acquire/release cycle against whatever hp_lock.LOCK_FILE points at.
    monkeypatch.setattr(ld.hp_lock, "LOCK_FILE", str(tmp_path / "orchestrator.lock"))
    monkeypatch.delenv(ld.hp_lock.HELD_BY_PARENT_ENV_VAR, raising=False)
    # Deterministic default: no real geocoding network call, and every
    # existing test's mocked search_places_page() already assumes the
    # original single-broad-query (no lat/lng) behavior this triggers.
    # Tests exercising the grid path override this explicitly.
    monkeypatch.setattr(ld.geo_grid, "get_city_bounds", lambda location, api_key: None)


# --- mark_exhausted_if_needed(): regression test for the plan's named bug ---

def test_exhausted_flag_written_when_job_matches_active_state():
    ld.save_state({"active_niche": "Hair Salons", "active_location": "Wichita, Kansas", "exhausted": False, "last_asked_at": None})
    ld.mark_exhausted_if_needed(qualifying_found=0, niche="Hair Salons", location="Wichita, Kansas")
    assert ld.load_state()["exhausted"] is True


def test_exhausted_flag_not_written_when_qualifying_target_met():
    ld.save_state({"active_niche": "Hair Salons", "active_location": "Wichita, Kansas", "exhausted": False, "last_asked_at": None})
    ld.mark_exhausted_if_needed(qualifying_found=ld.MIN_QUALIFYING_LEADS, niche="Hair Salons", location="Wichita, Kansas")
    assert ld.load_state()["exhausted"] is False


def test_exhausted_flag_regression_not_written_for_mismatched_manual_run():
    """
    The exact bug named in the rebuild plan: Lead Discovery run manually
    with different args than state.json's active job must not write the
    exhausted flag to the wrong city.
    """
    ld.save_state({"active_niche": "Hair Salons", "active_location": "Wichita, Kansas", "exhausted": False, "last_asked_at": None})

    ld.mark_exhausted_if_needed(qualifying_found=0, niche="Barber Shops", location="Denver, Colorado")

    state = ld.load_state()
    assert state["active_niche"] == "Hair Salons"
    assert state["active_location"] == "Wichita, Kansas"
    assert state["exhausted"] is False


# --- cache round trip ---

def test_cache_round_trip():
    ld.save_cache({"place123": "written"})
    assert ld.load_cache() == {"place123": "written"}


def test_cache_empty_when_no_file():
    assert ld.load_cache() == {}


# --- determine_skip_reason(): AC "cache hit/miss" + "CRM dedup" ---

def test_skip_reason_cached_place_id_is_skipped():
    place = {"id": "place-abc", "displayName": {"text": "New Salon"}}
    reason = ld.determine_skip_reason(place, existing_names=set(), seen_names_this_run=set(), cache={"place-abc": "not_box1"})
    assert reason == "cached"


def test_skip_reason_existing_crm_name_is_duplicate():
    place = {"id": "place-xyz", "displayName": {"text": "Joe's Barbershop"}}
    reason = ld.determine_skip_reason(place, existing_names={"Joe's Barbershop"}, seen_names_this_run=set(), cache={})
    assert reason == "duplicate"


def test_skip_reason_name_seen_earlier_this_run_is_duplicate():
    place = {"id": "place-xyz", "displayName": {"text": "Joe's Barbershop"}}
    reason = ld.determine_skip_reason(place, existing_names=set(), seen_names_this_run={"Joe's Barbershop"}, cache={})
    assert reason == "duplicate"


def test_skip_reason_genuinely_new_business_not_skipped():
    place = {"id": "place-new", "displayName": {"text": "Brand New Salon"}}
    reason = ld.determine_skip_reason(place, existing_names={"Some Other Salon"}, seen_names_this_run=set(), cache={"other-place": "written"})
    assert reason is None


# --- write_to_notion(): Gap 6 amendment ---

def _capture_notion_payload(monkeypatch):
    captured = {}

    class FakeResponse:
        status_code = 200
        text = ""

    def fake_post(url, headers=None, json=None):
        captured["url"] = url
        captured["payload"] = json
        return FakeResponse()

    monkeypatch.setattr(ld.requests, "post", fake_post)
    return captured


def _base_write_args(place):
    return dict(
        place=place, pdata=None, reason="broken", competitor=None,
        contact={"email": "owner@testsalon-fake.example.com", "social": None},
        subject="s", body="b", niche="Hair Salons", location="Wichita, KS",
    )


def test_write_to_notion_captures_review_rating_and_count(monkeypatch):
    captured = _capture_notion_payload(monkeypatch)
    place = {"displayName": {"text": "Test Salon"}, "rating": 4.7, "userRatingCount": 213}
    ld.write_to_notion(**_base_write_args(place))
    props = captured["payload"]["properties"]
    assert props["Review Rating"]["number"] == 4.7
    assert props["Review Count"]["number"] == 213


def test_write_to_notion_captures_business_hours(monkeypatch):
    captured = _capture_notion_payload(monkeypatch)
    place = {
        "displayName": {"text": "Test Salon"},
        "regularOpeningHours": {"weekdayDescriptions": ["Monday: 9:00 AM – 5:00 PM", "Tuesday: 9:00 AM – 5:00 PM"]},
    }
    ld.write_to_notion(**_base_write_args(place))
    content = captured["payload"]["properties"]["Business Hours"]["rich_text"][0]["text"]["content"]
    assert "Monday" in content
    assert "Tuesday" in content


def test_write_to_notion_omits_review_and_hours_gracefully_when_absent(monkeypatch):
    captured = _capture_notion_payload(monkeypatch)
    place = {"displayName": {"text": "Test Salon"}}  # no rating, no hours at all
    ld.write_to_notion(**_base_write_args(place))
    props = captured["payload"]["properties"]
    assert "Review Rating" not in props
    assert "Review Count" not in props
    assert "Business Hours" not in props


def test_write_to_notion_never_writes_services_list(monkeypatch):
    """Cyril's call: no reliable Places API source for this — stays
    unwritten, deferred to Sprint 16/17's niche-template defaults. This
    guards against silently fabricating it later."""
    captured = _capture_notion_payload(monkeypatch)
    place = {"displayName": {"text": "Test Salon"}, "rating": 4.5, "userRatingCount": 50}
    ld.write_to_notion(**_base_write_args(place))
    assert "Services List" not in captured["payload"]["properties"]


# --- write_to_notion(): Enhanced Website-Based Lead Qualification epic
# (2026-08-30) — crawl/booking/call-CTA/opportunity fields. CMS Status/Name
# are no longer written at all (CMS detection removed entirely, Cyril's
# call 2026-08-31). ---

def test_write_to_notion_captures_crawl_and_opportunity_fields(monkeypatch):
    captured = _capture_notion_payload(monkeypatch)
    place = {"displayName": {"text": "Test Salon"}}
    crawl_result = {
        "crawl_status": "Success", "crawled_at": "2026-08-30T12:00:00+00:00",
        "booking_status": "Booking Not Detected", "booking_provider": None,
        "call_cta_status": "Call CTA Detected",
    }
    opportunity = {"score": 30, "category": "Medium", "factors": {}}
    ld.write_to_notion(
        **_base_write_args(place), crawl_result=crawl_result,
        opportunity=opportunity, qualification_reason="MEDIUM OPPORTUNITY\n...",
    )
    props = captured["payload"]["properties"]
    assert props["Crawl Status"]["select"]["name"] == "Success"
    assert props["Last Crawled"]["date"]["start"] == "2026-08-30"
    assert "CMS Status" not in props
    assert "CMS Name" not in props
    assert props["Booking Status"]["select"]["name"] == "Booking Not Detected"
    assert "Booking Provider" not in props  # None -> omitted, same pattern as every other optional field
    assert props["Call CTA Status"]["select"]["name"] == "Call CTA Detected"
    assert props["Opportunity Score"]["number"] == 30
    assert props["Opportunity Category"]["select"]["name"] == "Medium"
    assert props["Qualification Reason"]["rich_text"][0]["text"]["content"] == "MEDIUM OPPORTUNITY\n..."


def test_write_to_notion_omits_crawl_fields_when_none(monkeypatch):
    captured = _capture_notion_payload(monkeypatch)
    place = {"displayName": {"text": "Test Salon"}}
    ld.write_to_notion(**_base_write_args(place))  # crawl_result/opportunity/qualification_reason all default None
    props = captured["payload"]["properties"]
    for key in ("Crawl Status", "CMS Status", "Booking Status", "Call CTA Status", "Opportunity Score", "Qualification Reason"):
        assert key not in props


# --- search_places_page(): field mask includes regularOpeningHours ---

def test_search_places_page_field_mask_requests_business_hours(monkeypatch):
    captured = {}

    class FakeResponse:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"places": [], "nextPageToken": None}

    def fake_post(url, headers=None, json=None):
        captured["headers"] = headers
        return FakeResponse()

    monkeypatch.setattr(ld.requests, "post", fake_post)
    ld.search_places_page("Hair Salons", "Wichita, Kansas")
    assert "places.regularOpeningHours" in captured["headers"]["X-Goog-FieldMask"]


# --- search_places_page(): grid-cell search (locationRestriction) and the
# hard monthly Places API budget, added 2026-08-31 ---

def _fake_places_response(monkeypatch, places=None, next_token=None):
    class FakeResponse:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"places": places or [], "nextPageToken": next_token}

    captured = {}

    def fake_post(url, headers=None, json=None):
        captured["body"] = json
        return FakeResponse()

    monkeypatch.setattr(ld.requests, "post", fake_post)
    return captured


def test_search_places_page_with_lat_lng_uses_location_restriction(monkeypatch):
    """Regression, found live 2026-09-01: Places API (New) Text Search's
    locationRestriction only accepts a "rectangle" shape — a "circle"
    (valid for the separate, soft locationBias field) is a real 400
    INVALID_ARGUMENT here, confirmed against the live API."""
    captured = _fake_places_response(monkeypatch)
    ld.search_places_page("Barber Shops", "New York, New York", lat=40.7, lng=-74.0, radius_meters=3000)
    body = captured["body"]
    assert body["textQuery"] == "Barber Shops"  # niche only, not "niche in location"
    rect = body["locationRestriction"]["rectangle"]
    assert rect["low"]["latitude"] < 40.7 < rect["high"]["latitude"]
    assert rect["low"]["longitude"] < -74.0 < rect["high"]["longitude"]


def test_search_places_page_without_lat_lng_uses_broad_query(monkeypatch):
    captured = _fake_places_response(monkeypatch)
    ld.search_places_page("Barber Shops", "New York, New York")
    body = captured["body"]
    assert body["textQuery"] == "Barber Shops in New York, New York"
    assert "locationRestriction" not in body


def test_search_places_page_records_a_call_on_success(monkeypatch):
    _fake_places_response(monkeypatch)
    ld.search_places_page("Barber Shops", "New York, New York")
    assert ld.hp_api_budget.calls_made_this_month() == 1


def test_search_places_page_raises_when_budget_exhausted(monkeypatch):
    """Core regression, Cyril's explicit call (2026-08-31): must never
    make the 5,000th real Places call in a month — the check has to
    happen BEFORE the real HTTP request, not after."""
    monkeypatch.setattr(ld.hp_api_budget, "MONTHLY_CAP", 1)
    _fake_places_response(monkeypatch)
    ld.search_places_page("Barber Shops", "New York, New York")  # uses up the 1 allowed call

    def post_should_not_be_called(*a, **k):
        raise AssertionError("requests.post() called despite the budget being exhausted")

    monkeypatch.setattr(ld.requests, "post", post_should_not_be_called)
    with pytest.raises(ld.PlacesBudgetExhausted):
        ld.search_places_page("Barber Shops", "New York, New York")


# --- discover_places(): grid-based coverage past Text Search's own
# ~60-result-per-query cap, added 2026-08-31 ---

def test_discover_places_falls_back_to_broad_query_when_geocoding_fails(monkeypatch):
    monkeypatch.setattr(ld.geo_grid, "get_city_bounds", lambda location, api_key: None)
    calls = []

    def fake_search(niche, location, page_token=None, lat=None, lng=None, radius_meters=None):
        calls.append({"lat": lat, "lng": lng})
        return [{"id": "p1"}], None

    monkeypatch.setattr(ld, "search_places_page", fake_search)
    stop_reason = {"reason": None}
    results = list(ld.discover_places("Barber Shops", "Smalltown, KS", stop_reason))

    assert len(calls) == 1
    assert calls[0]["lat"] is None  # no geocoding -> the original ungridded query
    assert stop_reason["reason"] == "market_exhausted"


def test_discover_places_uses_grid_cells_when_geocoding_succeeds(monkeypatch):
    monkeypatch.setattr(ld.geo_grid, "get_city_bounds", lambda location, api_key: {"north": 1, "south": 0, "east": 1, "west": 0})
    monkeypatch.setattr(ld.geo_grid, "generate_grid_points", lambda bounds, radius: [(0.1, 0.1), (0.2, 0.2)])
    calls = []

    def fake_search(niche, location, page_token=None, lat=None, lng=None, radius_meters=None):
        calls.append((lat, lng))
        return [], None

    monkeypatch.setattr(ld, "search_places_page", fake_search)
    stop_reason = {"reason": None}
    list(ld.discover_places("Barber Shops", "New York, New York", stop_reason))

    assert calls == [(0.1, 0.1), (0.2, 0.2)]
    assert stop_reason["reason"] == "market_exhausted"


def test_discover_places_yields_place_with_its_own_page_for_competitor_ranking(monkeypatch):
    """find_competitor() ranks a lead against the other businesses in the
    same page — the generator must hand callers that same page list
    alongside each place, not just the bare place dict."""
    monkeypatch.setattr(ld.geo_grid, "get_city_bounds", lambda location, api_key: None)
    page = [{"id": "p1"}, {"id": "p2"}]
    monkeypatch.setattr(ld, "search_places_page", lambda *a, **k: (page, None))
    stop_reason = {"reason": None}
    results = list(ld.discover_places("Barber Shops", "Smalltown, KS", stop_reason))

    assert results == [({"id": "p1"}, page), ({"id": "p2"}, page)]


def test_discover_places_stops_and_sets_budget_exhausted_reason(monkeypatch):
    monkeypatch.setattr(ld.geo_grid, "get_city_bounds", lambda location, api_key: None)

    def fake_search(*a, **k):
        raise ld.PlacesBudgetExhausted()

    monkeypatch.setattr(ld, "search_places_page", fake_search)
    stop_reason = {"reason": None}
    results = list(ld.discover_places("Barber Shops", "New York, New York", stop_reason))

    assert results == []
    assert stop_reason["reason"] == "budget_exhausted"


def test_main_does_not_mark_exhausted_when_budget_runs_out(monkeypatch):
    """Core regression, Cyril's explicit call (2026-08-31): hitting this
    month's own call budget must never be treated as "this market has no
    more leads" — the flag must stay unset so the same niche/location
    retries for real once the budget resets, instead of getting passed
    over for a different city."""
    monkeypatch.setattr(ld.hp_llm, "GEMINI_API_KEY", "fake-test-key")
    ld.save_state({"active_niche": "Barber Shops", "active_location": "New York, New York", "exhausted": False, "last_asked_at": None})

    monkeypatch.setattr(ld, "get_existing_business_names", lambda: set())

    def fake_discover(niche, location, stop_reason):
        stop_reason["reason"] = "budget_exhausted"
        return iter([])

    monkeypatch.setattr(ld, "discover_places", fake_discover)
    monkeypatch.setattr(sys, "argv", ["lead_discovery_and_evaluation_Contactsearch.py", "Barber Shops", "New York, New York"])

    ld.main()

    assert ld.load_state()["exhausted"] is False


# --- main() happy path: AC "new business -> added to both cache and CRM" ---

def test_main_raises_loudly_when_gemini_key_missing(monkeypatch):
    """Regression, found live 2026-08-30: GEMINI_API_KEY was never
    actually set on either machine since the Ollama->Gemini switch
    (2026-08-20), so every generate_middle_continuation() call silently
    returned None and every single qualifying lead got discarded right
    before it would have been written to Notion — no error, no crash, 10
    days of leads lost with a "success" status in state.db. This must
    fail loudly (a real exception main()'s hp_runlog.run_wrapped()
    wrapper turns into a real failure-alert email) rather than silently
    discarding every lead it finds."""
    monkeypatch.setattr(ld.hp_llm, "GEMINI_API_KEY", None)
    monkeypatch.setattr(sys, "argv", ["lead_discovery_and_evaluation_Contactsearch.py", "Hair Salons", "Wichita, Kansas"])

    with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
        ld.main()


def test_main_happy_path_writes_new_business_to_cache_and_crm(monkeypatch):
    monkeypatch.setattr(ld.hp_llm, "GEMINI_API_KEY", "fake-test-key")
    place = {
        "id": "place-happy-path",
        "displayName": {"text": "Happy Path Salon"},
        "websiteUri": "https://happypathsalon-fake.example.com",
        "formattedAddress": "123 Main St, Wichita, KS 67202",
        "rating": 4.2,
        "userRatingCount": 88,
    }

    monkeypatch.setattr(ld, "get_existing_business_names", lambda: set())
    monkeypatch.setattr(ld, "search_places_page", lambda niche, location, page_token=None: ([place], None))
    monkeypatch.setattr(ld, "classify_lead", lambda p: (True, None, "broken"))
    monkeypatch.setattr(ld, "find_contact_info", lambda url: {"email": "owner@happypathsalon-fake.example.com", "social": None})
    monkeypatch.setattr(ld, "scrape_website_content_snapshot", lambda url: None)
    monkeypatch.setattr(ld.website_crawler, "crawl_and_qualify", lambda url: {
        "crawl_status": "Success", "crawled_at": "2026-08-30T00:00:00+00:00",
        "pages_crawled": [], "booking_status": "Booking Not Detected",
        "booking_provider": None, "booking_source": None,
        "call_cta_status": "Call CTA Not Detected", "call_cta_has_tel_link": False,
        "call_cta_evidence": None,
    })
    monkeypatch.setattr(ld, "generate_middle_continuation", lambda *a, **k: "is completely down right now.")

    notion_calls = []
    monkeypatch.setattr(ld, "write_to_notion", lambda *a, **k: (notion_calls.append((a, k)), True)[1])

    monkeypatch.setattr(sys, "argv", ["lead_discovery_and_evaluation_Contactsearch.py", "Hair Salons", "Wichita, Kansas"])

    ld.main()

    assert ld.load_cache().get("place-happy-path") == "written"
    assert len(notion_calls) == 1


def test_main_refreshes_lock_while_processing_each_place(monkeypatch):
    """Regression, found live 2026-08-31: a real Lead Discovery run
    against a large market (Los Angeles) legitimately took over 30
    minutes, so a second orchestrator cycle treated the still-active
    lock as stale and started a genuinely concurrent duplicate run.
    main()'s per-place loop must call hp_lock.refresh_lock() to keep
    the lock alive for as long as it's genuinely still working."""
    monkeypatch.setattr(ld.hp_llm, "GEMINI_API_KEY", "fake-test-key")
    place = {
        "id": "place-lock-refresh",
        "displayName": {"text": "Lock Refresh Salon"},
        "websiteUri": "https://lockrefreshsalon-fake.example.com",
        "formattedAddress": "123 Main St, Wichita, KS 67202",
    }

    monkeypatch.setattr(ld, "get_existing_business_names", lambda: set())
    monkeypatch.setattr(ld, "search_places_page", lambda niche, location, page_token=None: ([place], None))
    monkeypatch.setattr(ld, "classify_lead", lambda p: (True, None, "broken"))
    monkeypatch.setattr(ld, "find_contact_info", lambda url: {"email": "owner@lockrefreshsalon-fake.example.com", "social": None})
    monkeypatch.setattr(ld, "scrape_website_content_snapshot", lambda url: None)
    monkeypatch.setattr(ld.website_crawler, "crawl_and_qualify", lambda url: {
        "crawl_status": "Success", "crawled_at": "2026-08-30T00:00:00+00:00",
        "pages_crawled": [], "booking_status": "Booking Not Detected",
        "booking_provider": None, "booking_source": None,
        "call_cta_status": "Call CTA Not Detected", "call_cta_has_tel_link": False,
        "call_cta_evidence": None,
    })
    monkeypatch.setattr(ld, "generate_middle_continuation", lambda *a, **k: "is completely down right now.")
    monkeypatch.setattr(ld, "write_to_notion", lambda *a, **k: True)

    refresh_calls = []
    monkeypatch.setattr(ld.hp_lock, "refresh_lock", lambda: refresh_calls.append(True))
    monkeypatch.setattr(sys, "argv", ["lead_discovery_and_evaluation_Contactsearch.py", "Hair Salons", "Wichita, Kansas"])

    ld.main()

    assert len(refresh_calls) >= 1


# --- main()'s own lock acquisition, added 2026-09-01: found live that a
# standalone/manual invocation of this script (bypassing orchestrator.py)
# never acquired the shared lock at all, so it raced a concurrent
# orchestrator cycle unprotected and both independently double-wrote the
# same qualifying businesses to Notion. ---

def test_main_acquires_and_releases_lock_when_run_standalone(monkeypatch):
    monkeypatch.setattr(ld.hp_llm, "GEMINI_API_KEY", "fake-test-key")
    monkeypatch.setattr(ld, "get_existing_business_names", lambda: set())
    monkeypatch.setattr(ld, "search_places_page", lambda niche, location, page_token=None: ([], None))
    monkeypatch.setattr(sys, "argv", ["lead_discovery_and_evaluation_Contactsearch.py", "Hair Salons", "Wichita, Kansas"])

    assert not os.path.exists(ld.hp_lock.LOCK_FILE)
    ld.main()
    # Released again once the run completes, not left held forever.
    assert not os.path.exists(ld.hp_lock.LOCK_FILE)


def test_main_refuses_to_run_standalone_when_lock_already_held(monkeypatch):
    monkeypatch.setattr(ld.hp_llm, "GEMINI_API_KEY", "fake-test-key")
    called = []
    monkeypatch.setattr(ld, "get_existing_business_names", lambda: called.append(True))
    monkeypatch.setattr(sys, "argv", ["lead_discovery_and_evaluation_Contactsearch.py", "Hair Salons", "Wichita, Kansas"])

    ld.hp_lock.acquire_lock()  # simulate another run already holding it
    with pytest.raises(SystemExit):
        ld.main()
    assert not called  # never got past the lock check to do any real work


def test_main_skips_own_lock_when_held_by_parent_orchestrator(monkeypatch):
    """When orchestrator.py invokes this script as a subprocess, it already
    holds the lock for the whole cycle — main() must not try to re-acquire
    it (which would just see its parent's own fresh lock file and refuse
    to run), and must not release it out from under the still-running
    parent either."""
    monkeypatch.setattr(ld.hp_llm, "GEMINI_API_KEY", "fake-test-key")
    monkeypatch.setattr(ld, "get_existing_business_names", lambda: set())
    monkeypatch.setattr(ld, "search_places_page", lambda niche, location, page_token=None: ([], None))
    monkeypatch.setattr(sys, "argv", ["lead_discovery_and_evaluation_Contactsearch.py", "Hair Salons", "Wichita, Kansas"])
    monkeypatch.setenv(ld.hp_lock.HELD_BY_PARENT_ENV_VAR, "1")

    ld.hp_lock.acquire_lock()  # simulate orchestrator.py already holding it
    ld.main()
    # Still held afterward — main() must not have released its parent's lock.
    assert os.path.exists(ld.hp_lock.LOCK_FILE)


def test_main_content_gen_failure_not_cached_regression(monkeypatch):
    """Regression, found live 2026-08-30: a content-generation failure
    used to just `continue` with no counter and no cache entry (the cache
    silently-uncached part was already correct) — but this test locks in
    that it stays uncached (retryable next run, not blacklisted forever)
    and that write_to_notion() is never even attempted for it."""
    monkeypatch.setattr(ld.hp_llm, "GEMINI_API_KEY", "fake-test-key")
    place = {
        "id": "place-content-gen-fail",
        "displayName": {"text": "Gen Fail Salon"},
        "websiteUri": "https://genfailsalon-fake.example.com",
        "formattedAddress": "123 Main St, Wichita, KS 67202",
    }

    monkeypatch.setattr(ld, "get_existing_business_names", lambda: set())
    monkeypatch.setattr(ld, "search_places_page", lambda niche, location, page_token=None: ([place], None))
    monkeypatch.setattr(ld, "classify_lead", lambda p: (True, None, "broken"))
    monkeypatch.setattr(ld, "find_contact_info", lambda url: {"email": "owner@genfailsalon-fake.example.com", "social": None})
    monkeypatch.setattr(ld, "scrape_website_content_snapshot", lambda url: None)
    monkeypatch.setattr(ld.website_crawler, "crawl_and_qualify", lambda url: {
        "crawl_status": "Success", "crawled_at": "2026-08-30T00:00:00+00:00",
        "pages_crawled": [], "booking_status": "Booking Not Detected",
        "booking_provider": None, "booking_source": None,
        "call_cta_status": "Call CTA Not Detected", "call_cta_has_tel_link": False,
        "call_cta_evidence": None,
    })
    monkeypatch.setattr(ld, "generate_middle_continuation", lambda *a, **k: None)  # simulates the real 10-day-live bug

    def write_should_not_be_called(*a, **k):
        raise AssertionError("write_to_notion() called despite content generation failing")

    monkeypatch.setattr(ld, "write_to_notion", write_should_not_be_called)
    monkeypatch.setattr(sys, "argv", ["lead_discovery_and_evaluation_Contactsearch.py", "Hair Salons", "Wichita, Kansas"])

    result = ld.main()

    assert "place-content-gen-fail" not in ld.load_cache()  # retryable next run, not blacklisted
    assert result["leads_found"] == 0


def test_main_notion_write_failure_not_cached_and_not_counted_regression(monkeypatch):
    """Regression, found live 2026-08-30: qualifying_found/box1_written
    used to increment unconditionally regardless of write_to_notion()'s
    return value, and the place got cached as "written" even on failure —
    permanently hiding a genuinely good lead from every future run while
    the day's run summary claimed it as a real success."""
    monkeypatch.setattr(ld.hp_llm, "GEMINI_API_KEY", "fake-test-key")
    place = {
        "id": "place-write-fail",
        "displayName": {"text": "Write Fail Salon"},
        "websiteUri": "https://writefailsalon-fake.example.com",
        "formattedAddress": "123 Main St, Wichita, KS 67202",
    }

    monkeypatch.setattr(ld, "get_existing_business_names", lambda: set())
    monkeypatch.setattr(ld, "search_places_page", lambda niche, location, page_token=None: ([place], None))
    monkeypatch.setattr(ld, "classify_lead", lambda p: (True, None, "broken"))
    monkeypatch.setattr(ld, "find_contact_info", lambda url: {"email": "owner@writefailsalon-fake.example.com", "social": None})
    monkeypatch.setattr(ld, "scrape_website_content_snapshot", lambda url: None)
    monkeypatch.setattr(ld.website_crawler, "crawl_and_qualify", lambda url: {
        "crawl_status": "Success", "crawled_at": "2026-08-30T00:00:00+00:00",
        "pages_crawled": [], "booking_status": "Booking Not Detected",
        "booking_provider": None, "booking_source": None,
        "call_cta_status": "Call CTA Not Detected", "call_cta_has_tel_link": False,
        "call_cta_evidence": None,
    })
    monkeypatch.setattr(ld, "generate_middle_continuation", lambda *a, **k: "is completely down right now.")
    monkeypatch.setattr(ld, "write_to_notion", lambda *a, **k: False)  # simulates a real Notion API failure
    monkeypatch.setattr(sys, "argv", ["lead_discovery_and_evaluation_Contactsearch.py", "Hair Salons", "Wichita, Kansas"])

    result = ld.main()

    assert "place-write-fail" not in ld.load_cache()  # retryable next run, not permanently hidden
    assert result["leads_found"] == 0


def test_main_website_disqualified_discarded_and_cached(monkeypatch):
    """Enhanced Website-Based Lead Qualification epic (2026-08-30, rule
    updated 2026-08-31 to booking-only): the crawler-based gate is now
    the live decider. A lead with real online booking must be discarded
    and cached, even when the old PageSpeed-based classify_lead() would
    have returned box1=True — PageSpeed no longer decides who gets
    emailed."""
    monkeypatch.setattr(ld.hp_llm, "GEMINI_API_KEY", "fake-test-key")
    place = {
        "id": "place-disqualified",
        "displayName": {"text": "Sophisticated Salon"},
        "websiteUri": "https://sophisticatedsalon-fake.example.com",
        "formattedAddress": "123 Main St, Wichita, KS 67202",
    }

    monkeypatch.setattr(ld, "get_existing_business_names", lambda: set())
    monkeypatch.setattr(ld, "search_places_page", lambda niche, location, page_token=None: ([place], None))
    monkeypatch.setattr(ld, "classify_lead", lambda p: (True, None, "broken"))
    monkeypatch.setattr(ld.website_crawler, "crawl_and_qualify", lambda url: {
        "crawl_status": "Success", "crawled_at": "2026-08-30T00:00:00+00:00",
        "pages_crawled": [], "booking_status": "Booking Detected",
        "booking_provider": "Calendly", "booking_source": None,
        "call_cta_status": "Call CTA Not Detected", "call_cta_has_tel_link": False,
        "call_cta_evidence": None,
    })

    def find_contact_should_not_be_called(*a, **k):
        raise AssertionError("find_contact_info() called for a disqualified lead")

    monkeypatch.setattr(ld, "find_contact_info", find_contact_should_not_be_called)
    monkeypatch.setattr(sys, "argv", ["lead_discovery_and_evaluation_Contactsearch.py", "Hair Salons", "Wichita, Kansas"])

    result = ld.main()

    assert ld.load_cache().get("place-disqualified") == "website_disqualified"
    assert result["leads_found"] == 0


def test_main_website_unknown_skipped_uncached(monkeypatch):
    """Cyril's explicit call (2026-08-30): an Unknown website signal must
    never be treated as either Qualify or Disqualify — skip and leave
    uncached so a future run retries once the crawl can confirm."""
    monkeypatch.setattr(ld.hp_llm, "GEMINI_API_KEY", "fake-test-key")
    place = {
        "id": "place-unknown-signals",
        "displayName": {"text": "Ambiguous Salon"},
        "websiteUri": "https://ambiguoussalon-fake.example.com",
        "formattedAddress": "123 Main St, Wichita, KS 67202",
    }

    monkeypatch.setattr(ld, "get_existing_business_names", lambda: set())
    monkeypatch.setattr(ld, "search_places_page", lambda niche, location, page_token=None: ([place], None))
    monkeypatch.setattr(ld, "classify_lead", lambda p: (True, None, "broken"))
    monkeypatch.setattr(ld.website_crawler, "crawl_and_qualify", lambda url: {
        "crawl_status": "Failed", "crawled_at": None,
        "pages_crawled": [], "booking_status": "Booking Unknown",
        "booking_provider": None, "booking_source": None,
        "call_cta_status": "Call CTA Unknown", "call_cta_has_tel_link": False,
        "call_cta_evidence": None,
    })

    def find_contact_should_not_be_called(*a, **k):
        raise AssertionError("find_contact_info() called for an Unknown-signal lead")

    monkeypatch.setattr(ld, "find_contact_info", find_contact_should_not_be_called)
    monkeypatch.setattr(sys, "argv", ["lead_discovery_and_evaluation_Contactsearch.py", "Hair Salons", "Wichita, Kansas"])

    result = ld.main()

    assert "place-unknown-signals" not in ld.load_cache()  # retryable next run, not blacklisted
    assert result["leads_found"] == 0
