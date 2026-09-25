#!/usr/bin/env python3
"""
Tests for scripts/orchestrator.py — Sprint 8 (Orchestrator).

Covers Sprint 8's AC:
  - A lock file <30 min old blocks a concurrent run; >30 min old is
    treated as stale and the run proceeds.
  - Decision-chain order: Inbox Monitoring -> quota check -> follow-ups
    before fresh leads -> Lead Discovery top-up -> Queue Check.

Two real bugs found during Sprint 8 review, fixed here (per Cyril's
clarified quota design: up to 10 NEW leads/day, follow-ups uncapped and
always sent first):
  - The daily quota previously only gated whether cold_email.py got
    invoked at all (all-or-nothing) — each invocation then used its own
    independent DAILY_SEND_LIMIT with no memory of today's running total,
    so across the ~4 hourly runs that catch different timezones' local
    8am, actual volume could reach ~4x the intended cap. Fixed: the
    orchestrator computes the day's *remaining new-lead* quota and passes
    it into cold_email.py via --max-new-sends.
  - Quota (and a missing-niche Queue Check) previously caused an early
    `return` that skipped cold_email.py entirely — meaning due follow-ups
    wouldn't send at all in those cases. Fixed: cold_email.py always runs;
    only the Lead Discovery top-up step is quota-gated.
"""

import os
import sys

import pytest

SCRIPTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
sys.path.insert(0, SCRIPTS_DIR)

import orchestrator as orch  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_lock_file(tmp_path, monkeypatch):
    monkeypatch.setattr(orch.hp_lock, "LOCK_FILE", str(tmp_path / "orchestrator.lock"))


# Low-level lock file mechanics (fresh blocks, stale proceeds) now live in
# test_hp_lock.py, testing hp_lock.py directly — orch.acquire_lock() /
# orch.release_lock() are thin pass-throughs, exercised below via the
# "wrapper" tests that monkeypatch them at the orchestrator level.


# --- count_todays_new_sends(): filters on Contact Status "Follow 1" specifically ---

def test_count_todays_new_sends_filters_by_follow_1_and_today(monkeypatch):
    captured = {}

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"results": [{"id": "a"}, {"id": "b"}]}

    def fake_post(url, headers=None, json=None):
        captured["payload"] = json
        return FakeResponse()

    import requests as real_requests
    monkeypatch.setattr(real_requests, "post", fake_post)

    count = orch.count_todays_new_sends()
    assert count == 2
    conditions = captured["payload"]["filter"]["and"]
    assert {"property": "Contact Status", "select": {"equals": "Follow 1"}} in conditions
    assert any(c.get("property") == "Last Contact Date" for c in conditions)


# --- count_fresh_leads_awaiting_first_touch(): filters by "Not Contacted" ---

def test_count_fresh_leads_filters_by_not_contacted_specifically(monkeypatch):
    """Regression, found live 2026-08-27: the old count (Box 1, Contact
    Status != Lead Lost) counted every lead still working through its
    follow-up cadence (Follow 1-4) just as much as a genuinely new one —
    a lead only ever leaves that count at Lead Lost. With a real CRM
    where every lead had already advanced past first-touch, that count
    stayed above DAILY_SEND_LIMIT indefinitely, so Lead Discovery's
    top-up never triggered even though zero leads were actually new."""
    captured = {}

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"results": [{"id": "a"}]}

    def fake_post(url, headers=None, json=None):
        captured["payload"] = json
        return FakeResponse()

    import requests as real_requests
    monkeypatch.setattr(real_requests, "post", fake_post)

    count = orch.count_fresh_leads_awaiting_first_touch()
    assert count == 1
    conditions = captured["payload"]["filter"]["and"]
    assert {"property": "Box", "select": {"equals": "Box 1"}} in conditions
    assert {"property": "Contact Status", "select": {"equals": "Not Contacted"}} in conditions


# --- main(): decision-chain order, per AC's "mocked states confirm priority order" ---

def _mock_common(monkeypatch, run_calls, new_sent_today=0, available=3,
                  niche="Hair Salons", location="Wichita, KS", exhausted=False):
    monkeypatch.setattr(orch, "acquire_lock", lambda: True)
    monkeypatch.setattr(orch, "release_lock", lambda: None)
    monkeypatch.setattr(orch, "count_todays_new_sends", lambda: new_sent_today)
    monkeypatch.setattr(orch, "count_fresh_leads_awaiting_first_touch", lambda: available)
    monkeypatch.setattr(orch, "load_state", lambda: {
        "active_niche": niche, "active_location": location, "exhausted": exhausted,
    })

    def fake_run_script(script_name, extra_args=None):
        run_calls.append((script_name, extra_args or []))
        return True

    monkeypatch.setattr(orch, "run_script", fake_run_script)


def test_decision_chain_order_replies_quota_followups_and_low_crm_all_true(monkeypatch):
    """AC: inbox -> quota check -> (follow-ups before fresh leads, handled
    inside cold_email.py's own priority) -> Lead Discovery top-up -> cold_email."""
    run_calls = []
    _mock_common(monkeypatch, run_calls, new_sent_today=2, available=1)  # quota remains, CRM low

    orch.main()

    scripts_called = [name for name, _args in run_calls]
    assert scripts_called == [
        "inbox_monitoring.py",
        "bounce_check.py",
        "lead_discovery_and_evaluation_Contactsearch.py",
        "queue_check.py",  # stock still below the daily limit after the top-up -> ask early
        "cold_email.py",
        "check_opens_reminder.py",
        "ghost_template_request.py",
        "ghost_site_builder.py",
        "ghost_site_sequence.py",
        "ghost_site_expiry.py",
    ]
    # remaining quota = 10 - 2 = 8, passed through to cold_email.py
    cold_email_call = next(c for c in run_calls if c[0] == "cold_email.py")
    assert "--max-new-sends" in cold_email_call[1]
    assert cold_email_call[1][cold_email_call[1].index("--max-new-sends") + 1] == "8"


def test_quota_exhausted_skips_discovery_but_still_runs_cold_email(monkeypatch):
    """Regression: quota used to cause an early return that skipped
    cold_email.py entirely — due follow-ups must still get a chance to send."""
    run_calls = []
    _mock_common(monkeypatch, run_calls, new_sent_today=10, available=1)  # quota exhausted, CRM low

    orch.main()

    scripts_called = [name for name, _args in run_calls]
    assert "lead_discovery_and_evaluation_Contactsearch.py" not in scripts_called
    # 2026-09-19: stock below the daily limit now asks for the next city even
    # when today's quota is used up, so tomorrow's 10 new leads aren't at risk.
    assert scripts_called == ["inbox_monitoring.py", "bounce_check.py", "queue_check.py", "cold_email.py", "check_opens_reminder.py", "ghost_template_request.py", "ghost_site_builder.py", "ghost_site_sequence.py", "ghost_site_expiry.py"]
    cold_email_call = next(c for c in run_calls if c[0] == "cold_email.py")
    assert cold_email_call[1][cold_email_call[1].index("--max-new-sends") + 1] == "0"


def test_no_active_niche_runs_queue_check_but_still_runs_cold_email(monkeypatch):
    """Regression: missing-niche Queue Check used to also return early,
    skipping cold_email.py — must not skip it, follow-ups still matter."""
    run_calls = []
    _mock_common(monkeypatch, run_calls, new_sent_today=0, available=1, niche=None, location=None)

    orch.main()

    scripts_called = [name for name, _args in run_calls]
    assert scripts_called == ["inbox_monitoring.py", "bounce_check.py", "queue_check.py", "cold_email.py", "check_opens_reminder.py", "ghost_template_request.py", "ghost_site_builder.py", "ghost_site_sequence.py", "ghost_site_expiry.py"]


def test_low_supply_asks_with_low_supply_flag_after_discovery(monkeypatch):
    run_calls = []
    _mock_common(monkeypatch, run_calls, new_sent_today=0, available=3)

    orch.main()

    qc = [c for c in run_calls if c[0] == "queue_check.py"]
    assert len(qc) == 1 and qc[0][1] == ["--low-supply"]


def test_exhausted_city_asks_once_not_twice(monkeypatch):
    run_calls = []
    _mock_common(monkeypatch, run_calls, new_sent_today=0, available=3, exhausted=True)

    orch.main()

    assert [c[0] for c in run_calls].count("queue_check.py") == 1


def test_sufficient_crm_stock_skips_discovery_still_runs_cold_email(monkeypatch):
    run_calls = []
    _mock_common(monkeypatch, run_calls, new_sent_today=0, available=50)  # plenty of leads

    orch.main()

    scripts_called = [name for name, _args in run_calls]
    assert scripts_called == ["inbox_monitoring.py", "bounce_check.py", "cold_email.py", "check_opens_reminder.py", "ghost_template_request.py", "ghost_site_builder.py", "ghost_site_sequence.py", "ghost_site_expiry.py"]


def test_locked_run_does_not_execute_any_script(monkeypatch):
    run_calls = []
    monkeypatch.setattr(orch, "acquire_lock", lambda: False)

    def run_script_should_not_be_called(*args, **kwargs):
        raise AssertionError("run_script() called despite a held lock")

    monkeypatch.setattr(orch, "run_script", run_script_should_not_be_called)

    orch.main()  # must not raise


def test_run_script_uses_sys_executable_not_bare_python3(monkeypatch):
    """Regression test for a real bug found during Sprint 13's live
    verification: launchd's minimal PATH resolved bare "python3" to a
    stray /usr/local/bin/python3 (missing pyyaml/flask) ahead of the real
    /opt/homebrew/bin/python3 this project actually installs into. Every
    child script silently broke the moment Sprint 12 added hp_config's
    yaml import. sys.executable pins it to whatever interpreter is
    actually running this process, immune to PATH-order surprises."""
    captured = {}

    class FakeResult:
        returncode = 0

    def fake_run(args, env=None):
        captured["args"] = args
        return FakeResult()

    monkeypatch.setattr(orch.subprocess, "run", fake_run)
    orch.run_script("queue_check.py")

    assert captured["args"][0] == orch.sys.executable
    assert captured["args"][1].endswith("queue_check.py")
