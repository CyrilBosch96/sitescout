#!/usr/bin/env python3
"""
Opportunity Scoring + Qualification Reason — SiteScout
(2026-08-30)

Enhanced Website-Based Lead Qualification epic, User Stories 6, 8, 9.
Combines website_crawler.py's functional signals (booking, call CTA —
CMS removed 2026-08-31, see qualifies_by_website_signals()) with
PageSpeed's technical/mobile signal into one opportunity score and a
human-readable qualification reason — the actual product-facing output
this whole epic exists to produce.

Deliberately built and tested as a STANDALONE module first, not yet
wired in to replace lead_discovery_and_evaluation_Contactsearch.py's
classify_lead() as the live qualification gate. That's the actual
end-goal Cyril confirmed (2026-08-28: "New score replaces/redefines
qualification"), but it's the single highest-risk change in this whole
epic — it decides who gets emailed at all, on a system that's live and
sending real emails right now. This module gets proven thoroughly (tests
+ live verification against real sites) before that wiring happens as
its own deliberate, separately-confirmed step.

Scoring weights are a plain module-level dict (WEIGHTS) rather than
wired into config.yaml/hp_config.py yet — this whole epic is still
mid-build (BuiltWith enrichment is pending Cyril's API key), so adding
config-file plumbing now would be premature. Easy to move there once the
epic is actually live.
"""

# Each factor's max contribution to the 0-100 opportunity score. Weighted
# so missing customer-conversion features (booking, call CTA) dominate
# over the purely technical mobile signal — the spec is explicit that a
# technically-old site with strong booking/call functionality must not
# score as high opportunity just because it's old. CMS removed entirely
# (Cyril's call, 2026-08-31) — see qualifies_by_website_signals().
WEIGHTS = {
    "booking_missing": 40,
    "call_cta_missing": 30,
    "poor_mobile": 20,
}

# Uncertain signals ("Unknown" — a crawl that failed or found nothing to
# inspect) get a small, non-zero contribution rather than either the full
# "missing" weight or zero. Absence of evidence is not evidence of
# absence (the spec's own words), so "Unknown" must never score as high
# as a confirmed "Not Detected" — but it shouldn't score as if everything
# were fine either, since we genuinely don't know.
UNKNOWN_FRACTION = 0.25

HIGH_THRESHOLD = 60
MEDIUM_THRESHOLD = 30


def qualifies_by_website_signals(crawl_result):
    """The actual qualify/disqualify decision, distinct from the
    opportunity score above (which is a "how strong is the pitch"
    ranking, not a pass/fail gate). Returns "Qualify", "Disqualify", or
    "Unknown".

    Rule (Cyril's call, 2026-08-31, superseding the original 2026-08-30
    CMS+booking combined rule): booking presence alone decides it. No
    online booking system -> Qualify. A real booking system already
    live -> Disqualify, never written to the CRM at all. CMS status is
    deliberately NOT part of this decision anymore — CMS presence
    stopped being a useful signal once most small-business sites turned
    out to be built on Wix/Squarespace regardless of how sophisticated
    the business's actual web presence is, so requiring both CMS AND
    booking to disqualify was letting genuinely well-equipped
    (booking-enabled) businesses slip through as "qualifying" leads.
    Call CTA remains informational only, not part of this decision.

    If booking status is Unknown (crawl failed, or a genuinely ambiguous
    page), the decision is "Unknown" — Cyril's explicit call (2026-08-30,
    still in force) is to NOT guess in either direction here, unlike
    this project's usual "ambiguous defaults to the safer/more-inclusive
    option" pattern elsewhere (e.g. classify_reply()). "Unknown" is not
    a permanent reject — callers should skip processing this lead for
    now and let it be retried on a future run once the crawl can
    actually confirm one way or the other, same as this project's
    existing "leave it uncached, don't blacklist a transient failure"
    convention (lead_discovery_and_evaluation_Contactsearch.py's
    content-gen/Notion-write failure handling)."""
    booking_status = crawl_result.get("booking_status")

    if booking_status == "Booking Unknown":
        return "Unknown"

    if booking_status == "Booking Detected":
        return "Disqualify"
    return "Qualify"


def determine_primary_website_gap(crawl_result):
    """Which crawler-detected gap the cold email's pitch should lead
    with, when more than one is missing. Priority order (Cyril's call,
    2026-08-30, CMS removed 2026-08-31): booking first — the strongest,
    most concrete pitch ("you're losing customers who'd rather book
    online"); call CTA next — the weaker, more generic angle.

    Given qualifies_by_website_signals()'s own disqualify rule (booking
    present -> Disqualify), a lead that reaches this function is always
    missing booking, so this always returns "no_booking" today unless
    that disqualify rule ever changes — kept as a real priority check
    rather than hardcoded so the ordering still holds correctly if it
    does.

    Returns "no_booking", "no_call_cta", or None (nothing missing —
    shouldn't happen for a lead that qualified, but never fabricate a
    gap that isn't real)."""
    if crawl_result.get("booking_status") == "Booking Not Detected":
        return "no_booking"
    if crawl_result.get("call_cta_status") == "Call CTA Not Detected":
        return "no_call_cta"
    return None


def _factor_score(status_detected, status_not_detected, status_unknown, current_status, max_points):
    if current_status == status_not_detected:
        return max_points
    if current_status == status_unknown:
        return round(max_points * UNKNOWN_FRACTION)
    return 0  # status_detected, or anything else — no opportunity here


def calculate_opportunity_score(crawl_result, pagespeed_mobile_is_poor):
    """crawl_result is website_crawler.crawl_and_qualify()'s output dict.
    pagespeed_mobile_is_poor is a plain bool — the caller already has
    PageSpeed's own mobile/performance score and threshold, this function
    just needs the yes/no verdict, not PageSpeed's internal shape.

    Returns {"score": int 0-100, "category": "High"|"Medium"|"Low",
    "factors": {factor_name: points_contributed}} — factors are exposed
    individually (not just the total) so the qualification reason
    generator, and the dashboard later, can show which specific gaps
    drove the score, per the spec's "must show the reasons behind the
    score" and "must not hide signals behind only an aggregate score"
    requirements."""
    factors = {
        "booking_missing": _factor_score(
            "Booking Detected", "Booking Not Detected", "Booking Unknown",
            crawl_result.get("booking_status"), WEIGHTS["booking_missing"],
        ),
        "call_cta_missing": _factor_score(
            "Call CTA Detected", "Call CTA Not Detected", "Call CTA Unknown",
            crawl_result.get("call_cta_status"), WEIGHTS["call_cta_missing"],
        ),
        "poor_mobile": WEIGHTS["poor_mobile"] if pagespeed_mobile_is_poor else 0,
    }

    score = sum(factors.values())
    if score >= HIGH_THRESHOLD:
        category = "High"
    elif score >= MEDIUM_THRESHOLD:
        category = "Medium"
    else:
        category = "Low"

    return {"score": score, "category": category, "factors": factors}


def generate_qualification_reason(business_name, crawl_result, pagespeed_mobile_is_poor, opportunity):
    """Builds the human-readable summary from the spec's own example
    format. Never invents anything not actually backed by a real signal —
    an "Unknown" status is shown as unknown, not silently folded into
    either the detected or missing column, matching the spec's explicit
    "must not present assumptions as verified facts" rule."""
    detected_lines = []
    missing_lines = []
    unknown_lines = []

    def _bucket(label, status, detected_value):
        if status == detected_value:
            detected_lines.append(f"✓ {label}")
        elif status is not None and "Unknown" in status:
            unknown_lines.append(f"? {label} (couldn't confirm)")
        else:
            missing_lines.append(f"✗ {label}")

    if not pagespeed_mobile_is_poor:
        detected_lines.append("✓ Mobile responsive")
    else:
        missing_lines.append("✗ Mobile-friendly performance")

    _bucket("Online appointment booking", crawl_result.get("booking_status"), "Booking Detected")
    _bucket("Click-to-call CTA", crawl_result.get("call_cta_status"), "Call CTA Detected")

    # Primary opportunity: the single highest-scoring missing factor,
    # phrased as the actual sales angle — not a generic catch-all.
    factor_labels = {
        "booking_missing": "Add online booking to capture customers who'd rather book than call",
        "call_cta_missing": "Add a real click-to-call path — a plain phone number isn't a usable mobile CTA",
        "poor_mobile": "Fix mobile performance — slow load times cost real visitors",
    }
    top_factor = max(opportunity["factors"], key=opportunity["factors"].get)
    primary_opportunity = factor_labels[top_factor] if opportunity["factors"][top_factor] > 0 else "No major gaps detected — a lower-priority lead."

    lines = [f"{opportunity['category'].upper()} OPPORTUNITY", ""]
    if detected_lines:
        lines.append("Website detected:")
        lines.extend(detected_lines)
        lines.append("")
    if missing_lines:
        lines.append("Missing:")
        lines.extend(missing_lines)
        lines.append("")
    if unknown_lines:
        lines.append("Couldn't confirm:")
        lines.extend(unknown_lines)
        lines.append("")
    lines.append("Primary opportunity:")
    lines.append(primary_opportunity)

    return "\n".join(lines).strip()
