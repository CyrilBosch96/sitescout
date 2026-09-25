#!/usr/bin/env python3
"""
Tests for the Website Evaluation portion of
scripts/lead_discovery_and_evaluation_Contactsearch.py — Sprint 3.

Covers Sprint 3's AC:
  - 4-run median: run 1 discarded, median of runs 2-4 used.
  - A run that errors out is retried once, then excluded if it still fails
    (not silently corrupting the result with an undocumented median-of-2).
  - 3-of-7 signal threshold tested against Google's actual published
    "Good" cutoffs, with cases exactly at and just past each boundary.
  - "broken" requires independent HTTP confirmation — regression test for
    both directions (PageSpeed fails + HTTP fails -> broken; PageSpeed
    fails + HTTP succeeds -> NOT broken).
  - No preliminary screen on run 1 alone — every lead gets the full
    4-run process regardless of how run 1 looks.
  - classify_lead() returns the median data (not run 1's single-run data)
    as evidence for downstream content generation.

Box 2/Box 3 classification does not exist in this codebase at all
(confirmed during Sprint 3 review) and is out of scope here per Cyril's
call — tracked as a separate, deferred gap in PLAN.md, not tested.
"""

import os
import sys

import pytest

SCRIPTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
sys.path.insert(0, SCRIPTS_DIR)

import lead_discovery_and_evaluation_Contactsearch as ld  # noqa: E402


@pytest.fixture(autouse=True)
def no_real_sleeps(monkeypatch):
    """classify_lead() sleeps 5s between real PageSpeed calls — mock it out
    so this suite runs in milliseconds, not minutes."""
    monkeypatch.setattr(ld.time, "sleep", lambda seconds: None)


def make_pdata(performance=0.95, seo=0.95, accessibility=0.95, best_practices=0.95,
                lcp=2000, cls=0.05, tbt=100):
    """A fully-passing pdata dict by default — override individual values
    per test to push a specific signal above/below its threshold."""
    return {
        "scores": {
            "performance": performance,
            "seo": seo,
            "accessibility": accessibility,
            "best-practices": best_practices,
        },
        "audits": {
            "largest-contentful-paint": {"numericValue": lcp},
            "cumulative-layout-shift": {"numericValue": cls},
            "total-blocking-time": {"numericValue": tbt},
        },
        "audit_refs": {"performance": [], "seo": [], "accessibility": [], "best-practices": []},
    }


# --- check_signals(): AC "3-of-7 threshold against Google's published
# 'Good' cutoffs, cases just above/below each cutoff" ---

@pytest.mark.parametrize("field,at_threshold,past_threshold", [
    ("performance", 0.9, 0.89),
    ("seo", 0.9, 0.89),
    ("accessibility", 0.9, 0.89),
    ("best_practices", 0.9, 0.89),
])
def test_score_signal_exactly_at_threshold_does_not_fail(field, at_threshold, past_threshold):
    pdata = make_pdata(**{field: at_threshold})
    total_failed, _ = ld.check_signals(pdata)
    assert total_failed == 0


@pytest.mark.parametrize("field,at_threshold,past_threshold", [
    ("performance", 0.9, 0.89),
    ("seo", 0.9, 0.89),
    ("accessibility", 0.9, 0.89),
    ("best_practices", 0.9, 0.89),
])
def test_score_signal_just_past_threshold_fails(field, at_threshold, past_threshold):
    pdata = make_pdata(**{field: past_threshold})
    total_failed, _ = ld.check_signals(pdata)
    assert total_failed == 1


def test_lcp_exactly_at_2500ms_does_not_fail():
    total_failed, _ = ld.check_signals(make_pdata(lcp=2500))
    assert total_failed == 0


def test_lcp_just_past_2500ms_fails():
    total_failed, _ = ld.check_signals(make_pdata(lcp=2501))
    assert total_failed == 1


def test_cls_exactly_at_point_one_does_not_fail():
    total_failed, _ = ld.check_signals(make_pdata(cls=0.1))
    assert total_failed == 0


def test_cls_just_past_point_one_fails():
    total_failed, _ = ld.check_signals(make_pdata(cls=0.11))
    assert total_failed == 1


def test_tbt_exactly_at_200ms_does_not_fail():
    total_failed, _ = ld.check_signals(make_pdata(tbt=200))
    assert total_failed == 0


def test_tbt_just_past_200ms_fails():
    total_failed, _ = ld.check_signals(make_pdata(tbt=201))
    assert total_failed == 1


def test_all_seven_signals_can_fail_simultaneously():
    pdata = make_pdata(performance=0.5, seo=0.5, accessibility=0.5, best_practices=0.5,
                        lcp=5000, cls=0.5, tbt=1000)
    total_failed, failed = ld.check_signals(pdata)
    assert total_failed == 7


def test_two_failed_signals_below_the_three_signal_threshold():
    pdata = make_pdata(performance=0.5, seo=0.5)  # exactly 2 failing
    total_failed, _ = ld.check_signals(pdata)
    assert total_failed == 2
    assert total_failed < ld.MIN_SIGNALS_FAILED


def test_three_failed_signals_meets_the_threshold():
    pdata = make_pdata(performance=0.5, seo=0.5, accessibility=0.5)  # exactly 3
    total_failed, _ = ld.check_signals(pdata)
    assert total_failed == 3
    assert total_failed >= ld.MIN_SIGNALS_FAILED


# --- median_pdata_for_check(): AC "median calculation ... ties" ---

def test_median_of_three_distinct_values():
    pdata_list = [make_pdata(performance=0.5), make_pdata(performance=0.7), make_pdata(performance=0.9)]
    median = ld.median_pdata_for_check(pdata_list)
    assert median["scores"]["performance"] == 0.7


def test_median_handles_ties():
    pdata_list = [make_pdata(performance=0.7), make_pdata(performance=0.7), make_pdata(performance=0.9)]
    median = ld.median_pdata_for_check(pdata_list)
    assert median["scores"]["performance"] == 0.7


def test_median_of_lcp_across_three_runs():
    pdata_list = [make_pdata(lcp=1000), make_pdata(lcp=3000), make_pdata(lcp=2000)]
    median = ld.median_pdata_for_check(pdata_list)
    assert median["audits"]["largest-contentful-paint"]["numericValue"] == 2000


# --- classify_lead(): full AC coverage ---

def _fake_pagespeed_sequence(monkeypatch, results):
    """results: list of values (dict or None) returned in call order.
    Raises if called more times than provided, so tests catch unexpected
    extra/missing calls."""
    calls = {"count": 0}

    def fake_get_pagespeed_data(url):
        idx = calls["count"]
        calls["count"] += 1
        if idx >= len(results):
            raise AssertionError(f"get_pagespeed_data called more times than expected ({idx + 1})")
        return results[idx]

    monkeypatch.setattr(ld, "get_pagespeed_data", fake_get_pagespeed_data)
    return calls


def test_classify_lead_no_website_returns_false():
    assert ld.classify_lead({"websiteUri": None}) == (False, None, None)
    assert ld.classify_lead({}) == (False, None, None)


def test_classify_lead_broken_regression_pagespeed_fails_and_http_fails(monkeypatch):
    """Regression test for the AC's explicit case: PageSpeed and an
    independent HTTP fetch must BOTH fail before 'broken' is assigned."""
    _fake_pagespeed_sequence(monkeypatch, [None])
    monkeypatch.setattr(ld, "direct_site_reachable", lambda url: False)

    box1, pdata, reason = ld.classify_lead({"websiteUri": "https://down-site-fake.example.com"})

    assert box1 is True
    assert reason == "broken"


def test_classify_lead_not_broken_when_pagespeed_fails_but_http_succeeds(monkeypatch):
    """Regression test for the AC's explicit case: PageSpeed says failing
    but HTTP fetch succeeds — must NOT label 'broken'."""
    _fake_pagespeed_sequence(monkeypatch, [None])
    monkeypatch.setattr(ld, "direct_site_reachable", lambda url: True)

    box1, pdata, reason = ld.classify_lead({"websiteUri": "https://flaky-pagespeed-fake.example.com"})

    assert box1 is False
    assert reason is None


def test_classify_lead_no_preliminary_screen_always_runs_full_process(monkeypatch):
    """Run 1 looks perfectly fine (0 failed signals) — the old preliminary
    screen would have short-circuited here without ever checking runs 2-4.
    Confirms all 4 calls happen regardless."""
    good_run = make_pdata()  # all passing
    calls = _fake_pagespeed_sequence(monkeypatch, [good_run, good_run, good_run, good_run])

    box1, pdata, reason = ld.classify_lead({"websiteUri": "https://fine-site-fake.example.com"})

    assert calls["count"] == 4  # run 1 + all 3 additional runs, no short-circuit
    assert box1 is False  # genuinely a fine site


def test_classify_lead_qualifying_site_returns_median_not_run_one(monkeypatch):
    """classify_lead must return the median across runs 2-4 as evidence,
    not run 1's data — even though run 1 also gets fetched."""
    run1 = make_pdata(performance=0.99)  # distinctly different from the others
    run2 = make_pdata(performance=0.3, seo=0.3, accessibility=0.3)
    run3 = make_pdata(performance=0.3, seo=0.3, accessibility=0.3)
    run4 = make_pdata(performance=0.3, seo=0.3, accessibility=0.3)
    _fake_pagespeed_sequence(monkeypatch, [run1, run2, run3, run4])

    box1, pdata, reason = ld.classify_lead({"websiteUri": "https://bad-site-fake.example.com"})

    assert box1 is True
    assert pdata["scores"]["performance"] == 0.3  # median of runs 2-4, not run 1's 0.99


def test_classify_lead_retries_once_on_a_failed_additional_run(monkeypatch):
    """One additional run fails, then succeeds on retry — the median
    should still be computed from a full 3 kept runs, not fall back to 2."""
    run1 = make_pdata(performance=0.3, seo=0.3, accessibility=0.3)
    run2 = make_pdata(performance=0.3, seo=0.3, accessibility=0.3)
    run3_retry = make_pdata(performance=0.3, seo=0.3, accessibility=0.3)
    run4 = make_pdata(performance=0.3, seo=0.3, accessibility=0.3)
    # run1, run2 (ok), run3 fails then retry succeeds, run4 (ok) = 5 calls total
    calls = _fake_pagespeed_sequence(monkeypatch, [run1, run2, None, run3_retry, run4])

    box1, pdata, reason = ld.classify_lead({"websiteUri": "https://flaky-run-fake.example.com"})

    assert calls["count"] == 5  # includes the one retry
    assert box1 is True
    assert pdata["scores"]["performance"] == 0.3


def test_classify_lead_excludes_run_when_retry_also_fails(monkeypatch):
    """One additional run fails twice (original + retry) — excluded, and
    the median is computed from the remaining 2 kept runs rather than
    erroring out, per the AC's 'retry or exclude' requirement."""
    run1 = make_pdata(performance=0.3, seo=0.3, accessibility=0.3)
    run2 = make_pdata(performance=0.3, seo=0.3, accessibility=0.3)
    run4 = make_pdata(performance=0.3, seo=0.3, accessibility=0.3)
    # run1, run2 (ok), run3 fails, retry fails, run4 (ok) = 5 calls
    calls = _fake_pagespeed_sequence(monkeypatch, [run1, run2, None, None, run4])

    box1, pdata, reason = ld.classify_lead({"websiteUri": "https://one-run-lost-fake.example.com"})

    assert calls["count"] == 5
    assert box1 is True  # still classifiable from 2 kept runs, not silently dropped


def test_classify_lead_not_enough_data_when_too_many_runs_fail(monkeypatch):
    """Only 1 of the 3 additional runs ultimately succeeds (2 fail even
    after retries) — not enough data for a reliable median, so no
    classification is made rather than guessing from 1 data point."""
    run1 = make_pdata(performance=0.3, seo=0.3, accessibility=0.3)
    run4 = make_pdata(performance=0.3, seo=0.3, accessibility=0.3)
    # run1, run2 fails+retry fails, run3 fails+retry fails, run4 (ok) = 7 calls
    _fake_pagespeed_sequence(monkeypatch, [run1, None, None, None, None, run4])

    box1, pdata, reason = ld.classify_lead({"websiteUri": "https://mostly-failed-fake.example.com"})

    assert box1 is False
    assert reason is None


def test_classify_lead_fine_site_below_threshold_is_not_box1(monkeypatch):
    good = make_pdata()
    _fake_pagespeed_sequence(monkeypatch, [good, good, good, good])
    box1, pdata, reason = ld.classify_lead({"websiteUri": "https://all-fine-fake.example.com"})
    assert box1 is False


@pytest.mark.parametrize("failing,expected_reason", [
    (dict(performance=0.3, lcp=3000, tbt=500), "slow"),        # speed-dominant
    (dict(seo=0.3, accessibility=0.3, best_practices=0.3), "quality"),  # quality-dominant (seo tied but quality checked first... see below)
])
def test_classify_lead_reason_selection(monkeypatch, failing, expected_reason):
    pdata_kwargs = dict(failing)
    run = make_pdata(**pdata_kwargs)
    _fake_pagespeed_sequence(monkeypatch, [run, run, run, run])
    box1, pdata, reason = ld.classify_lead({"websiteUri": "https://reason-test-fake.example.com"})
    assert box1 is True
    assert reason == expected_reason
