#!/usr/bin/env python3
"""
Tests for scripts/opportunity_scoring.py — Enhanced Website-Based Lead
Qualification epic, User Stories 6, 8, 9 (combining signals, opportunity
score, qualification reason). CMS removed entirely from this module
2026-08-31 (Cyril's call) — booking presence alone now drives
qualification, and scoring/reason generation only ever consider booking,
call CTA, and PageSpeed mobile signals.

Covers:
  - Score prioritizes missing customer-conversion features (booking, call
    CTA) over the purely technical mobile signal — the spec's explicit
    "must not make a decision based solely on PageSpeed" and "must not
    score high just because a site is technically old" rules.
  - "Unknown" signals never score as confidently as a real "Not Detected"
    negative, but aren't silently treated as "fine" either.
  - Every individual factor is independently visible, not hidden behind
    a single aggregate score.
  - The qualification reason never claims something as verified when the
    underlying signal was Unknown.
"""

import os
import sys

SCRIPTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
sys.path.insert(0, SCRIPTS_DIR)

import opportunity_scoring as os_  # noqa: E402


def _crawl_result(booking="Booking Detected", call="Call CTA Detected"):
    return {
        "crawl_status": "Success",
        "booking_status": booking, "booking_provider": None,
        "call_cta_status": call, "call_cta_has_tel_link": call == "Call CTA Detected",
    }


# --- qualifies_by_website_signals(): the actual qualify/disqualify gate,
# distinct from the opportunity score (a ranking, not a pass/fail gate).
# Rule as of 2026-08-31 (Cyril's call, superseding the original CMS+booking
# combined rule): booking presence alone decides it — CMS is no longer
# part of the crawler's output at all, since CMS presence stopped being a
# useful signal once most small-business sites turned out to be built on
# Wix/Squarespace regardless of how sophisticated the business's actual
# web presence is. ---

def test_qualifies_when_no_booking():
    result = os_.qualifies_by_website_signals(_crawl_result(booking="Booking Not Detected"))
    assert result == "Qualify"


def test_disqualifies_when_booking_present():
    result = os_.qualifies_by_website_signals(_crawl_result(booking="Booking Detected"))
    assert result == "Disqualify"


def test_disqualify_ignores_call_cta_status_entirely():
    """Call CTA is informational only — booking is the sole factor in
    this decision."""
    result = os_.qualifies_by_website_signals(
        _crawl_result(booking="Booking Detected", call="Call CTA Not Detected")
    )
    assert result == "Disqualify"


def test_unknown_booking_makes_decision_unknown():
    """Regression guard: Cyril explicitly chose NOT to guess when the
    booking signal is Unknown (2026-08-30, still in force) — must not
    silently fall through to Qualify or Disqualify."""
    result = os_.qualifies_by_website_signals(_crawl_result(booking="Booking Unknown"))
    assert result == "Unknown"


# --- determine_primary_website_gap(): email pitch priority ---

def test_primary_gap_is_booking_when_missing():
    result = os_.determine_primary_website_gap(_crawl_result(booking="Booking Not Detected"))
    assert result == "no_booking"


def test_primary_gap_falls_back_to_call_cta_when_only_that_missing():
    result = os_.determine_primary_website_gap(_crawl_result(booking="Booking Detected", call="Call CTA Not Detected"))
    assert result == "no_call_cta"


def test_primary_gap_none_when_nothing_missing():
    result = os_.determine_primary_website_gap(_crawl_result())
    assert result is None


# --- calculate_opportunity_score(): weighting, categories, factor visibility ---

def test_score_zero_when_everything_detected_and_mobile_fine():
    result = os_.calculate_opportunity_score(_crawl_result(), pagespeed_mobile_is_poor=False)
    assert result["score"] == 0
    assert result["category"] == "Low"


def test_score_high_when_booking_and_call_cta_both_missing():
    """Missing customer-conversion features (booking + call CTA) alone
    total 70 points — must clear High threshold on their own, without
    needing the mobile signal at all. This is the spec's core priority:
    conversion features over purely technical signals."""
    result = os_.calculate_opportunity_score(
        _crawl_result(booking="Booking Not Detected", call="Call CTA Not Detected"),
        pagespeed_mobile_is_poor=False,
    )
    assert result["category"] == "High"
    assert result["factors"]["booking_missing"] == 40
    assert result["factors"]["call_cta_missing"] == 30
    assert result["factors"]["poor_mobile"] == 0


def test_score_low_despite_poor_mobile_when_conversion_features_present():
    """A technically old/slow site that already has real booking and
    call CTA functionality must not score High just from the mobile
    signal alone — explicit Must-Not in the spec."""
    result = os_.calculate_opportunity_score(_crawl_result(), pagespeed_mobile_is_poor=True)
    assert result["category"] != "High"
    assert result["factors"]["poor_mobile"] == 20


def test_score_unknown_signals_score_less_than_confirmed_missing():
    """An Unknown status must never score as confidently as a real
    "Not Detected" — absence of evidence is not evidence of absence."""
    unknown_result = os_.calculate_opportunity_score(
        _crawl_result(booking="Booking Unknown"), pagespeed_mobile_is_poor=False,
    )
    missing_result = os_.calculate_opportunity_score(
        _crawl_result(booking="Booking Not Detected"), pagespeed_mobile_is_poor=False,
    )
    assert unknown_result["factors"]["booking_missing"] < missing_result["factors"]["booking_missing"]
    assert unknown_result["factors"]["booking_missing"] > 0  # not treated as "fine" either


def test_score_every_factor_independently_visible():
    """Must not hide individual signals behind only a single aggregate
    score — explicit AC in the spec."""
    result = os_.calculate_opportunity_score(
        _crawl_result(booking="Booking Not Detected"),
        pagespeed_mobile_is_poor=True,
    )
    assert set(result["factors"].keys()) == {"booking_missing", "call_cta_missing", "poor_mobile"}
    assert result["factors"]["booking_missing"] == 40
    assert result["factors"]["call_cta_missing"] == 0
    assert result["factors"]["poor_mobile"] == 20


# --- generate_qualification_reason(): human-readable summary, honesty about Unknown ---

def test_qualification_reason_matches_spec_example_shape():
    """Reproduces the spec's own worked example: mobile responsive, but
    missing booking and call CTA -> High opportunity."""
    crawl_result = _crawl_result(booking="Booking Not Detected", call="Call CTA Not Detected")
    opportunity = os_.calculate_opportunity_score(crawl_result, pagespeed_mobile_is_poor=False)
    reason = os_.generate_qualification_reason("Test Salon", crawl_result, pagespeed_mobile_is_poor=False, opportunity=opportunity)

    assert "HIGH OPPORTUNITY" in reason
    assert "✓ Mobile responsive" in reason
    assert "✗ Online appointment booking" in reason
    assert "✗ Click-to-call CTA" in reason
    assert "Primary opportunity:" in reason


def test_qualification_reason_never_claims_unknown_as_verified():
    """Must not present an Unknown signal as a verified fact in either
    direction — explicit Must-Not in the spec."""
    crawl_result = _crawl_result(booking="Booking Unknown")
    opportunity = os_.calculate_opportunity_score(crawl_result, pagespeed_mobile_is_poor=False)
    reason = os_.generate_qualification_reason("Test Salon", crawl_result, pagespeed_mobile_is_poor=False, opportunity=opportunity)

    assert "✓ Online appointment booking" not in reason
    assert "✗ Online appointment booking" not in reason
    assert "couldn't confirm" in reason.lower()


def test_qualification_reason_primary_opportunity_is_the_top_factor():
    crawl_result = _crawl_result(booking="Booking Not Detected")  # only booking missing
    opportunity = os_.calculate_opportunity_score(crawl_result, pagespeed_mobile_is_poor=False)
    reason = os_.generate_qualification_reason("Test Salon", crawl_result, pagespeed_mobile_is_poor=False, opportunity=opportunity)

    assert "online booking" in reason.lower().split("primary opportunity:")[1]


def test_qualification_reason_low_opportunity_says_no_major_gaps():
    crawl_result = _crawl_result()  # everything detected
    opportunity = os_.calculate_opportunity_score(crawl_result, pagespeed_mobile_is_poor=False)
    reason = os_.generate_qualification_reason("Test Salon", crawl_result, pagespeed_mobile_is_poor=False, opportunity=opportunity)

    assert "No major gaps detected" in reason
