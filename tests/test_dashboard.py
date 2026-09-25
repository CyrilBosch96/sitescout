#!/usr/bin/env python3
"""
Tests for dashboard/app.py — Sprint 13 (Dashboard).

Uses Flask's test client (app.test_client()) — no live server/port needed.

Covers Sprint 13's AC:
  - state.db history -> status grid shows accurate last-run time, status,
    duration.
  - A recently-failed script appears in the failures panel with its error.
  - A manual trigger runs the script and the grid reflects it afterward
    (verified here as: the correct subprocess call happens and the
    response redirects back to the grid).
  - (Gap 7) previews-activated/conversions-this-week are their own figures.
  - (Gap 8) bulk-select leads and set Ghost Site Status to Selected.
  - (Gap 9) "today's numbers" reuses daily_reporting.build_report()
    directly rather than recomputing the same aggregation independently.
"""

import os
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone

import pytest

DASHBOARD_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dashboard")
SCRIPTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
sys.path.insert(0, DASHBOARD_DIR)
sys.path.insert(0, SCRIPTS_DIR)

import app as dashboard_app  # noqa: E402
import hp_runlog  # noqa: E402
import hp_settings  # noqa: E402

_real_get_leads = dashboard_app.get_leads  # captured before the autouse fixture below patches it

FIXTURE_CONFIG = """\
# Comment that must survive every settings-panel edit.
pagespeed_performance_threshold: 0.9
pagespeed_seo_threshold: 0.9
pagespeed_accessibility_threshold: 0.9
pagespeed_best_practices_threshold: 0.9
core_web_vitals_lcp_threshold_ms: 2500
core_web_vitals_cls_threshold: 0.1
core_web_vitals_tbt_threshold_ms: 200
lead_discovery_min_qualifying_leads: 20
operator_email: operator@example.com
"""


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(hp_runlog, "DB_FILE", str(tmp_path / "state.db"))
    # hp_status.py owns its own DB_FILE/CACHE_FILE constants (not shared
    # with hp_runlog) — index() calls it on every load, so leaving these
    # unpatched would hit the real production state.db/cache file.
    monkeypatch.setattr(dashboard_app.hp_status, "DB_FILE", str(tmp_path / "state.db"))
    monkeypatch.setattr(dashboard_app.hp_status, "CACHE_FILE", str(tmp_path / "checked_places_cache.json"))
    monkeypatch.setattr(dashboard_app.hp_lock, "LOCK_FILE", str(tmp_path / "orchestrator.lock"))


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(FIXTURE_CONFIG)
    monkeypatch.setattr(hp_settings, "CONFIG_FILE", str(config_path))
    return config_path


@pytest.fixture(autouse=True)
def no_real_notion_leads(monkeypatch):
    """Most tests don't care about the lead table — stub it to empty so
    they don't depend on real network calls. Tests that DO care about
    leads override this explicitly."""
    monkeypatch.setattr(dashboard_app, "get_leads", lambda: [])


@pytest.fixture(autouse=True)
def no_real_upcoming_sends_preview(monkeypatch):
    """cold_email.preview_upcoming_sends() hits real Notion (get_box1_leads
    + count_todays_new_sends) — index() calls it every load, so stub it
    the same way no_real_notion_leads stubs get_leads()."""
    monkeypatch.setattr(dashboard_app.cold_email, "get_box1_leads", lambda: [])
    monkeypatch.setattr(dashboard_app.cold_email, "count_todays_new_sends", lambda: 0)


@pytest.fixture(autouse=True)
def no_real_daily_report(monkeypatch):
    """Same idea for today's-numbers — stub a fixed report unless a test
    is specifically checking the Gap 9 reuse behavior."""
    monkeypatch.setattr(dashboard_app.daily_reporting, "build_report", lambda target_date: {
        "date": target_date, "sent": 0, "followups": {1: 0, 2: 0, 3: 0, 4: 0},
        "replies_needing_review": 0, "previews_activated_this_week": 0, "conversions_this_week": 0,
    })


@pytest.fixture
def client():
    dashboard_app.app.config["TESTING"] = True
    return dashboard_app.app.test_client()


def _insert_run(script_name, status, started_at, finished_at=None, error_message=None, notes=None):
    conn = hp_runlog._connect()
    conn.execute(
        "INSERT INTO runs (script_name, started_at, finished_at, status, error_message, notes) VALUES (?, ?, ?, ?, ?, ?)",
        (script_name, started_at, finished_at, status, error_message, notes),
    )
    conn.commit()
    conn.close()


# --- Status grid: AC "accurate last-run time, status, duration" ---

def test_status_grid_shows_never_run_for_untouched_scripts():
    grid = dashboard_app.get_status_grid()
    assert len(grid) == len(dashboard_app.SCRIPTS)
    assert all(row["status"] == "never run" for row in grid)


def test_status_grid_reflects_real_run_with_correct_duration():
    started = datetime(2026, 8, 12, 9, 0, 0)
    finished = started + timedelta(seconds=45)
    _insert_run("queue_check", "success", started.isoformat(), finished.isoformat(), notes="ask email sent")

    grid = dashboard_app.get_status_grid()
    row = next(r for r in grid if r["script_name"] == "queue_check")
    assert row["status"] == "success"
    assert row["duration_seconds"] == 45.0
    assert row["notes"] == "ask email sent"


def test_status_grid_uses_most_recent_row_when_a_script_ran_twice():
    _insert_run("cold_email", "fail", "2026-08-12T08:00:00", "2026-08-12T08:00:05", error_message="boom")
    _insert_run("cold_email", "success", "2026-08-12T09:00:00", "2026-08-12T09:00:10", notes="all sent")

    grid = dashboard_app.get_status_grid()
    row = next(r for r in grid if r["script_name"] == "cold_email")
    assert row["status"] == "success"
    assert row["notes"] == "all sent"


# --- Failures panel: AC "appears in the failures panel with its error" ---

def test_failures_panel_shows_only_fail_rows():
    _insert_run("cold_email", "success", "2026-08-12T08:00:00", "2026-08-12T08:00:05")
    _insert_run("orchestrator", "fail", "2026-08-12T09:00:00", "2026-08-12T09:00:02", error_message="ValueError: boom")

    failures = dashboard_app.get_recent_failures()
    assert len(failures) == 1
    assert failures[0]["script_name"] == "orchestrator"
    assert failures[0]["error_message"] == "ValueError: boom"


def test_failures_panel_respects_limit():
    for i in range(5):
        _insert_run("cold_email", "fail", f"2026-08-12T0{i}:00:00", f"2026-08-12T0{i}:00:05", error_message=f"err{i}")
    failures = dashboard_app.get_recent_failures(limit=2)
    assert len(failures) == 2


def test_index_route_renders_failures(client):
    _insert_run("orchestrator", "fail", "2026-08-12T09:00:00", "2026-08-12T09:00:02", error_message="ValueError: boom")
    resp = client.get("/")
    assert resp.status_code == 200
    assert b"ValueError: boom" in resp.data


def test_index_shows_real_hp_env_not_a_hardcoded_default(monkeypatch, client):
    """Regression: config_env used to never be passed to the template at
    all, so the page always showed its hardcoded fallback text
    ('development') regardless of the real environment — harmless when
    the two happened to match, actively misleading once a real
    production dashboard exists side-by-side with dev."""
    monkeypatch.setattr(dashboard_app.hp_env, "HP_ENV", "production")
    resp = client.get("/")
    assert b'class="env-tag">production' in resp.data


# --- Manual trigger: AC "script completes, grid updates" ---

def test_trigger_invokes_correct_script_and_redirects(monkeypatch):
    captured = {}
    monkeypatch.setattr(dashboard_app, "trigger_script", lambda name, extra_args=None: (captured.update(name=name, args=extra_args), (True, None))[1])

    dashboard_app.app.config["TESTING"] = True
    client = dashboard_app.app.test_client()
    resp = client.post("/trigger/queue_check", follow_redirects=False)

    assert resp.status_code == 302
    assert resp.headers["Location"] == "/"
    assert captured["name"] == "queue_check"


def test_trigger_lead_discovery_requires_niche_and_location(monkeypatch):
    monkeypatch.setattr(dashboard_app, "trigger_script", lambda *a, **k: (True, None))
    dashboard_app.app.config["TESTING"] = True
    client = dashboard_app.app.test_client()

    resp = client.post("/trigger/lead_discovery_and_evaluation_Contactsearch", data={})
    assert resp.status_code == 400


def test_trigger_lead_discovery_passes_niche_and_location_as_args(monkeypatch):
    captured = {}
    monkeypatch.setattr(dashboard_app, "trigger_script", lambda name, extra_args=None: (captured.update(name=name, args=extra_args), (True, None))[1])
    dashboard_app.app.config["TESTING"] = True
    client = dashboard_app.app.test_client()

    resp = client.post(
        "/trigger/lead_discovery_and_evaluation_Contactsearch",
        data={"niche": "Hair Salons", "location": "Wichita, KS"},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert captured["args"] == ["Hair Salons", "Wichita, KS"]


def test_trigger_unknown_script_returns_404(monkeypatch):
    monkeypatch.setattr(dashboard_app, "trigger_script", lambda *a, **k: (True, None))
    dashboard_app.app.config["TESTING"] = True
    client = dashboard_app.app.test_client()
    resp = client.post("/trigger/not_a_real_script")
    assert resp.status_code == 404


def test_trigger_blocked_when_lock_held_returns_409(monkeypatch):
    monkeypatch.setattr(dashboard_app, "trigger_script", lambda *a, **k: (False, "Another pipeline run is already in progress — try again shortly."))
    dashboard_app.app.config["TESTING"] = True
    client = dashboard_app.app.test_client()
    resp = client.post("/trigger/queue_check")
    assert resp.status_code == 409


def test_trigger_script_invokes_subprocess_with_correct_path(monkeypatch, tmp_path):
    monkeypatch.setattr(dashboard_app.hp_lock, "LOCK_FILE", str(tmp_path / "orchestrator.lock"))
    # Run the backgrounded call synchronously so the assertion below isn't
    # racing a real thread (2026-08-18: trigger_script() no longer blocks
    # on subprocess.run(), see test_trigger_script_does_not_block_caller).
    monkeypatch.setattr(dashboard_app, "_start_background", lambda target, args: target(*args))
    captured = {}

    def fake_run(args, env=None):
        captured["args"] = args
        return None

    monkeypatch.setattr(dashboard_app.subprocess, "run", fake_run)
    ok, error = dashboard_app.trigger_script("inbox_monitoring")

    assert ok is True
    assert error is None
    # sys.executable, not bare "python3" — regression test for a real bug
    # found during live verification: launchd's minimal PATH resolved bare
    # "python3" to a stray /usr/local/bin/python3 (missing pyyaml/flask)
    # ahead of the real /opt/homebrew/bin/python3 this project installs into.
    assert captured["args"][0] == dashboard_app.sys.executable
    assert captured["args"][1].endswith("inbox_monitoring.py")


def test_trigger_script_returns_false_when_lock_held(monkeypatch, tmp_path):
    lock_file = str(tmp_path / "orchestrator.lock")
    monkeypatch.setattr(dashboard_app.hp_lock, "LOCK_FILE", lock_file)
    with open(lock_file, "w") as f:
        f.write("123")  # fresh lock, well within LOCK_MAX_AGE_SECONDS

    called = {"subprocess_ran": False}
    monkeypatch.setattr(dashboard_app, "_start_background", lambda target, args: called.update(background_started=True))
    monkeypatch.setattr(dashboard_app.subprocess, "run", lambda *a, **k: called.update(subprocess_ran=True))

    ok, error = dashboard_app.trigger_script("inbox_monitoring")

    assert ok is False
    assert error
    assert called["subprocess_ran"] is False
    assert "background_started" not in called  # never even attempted


def test_trigger_script_does_not_block_caller(monkeypatch, tmp_path):
    """The actual point of backgrounding (2026-08-18): trigger_script()
    must return before the script itself finishes, not after — otherwise
    the dashboard request still hangs for the full run, same as before."""
    monkeypatch.setattr(dashboard_app.hp_lock, "LOCK_FILE", str(tmp_path / "orchestrator.lock"))
    started_real_thread = {}

    class _FakeThread:
        def __init__(self, target, args, daemon):
            started_real_thread["thread"] = True

        def start(self):
            pass  # never actually invokes target — that's the whole point

    monkeypatch.setattr(dashboard_app.threading, "Thread", _FakeThread)

    def slow_run(*a, **k):
        raise AssertionError("subprocess.run() called synchronously on the request thread")

    monkeypatch.setattr(dashboard_app.subprocess, "run", slow_run)

    ok, error = dashboard_app.trigger_script("inbox_monitoring")

    assert ok is True
    assert started_real_thread.get("thread") is True  # a real Thread was constructed, just never actually run here


# --- Gap 7: previews/conversions as their own figures ---

def test_index_renders_previews_and_conversions_as_own_figures(monkeypatch, client):
    monkeypatch.setattr(dashboard_app.daily_reporting, "build_report", lambda target_date: {
        "date": target_date, "sent": 3, "followups": {1: 1, 2: 0, 3: 0, 4: 0},
        "replies_needing_review": 2, "previews_activated_this_week": 6, "conversions_this_week": 1,
    })
    resp = client.get("/")
    html = resp.data.decode()
    assert "6" in html and "Previews activated" in html
    assert "1" in html and "Conversions" in html


# --- Gap 9: reuse Sprint 9's aggregation, don't recompute independently ---

def test_index_calls_daily_reporting_build_report_directly(monkeypatch, client):
    calls = []

    def spy_build_report(target_date):
        calls.append(target_date)
        return {
            "date": target_date, "sent": 0, "followups": {1: 0, 2: 0, 3: 0, 4: 0},
            "replies_needing_review": 0, "previews_activated_this_week": 0, "conversions_this_week": 0,
        }

    monkeypatch.setattr(dashboard_app.daily_reporting, "build_report", spy_build_report)
    client.get("/")
    assert len(calls) == 1


# --- Gap 8: bulk-select leads, set Ghost Site Status to Selected ---

def _lead(page_id, name, ghost_site_status=None, contact_status="Not Contacted", box="Box 1"):
    return {"page_id": page_id, "name": name, "box": box, "contact_status": contact_status, "ghost_site_status": ghost_site_status}


# --- Candidate pool vs Selected Leads split (2026-08-17) ---

def test_index_shows_only_leads_with_no_ghost_site_status(monkeypatch, client):
    leads = [
        _lead("p1", "Alpha", ghost_site_status=None),
        _lead("p2", "Beta", ghost_site_status="Selected"),
        _lead("p3", "Gamma", ghost_site_status="Live"),
    ]
    monkeypatch.setattr(dashboard_app, "get_leads", lambda: leads)
    resp = client.get("/")
    html = resp.data.decode()
    assert "Alpha" in html
    assert "Beta" not in html
    assert "Gamma" not in html


def test_selected_leads_page_splits_pending_and_active(monkeypatch, client):
    leads = [
        _lead("p1", "Alpha", ghost_site_status=None),
        _lead("p2", "Beta", ghost_site_status="Selected"),
        _lead("p3", "Gamma", ghost_site_status="Live"),
        _lead("p4", "Delta", ghost_site_status="Replied"),
        _lead("p5", "Epsilon", ghost_site_status="Expired"),
    ]
    monkeypatch.setattr(dashboard_app, "get_leads", lambda: leads)
    resp = client.get("/leads/selected")
    html = resp.data.decode()
    assert "Alpha" not in html  # still a bare candidate, not Track B's concern
    assert "Beta" in html
    assert "Gamma" in html
    assert "Delta" in html
    assert "Epsilon" not in html  # Expired isn't "pending" or "active" here


def test_clear_selection_route_calls_clear_ghost_site_selection(monkeypatch):
    captured = {}
    monkeypatch.setattr(dashboard_app, "clear_ghost_site_selection", lambda page_ids: captured.update(page_ids=page_ids))
    dashboard_app.app.config["TESTING"] = True
    client = dashboard_app.app.test_client()

    resp = client.post("/leads/clear-selection", data={"page_id": ["page-1", "page-2"]}, follow_redirects=False)

    assert resp.status_code == 302
    assert resp.headers["Location"] == "/leads/selected"
    assert captured["page_ids"] == ["page-1", "page-2"]


def test_clear_selection_with_no_checkboxes_does_not_call_notion(monkeypatch):
    def should_not_be_called(page_ids):
        raise AssertionError("clear_ghost_site_selection called with no page_ids selected")

    monkeypatch.setattr(dashboard_app, "clear_ghost_site_selection", should_not_be_called)
    dashboard_app.app.config["TESTING"] = True
    client = dashboard_app.app.test_client()

    resp = client.post("/leads/clear-selection", data={}, follow_redirects=False)
    assert resp.status_code == 302


def test_clear_ghost_site_selection_patches_notion_with_null_select(monkeypatch):
    captured_calls = []

    class FakeResponse:
        status_code = 200

    def fake_patch(url, headers=None, json=None):
        captured_calls.append((url, json))
        return FakeResponse()

    monkeypatch.setattr(dashboard_app.requests, "patch", fake_patch)
    results = dashboard_app.clear_ghost_site_selection(["page-abc"])

    assert results == {"page-abc": True}
    url, payload = captured_calls[0]
    assert url.endswith("/pages/page-abc")
    assert payload["properties"]["Ghost Site Status"]["select"] is None


def test_select_leads_redirects_to_selected_leads_page(monkeypatch):
    monkeypatch.setattr(dashboard_app, "set_ghost_site_status_selected", lambda page_ids: None)
    dashboard_app.app.config["TESTING"] = True
    client = dashboard_app.app.test_client()

    resp = client.post("/leads/select", data={"page_id": ["page-1"]}, follow_redirects=False)
    assert resp.headers["Location"] == "/leads/selected"


# --- Manual "Run Full Pipeline" button: orchestrator trigger (2026-08-18) ---

def test_trigger_orchestrator_does_not_acquire_lock_itself(monkeypatch, tmp_path):
    """Regression: trigger_script() used to acquire hp_lock for every
    script including "orchestrator" — but orchestrator.py acquires that
    same lock internally, so holding it here for the whole subprocess made
    the child's own acquire_lock() always see it held and skip, silently
    no-op'ing every manual orchestrator run."""
    lock_file = str(tmp_path / "orchestrator.lock")
    monkeypatch.setattr(dashboard_app.hp_lock, "LOCK_FILE", lock_file)
    monkeypatch.setattr(dashboard_app.subprocess, "run", lambda *a, **k: None)

    ok, error = dashboard_app.trigger_script("orchestrator")

    assert ok is True
    assert error is None
    assert not os.path.exists(lock_file)  # never created/held by the dashboard for this one


def test_trigger_orchestrator_route_passes_live_flag_in_production(monkeypatch):
    captured = {}
    monkeypatch.setattr(dashboard_app, "trigger_script", lambda name, extra_args=None: (captured.update(name=name, args=extra_args), (True, None))[1])
    monkeypatch.setattr(dashboard_app.hp_env, "IS_PRODUCTION", True)
    dashboard_app.app.config["TESTING"] = True
    client = dashboard_app.app.test_client()

    client.post("/trigger/orchestrator", follow_redirects=False)
    assert captured["args"] == ["--live"]


def test_trigger_orchestrator_route_no_live_flag_outside_production(monkeypatch):
    captured = {}
    monkeypatch.setattr(dashboard_app, "trigger_script", lambda name, extra_args=None: (captured.update(name=name, args=extra_args), (True, None))[1])
    monkeypatch.setattr(dashboard_app.hp_env, "IS_PRODUCTION", False)
    dashboard_app.app.config["TESTING"] = True
    client = dashboard_app.app.test_client()

    client.post("/trigger/orchestrator", follow_redirects=False)
    assert captured["args"] == []


def test_select_leads_updates_only_checked_page_ids(monkeypatch):
    captured = {}
    monkeypatch.setattr(dashboard_app, "set_ghost_site_status_selected", lambda page_ids: captured.update(page_ids=page_ids))
    dashboard_app.app.config["TESTING"] = True
    client = dashboard_app.app.test_client()

    resp = client.post("/leads/select", data={"page_id": ["page-1", "page-2"]}, follow_redirects=False)

    assert resp.status_code == 302
    assert captured["page_ids"] == ["page-1", "page-2"]


def test_select_leads_with_no_checkboxes_does_not_call_notion(monkeypatch):
    def should_not_be_called(page_ids):
        raise AssertionError("set_ghost_site_status_selected called with no page_ids selected")

    monkeypatch.setattr(dashboard_app, "set_ghost_site_status_selected", should_not_be_called)
    dashboard_app.app.config["TESTING"] = True
    client = dashboard_app.app.test_client()

    resp = client.post("/leads/select", data={}, follow_redirects=False)
    assert resp.status_code == 302


def test_set_ghost_site_status_selected_patches_notion_correctly(monkeypatch):
    captured_calls = []

    class FakeResponse:
        status_code = 200

    def fake_patch(url, headers=None, json=None):
        captured_calls.append((url, json))
        return FakeResponse()

    monkeypatch.setattr(dashboard_app.requests, "patch", fake_patch)
    results = dashboard_app.set_ghost_site_status_selected(["page-abc"])

    assert results == {"page-abc": True}
    url, payload = captured_calls[0]
    assert url.endswith("/pages/page-abc")
    assert payload["properties"]["Ghost Site Status"]["select"]["name"] == "Selected"


# --- Converted tracking: /leads/convert, mark_converted() ---

def test_convert_lead_route_calls_mark_converted(monkeypatch):
    captured = {}
    monkeypatch.setattr(dashboard_app, "mark_converted", lambda page_id: captured.setdefault("page_id", page_id) or True)
    dashboard_app.app.config["TESTING"] = True
    client = dashboard_app.app.test_client()

    resp = client.post("/leads/convert/page-abc", follow_redirects=False)

    assert resp.status_code == 302
    # Redirects to Selected Leads, not the main dashboard (2026-08-17) — that's
    # where "Live"/"Replied" leads (the only ones with a Mark Converted button)
    # are shown now that the candidate pool and the working list are split.
    assert resp.headers["Location"] == "/leads/selected"
    assert captured["page_id"] == "page-abc"
    assert captured["page_id"] == "page-abc"


def test_mark_converted_patches_status_and_date(monkeypatch):
    captured_calls = []

    class FakeResponse:
        status_code = 200

    def fake_patch(url, headers=None, json=None):
        captured_calls.append((url, json))
        return FakeResponse()

    monkeypatch.setattr(dashboard_app.requests, "patch", fake_patch)
    result = dashboard_app.mark_converted("page-abc")

    assert result is True
    url, payload = captured_calls[0]
    assert url.endswith("/pages/page-abc")
    assert payload["properties"]["Ghost Site Status"]["select"]["name"] == "Converted"
    assert payload["properties"]["Converted At"]["date"]["start"] == date.today().isoformat()


def test_mark_converted_returns_false_on_notion_failure(monkeypatch):
    class FakeResponse:
        status_code = 400

    monkeypatch.setattr(dashboard_app.requests, "patch", lambda url, headers=None, json=None: FakeResponse())
    assert dashboard_app.mark_converted("page-abc") is False


# --- get_leads(): Notion response parsing ---

def test_get_leads_parses_notion_response(monkeypatch):
    class FakeResponse:
        status_code = 200

        def json(self):
            return {
                "results": [{
                    "id": "page-xyz",
                    "properties": {
                        "Business Name": {"title": [{"text": {"content": "Test Salon"}}]},
                        "Box": {"select": {"name": "Box 1"}},
                        "Contact Status": {"select": {"name": "Follow 1"}},
                        "Ghost Site Status": {"select": None},
                    },
                }],
                "has_more": False,
                "next_cursor": None,
            }

    monkeypatch.setattr(dashboard_app.requests, "post", lambda url, headers=None, json=None: FakeResponse())
    leads = _real_get_leads()

    assert leads == [{
        "page_id": "page-xyz", "name": "Test Salon", "box": "Box 1",
        "contact_status": "Follow 1", "ghost_site_status": None,
    }]


# --- Sprint 13a: /settings routes ---

def test_settings_page_shows_current_value_and_default(client):
    resp = client.get("/settings")
    assert resp.status_code == 200
    html = resp.data.decode()
    assert "PageSpeed: Performance" in html
    assert "0.9 (matches)" in html  # current == Google default, matches


def test_settings_page_shows_deviation_when_value_differs(client, isolated_config):
    hp_settings.update_setting("pagespeed_performance_threshold", "0.75")
    resp = client.get("/settings")
    html = resp.data.decode()
    assert "0.9 (deviates)" in html


def test_settings_page_shows_no_default_for_lead_discovery_quota(client):
    resp = client.get("/settings")
    html = resp.data.decode()
    assert "no external default" in html


def test_settings_page_shows_change_history(client, isolated_config):
    hp_settings.update_setting("lead_discovery_min_qualifying_leads", "30")
    resp = client.get("/settings")
    html = resp.data.decode()
    assert "lead_discovery_min_qualifying_leads" in html
    assert "30" in html


def test_update_settings_valid_edit_redirects_and_writes(client, isolated_config):
    resp = client.post("/settings/update", data={"key": "lead_discovery_min_qualifying_leads", "value": "45"}, follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["Location"] == "/settings"
    assert hp_settings.read_current_config()["lead_discovery_min_qualifying_leads"] == 45


def test_update_settings_invalid_edit_redirects_with_error_and_does_not_write(client, isolated_config):
    before = isolated_config.read_text()
    resp = client.post("/settings/update", data={"key": "lead_discovery_min_qualifying_leads", "value": "99999"}, follow_redirects=False)
    assert resp.status_code == 302
    assert "error=" in resp.headers["Location"]
    assert isolated_config.read_text() == before


def test_update_settings_invalid_edit_shows_clear_error_message(client, isolated_config):
    resp = client.post(
        "/settings/update",
        data={"key": "core_web_vitals_lcp_threshold_ms", "value": "abc"},
        follow_redirects=True,
    )
    assert b"must be a number" in resp.data


# --- Sprint 22: /content ---

@pytest.fixture
def isolated_content(tmp_path, monkeypatch):
    email_dir = tmp_path / "email_templates"
    email_dir.mkdir()
    ghost_dir = tmp_path / "ghost_templates"
    ghost_dir.mkdir()

    registry = {}
    for key, filename in [("followup_1", "followup_1.md"), ("resurrection", "resurrection.md")]:
        path = email_dir / filename
        path.write_text(f"Original {key} text.\n")
        registry[key] = {"label": key, "category": "Test category", "path": str(path), "placeholders": []}

    monkeypatch.setattr(dashboard_app.hp_content, "CONTENT_REGISTRY", registry)
    return registry


def test_content_page_shows_current_text(client, isolated_content):
    resp = client.get("/content")
    assert resp.status_code == 200
    assert b"Original followup_1 text." in resp.data


def test_update_content_saves_new_text(client, isolated_content):
    resp = client.post("/content/update", data={"key": "followup_1", "text": "Edited text."}, follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["Location"] == "/content"
    assert dashboard_app.hp_content.read_content("followup_1") == "Edited text."


def test_update_content_empty_save_redirects_with_error(client, isolated_content):
    resp = client.post("/content/update", data={"key": "followup_1", "text": "   "}, follow_redirects=False)
    assert resp.status_code == 302
    assert "error=" in resp.headers["Location"]
    assert dashboard_app.hp_content.read_content("followup_1") == "Original followup_1 text.\n"


# --- Sprint 22: /history/<script_name> ---

def test_history_page_shows_recent_runs_for_one_script(client):
    _insert_run("cold_email", "success", "2026-08-14T08:00:00", "2026-08-14T08:00:05", notes="sent 3")
    _insert_run("queue_check", "success", "2026-08-14T09:00:00", "2026-08-14T09:00:01", notes="unrelated")
    resp = client.get("/history/cold_email")
    assert resp.status_code == 200
    assert b"sent 3" in resp.data
    assert b"unrelated" not in resp.data


def test_history_page_unknown_script_404s(client):
    resp = client.get("/history/not_a_real_script")
    assert resp.status_code == 404


# --- Sprint 22: /templates upload + confirm ---

@pytest.fixture
def isolated_templates(tmp_path, monkeypatch):
    monkeypatch.setattr(dashboard_app.gtr, "STATE_FILE", str(tmp_path / "ghost_template_requests.json"))
    monkeypatch.setattr(dashboard_app.gtr, "TEMPLATES_DIR", str(tmp_path / "templates"))
    return tmp_path


def _upload_html():
    return b'<html><body><h1 data-slot="business_name"></h1><p data-slot="phone"></p></body></html>'


def test_templates_page_loads_with_no_state(client, isolated_templates):
    resp = client.get("/templates")
    assert resp.status_code == 200
    assert b"No niches in the template handoff pipeline yet." in resp.data


def test_upload_template_processes_and_sets_pending_confirmation(client, isolated_templates):
    from io import BytesIO
    resp = client.post(
        "/templates/upload",
        data={"niche": "Barber Shops", "html_file": (BytesIO(_upload_html()), "design.html")},
        content_type="multipart/form-data",
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert resp.headers["Location"] == "/templates"

    state = dashboard_app.gtr.load_state()
    assert state["Barber Shops"]["status"] == "pending_confirmation"
    assert set(state["Barber Shops"]["detected_placeholders"]) == {"business_name", "phone"}

    saved_path = state["Barber Shops"]["template_path"]
    assert os.path.exists(saved_path)
    with open(saved_path) as f:
        assert 'data-slot="business_name"' in f.read()
    assert state["Barber Shops"]["css_path"] is None


def test_upload_template_missing_niche_redirects_with_error(client, isolated_templates):
    from io import BytesIO
    resp = client.post(
        "/templates/upload",
        data={"niche": "", "html_file": (BytesIO(_upload_html()), "design.html")},
        content_type="multipart/form-data",
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert "error=" in resp.headers["Location"]


def test_upload_template_with_css_file_saves_both(client, isolated_templates):
    from io import BytesIO
    resp = client.post(
        "/templates/upload",
        data={
            "niche": "Barber Shops",
            "html_file": (BytesIO(_upload_html()), "design.html"),
            "css_file": (BytesIO(b".x{color:red}"), "style.css"),
        },
        content_type="multipart/form-data",
        follow_redirects=False,
    )
    assert resp.status_code == 302

    state = dashboard_app.gtr.load_state()
    css_path = state["Barber Shops"]["css_path"]
    assert css_path is not None
    assert os.path.exists(css_path)
    with open(css_path) as f:
        assert f.read() == ".x{color:red}"


def test_upload_template_bad_bundler_export_redirects_with_error(client, isolated_templates):
    from io import BytesIO
    bad_html = b'<script type="__bundler/template">not valid json</script>'
    resp = client.post(
        "/templates/upload",
        data={"niche": "Barber Shops", "html_file": (BytesIO(bad_html), "design.html")},
        content_type="multipart/form-data",
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert "error=" in resp.headers["Location"]
    assert dashboard_app.gtr.load_state() == {}


def test_confirm_template_flips_status_to_approved(client, isolated_templates):
    dashboard_app.gtr.save_state({"Barber Shops": {"status": "pending_confirmation", "template_path": "x"}})
    resp = client.post("/templates/confirm/Barber Shops", follow_redirects=False)
    assert resp.status_code == 302
    assert dashboard_app.gtr.load_state()["Barber Shops"]["status"] == "approved"


def test_confirm_template_no_op_when_not_pending(client, isolated_templates):
    dashboard_app.gtr.save_state({"Barber Shops": {"status": "approved", "template_path": "x"}})
    client.post("/templates/confirm/Barber Shops", follow_redirects=False)
    assert dashboard_app.gtr.load_state()["Barber Shops"]["status"] == "approved"  # unchanged, not re-triggered


def test_upload_template_preserves_existing_thread_state(client, isolated_templates):
    """A niche that already had an in-flight email ask (existing
    thread_message_id from ghost_template_request.py's automated flow)
    keeps that thread info when Cyril uploads directly instead of
    waiting for the email reply."""
    dashboard_app.gtr.save_state({"Barber Shops": {
        "status": "pending_template", "subject": "Ghost Site template needed: Barber Shops",
        "thread_message_id": "<original@x>", "requested_at": "2026-08-01T00:00:00+00:00",
        "last_reminded_at": "2026-08-01T00:00:00+00:00",
    }})
    from io import BytesIO
    client.post(
        "/templates/upload",
        data={"niche": "Barber Shops", "html_file": (BytesIO(_upload_html()), "design.html")},
        content_type="multipart/form-data",
        follow_redirects=False,
    )
    state = dashboard_app.gtr.load_state()
    assert state["Barber Shops"]["thread_message_id"] == "<original@x>"
    assert state["Barber Shops"]["status"] == "pending_confirmation"


# --- "What's About to Happen" panel + current-status banner + rejected-
# leads cache (2026-08-17) ---

def test_index_shows_idle_status_when_nothing_running(client):
    resp = client.get("/")
    assert b"Idle" in resp.data


def test_index_shows_running_status_from_state_db(client):
    _insert_run("lead_discovery_and_evaluation_Contactsearch", "running", datetime.now(timezone.utc).isoformat())
    resp = client.get("/")
    assert b"Searching" in resp.data


def test_index_upcoming_sends_shows_ready_new_lead(monkeypatch, client):
    lead = {
        "page_id": "p1", "name": "Alpha", "email": "a@fakebiz-test.dev",
        "contact_status": "Not Contacted", "reply_status": None, "last_contact_date": None,
        "emails_sent": 0, "email_draft": "", "email_subject": "", "thread_message_id": "",
        "timezone": "PT", "ghost_site_status": None,
    }
    monkeypatch.setattr(dashboard_app.cold_email, "get_box1_leads", lambda: [lead])
    monkeypatch.setattr(dashboard_app.cold_email, "count_todays_new_sends", lambda: 0)

    class _FrozenAt8amPT:
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 6, 15, 8, 0, tzinfo=tz)

    monkeypatch.setattr(dashboard_app.cold_email, "datetime", _FrozenAt8amPT)

    resp = client.get("/")
    assert b"First cold email" in resp.data


def test_index_upcoming_sends_no_leads_shows_empty_state(client):
    resp = client.get("/")
    assert b"No leads currently queued" in resp.data


def test_index_shows_rejected_cache_count(monkeypatch, tmp_path, client):
    import json as _json
    cache_path = tmp_path / "cache.json"
    cache_path.write_text(_json.dumps({"p1": "not_box1", "p2": "no_email"}))
    monkeypatch.setattr(dashboard_app.hp_status, "CACHE_FILE", str(cache_path))
    resp = client.get("/")
    assert b"2</strong> businesses cached" in resp.data


def test_clear_checked_places_cache_route_empties_file(monkeypatch, tmp_path, client):
    import json as _json
    cache_path = tmp_path / "cache.json"
    cache_path.write_text(_json.dumps({"p1": "not_box1"}))
    monkeypatch.setattr(dashboard_app.hp_status, "CACHE_FILE", str(cache_path))

    resp = client.post("/cache/clear-checked-places", follow_redirects=False)

    assert resp.status_code == 302
    assert _json.loads(cache_path.read_text()) == {}
