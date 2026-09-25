#!/usr/bin/env python3
"""
Tests for the Outreach Generation portion of
scripts/lead_discovery_and_evaluation_Contactsearch.py — Sprint 4.

Covers Sprint 4's AC:
  - Junk email denylist: known-bad patterns (sentry, wixpress, booksy.com,
    squareup.com, 20+ char hex local-part) rejected; a normal business
    email passes.
  - Duplicate lead-in phrase ("Right now, your website") stripped via the
    regex safety net regardless of what the model outputs.
  - No em-dashes: any em-dash in a Qwen completion is sanitized before it
    can reach Notion (generate_middle_continuation and generate_hero_line
    both auto-replace "—" with ","; Cyril's call — same real-world
    guarantee as a reject-and-retry gate, simpler, no wasted generations).
  - Competitor "beatable" logic: 10+ rank gap with review count within 50
    flags a competitor; just under either threshold does not.
  - Hero Line generation: short honest tagline, no em-dash, written to its
    own Notion field only when generated successfully.

Out of scope, per Cyril's call (2026-08-11):
  - Box 2/3 classification — stays deferred, not built this sprint either.
  - Yelp/Angie's List contact-finding fallback — not built; no API access
    configured. Contact-finding stays limited to the lead's own website.
"""

import os
import sys

import pytest

SCRIPTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
sys.path.insert(0, SCRIPTS_DIR)

import lead_discovery_and_evaluation_Contactsearch as ld  # noqa: E402


# --- validate_email(): AC "junk pattern rejected, next source tried" ---

@pytest.mark.parametrize("candidate", [
    "info@sentry.io",
    "noreply@wixpress.com",
    "owner@booksy.com",
    "shop@squareup.com",
    "a1b2c3d4e5f6a7b8c9d0e1f2@mailtrack-fake.test",  # 24-char hex local-part
])
def test_validate_email_rejects_known_junk_patterns(candidate):
    assert ld.validate_email(candidate) is False


def test_validate_email_accepts_normal_business_email():
    assert ld.validate_email("owner@localbarbershop-fake.test") is True


@pytest.mark.parametrize("candidate", [
    "info@yourwebsite.com",  # found live during Sprint 8's pipeline test
    "contact@yoursite.com",
    "hello@yourbusiness.com",
    "info@YourWebsite.com",  # case-insensitive
])
def test_validate_email_rejects_generic_placeholder_domains(candidate):
    assert ld.validate_email(candidate) is False


def test_validate_email_does_not_reject_a_domain_that_merely_contains_your():
    """The your{noun}.tld check is domain-anchored, not a loose substring
    match — shouldn't reject a real business whose name happens to start
    with "your" (e.g. "Your Neighborhood Barbers")."""
    assert ld.validate_email("owner@yourneighborhoodbarbers-fake.test") is True


def test_validate_email_rejects_mysite_domain():
    """Found live during Sprint 14's integration test: example@mysite.com."""
    assert ld.validate_email("example@mysite.com") is False


def test_validate_email_rejects_example_local_part_on_any_domain():
    assert ld.validate_email("example@some-real-looking-biz.test") is False
    assert ld.validate_email("EXAMPLE@some-real-looking-biz.test") is False


def test_validate_email_does_not_reject_local_part_merely_containing_example():
    """Exact match only, not a substring check — shouldn't false-flag a
    real address that happens to contain the word "example"."""
    assert ld.validate_email("forexample@realbiz-fake.test") is True


def test_validate_email_rejects_none_or_empty():
    assert ld.validate_email(None) is False
    assert ld.validate_email("") is False


def test_validate_email_hex_threshold_is_20_chars():
    # 19 hex chars, just under the 20-char threshold: not flagged as a tracking ID.
    assert ld.validate_email("a1b2c3d4e5f6a7b8c9d@fakebiz-test.dev") is True
    # Exactly 20 hex chars: flagged.
    assert ld.validate_email("a1b2c3d4e5f6a7b8c9d0@fakebiz-test.dev") is False


# --- generate_middle_continuation(): duplicate lead-in + em-dash sanitize ---

def _fake_llm_response(monkeypatch, text):
    # hp_llm.generate() already collapses network errors/non-200/malformed
    # responses down to None — these generation functions only ever see
    # text or None, so tests mock at that boundary rather than a specific
    # HTTP provider's request/response shape (2026-08-20: Ollama -> Gemini).
    monkeypatch.setattr(ld.hp_llm, "generate", lambda prompt, timeout=None: text)


def test_generate_middle_continuation_strips_duplicated_lead_in(monkeypatch):
    _fake_llm_response(monkeypatch, "Right now, your website is completely down and losing customers.")
    result = ld.generate_middle_continuation("Test Salon", "Hair Salons", None, None, "broken")
    assert not result.lower().startswith("right now")
    assert result == "is completely down and losing customers."


def test_generate_middle_continuation_sanitizes_em_dash(monkeypatch):
    _fake_llm_response(monkeypatch, "is slow — and losing customers because of it.")
    result = ld.generate_middle_continuation("Test Salon", "Hair Salons", None, None, "broken")
    assert "—" not in result
    assert "," in result


def test_generate_middle_continuation_returns_none_on_http_error(monkeypatch):
    _fake_llm_response(monkeypatch, None)  # hp_llm.generate() returns None on any non-200
    assert ld.generate_middle_continuation("Test Salon", "Hair Salons", None, None, "broken") is None


# --- generate_middle_continuation(): Enhanced Website-Based Lead
# Qualification epic (2026-08-30) — gap_reason blending ---

def test_generate_middle_continuation_gap_only_when_no_technical_reason(monkeypatch):
    """PageSpeed found nothing wrong (reason=None), but the crawler found
    a missing booking system — the prompt must still produce real content
    about that gap, not silently fail."""
    captured = {}

    def fake_generate(prompt, timeout=None):
        captured["prompt"] = prompt
        return "doesn't let customers book online, so they call a competitor instead."

    monkeypatch.setattr(ld.hp_llm, "generate", fake_generate)
    result = ld.generate_middle_continuation("Test Salon", "Hair Salons", None, None, None, gap_reason="no_booking")
    assert result is not None
    assert "book an appointment online" in captured["prompt"]


def test_generate_middle_continuation_blends_technical_and_gap_reasons(monkeypatch):
    """Cyril's call, 2026-08-30: when a lead has BOTH a real PageSpeed
    issue AND a missing-capability gap, the prompt must mention both,
    not just pick one."""
    captured = {}

    def fake_generate(prompt, timeout=None):
        captured["prompt"] = prompt
        return "is slow to load and also has no way to book online."

    monkeypatch.setattr(ld.hp_llm, "generate", fake_generate)
    pdata = {"scores": {"performance": 0.3}}
    monkeypatch.setattr(ld, "get_real_load_time", lambda p: "6.2 seconds")
    monkeypatch.setattr(ld, "get_top_findings", lambda cat, p, max_findings=1: ["large images"])
    ld.generate_middle_continuation("Test Salon", "Hair Salons", None, pdata, "slow", gap_reason="no_booking")
    assert "6.2 seconds" in captured["prompt"]
    assert "book an appointment online" in captured["prompt"]


def test_generate_middle_continuation_returns_none_when_neither_reason_present():
    """No PageSpeed issue and no crawler gap — nothing real to write
    about. Must never fabricate a reason."""
    assert ld.generate_middle_continuation("Test Salon", "Hair Salons", None, None, None, gap_reason=None) is None


# --- generate_hero_line(): Gap 6 amendment ---

def test_generate_hero_line_sanitizes_em_dash(monkeypatch):
    _fake_llm_response(monkeypatch, "Fresh cuts — friendly faces, every time.")
    result = ld.generate_hero_line("Test Salon", "Hair Salons", "Wichita, KS")
    assert "—" not in result
    assert "," in result


def test_generate_hero_line_strips_surrounding_quotes(monkeypatch):
    _fake_llm_response(monkeypatch, '"Your neighborhood barber shop."')
    result = ld.generate_hero_line("Test Salon", "Barber Shops", "Wichita, KS")
    assert result == "Your neighborhood barber shop."


def test_generate_hero_line_returns_none_on_error(monkeypatch):
    _fake_llm_response(monkeypatch, None)  # hp_llm.generate() returns None on any exception too
    assert ld.generate_hero_line("Test Salon", "Hair Salons", "Wichita, KS") is None


# --- write_to_notion(): Hero Line field ---

def _capture_notion_payload(monkeypatch):
    captured = {}

    class FakeResponse:
        status_code = 200
        text = ""

    def fake_post(url, headers=None, json=None):
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


def test_write_to_notion_writes_hero_line_when_generated(monkeypatch):
    captured = _capture_notion_payload(monkeypatch)
    place = {"displayName": {"text": "Test Salon"}}
    ld.write_to_notion(**_base_write_args(place), hero_line="Your neighborhood salon.")
    content = captured["payload"]["properties"]["Hero Line"]["rich_text"][0]["text"]["content"]
    assert content == "Your neighborhood salon."


def test_write_to_notion_omits_hero_line_when_generation_failed(monkeypatch):
    captured = _capture_notion_payload(monkeypatch)
    place = {"displayName": {"text": "Test Salon"}}
    ld.write_to_notion(**_base_write_args(place), hero_line=None)
    assert "Hero Line" not in captured["payload"]["properties"]


# --- find_competitor(): AC "10+ rank gap, review count within 50 -> beatable" ---

def _place(name, reviews):
    return {"displayName": {"text": name}, "userRatingCount": reviews}


def test_find_competitor_flags_beatable_at_exact_thresholds():
    # Lead is at index 10 (rank 11). Candidate at index 0 (rank 1) is a
    # rank gap of exactly 10, review delta of exactly 50 -> both at the
    # boundary, both AC thresholds are inclusive ("within 50", "10+").
    places = [_place("Competitor", 150)] + [_place(f"Filler {i}", 100) for i in range(9)] + [_place("Lead", 100)]
    competitor = ld.find_competitor(places, 10)
    assert competitor is not None
    assert competitor["name"] == "Competitor"
    assert competitor["rank"] == 1
    assert competitor["delta"] == 50


def test_find_competitor_not_flagged_just_under_rank_gap():
    # Lead at index 9 (rank 10) -> only a 9-rank gap from index 0, under the 10+ threshold.
    places = [_place("Competitor", 150)] + [_place(f"Filler {i}", 100) for i in range(8)] + [_place("Lead", 100)]
    assert ld.find_competitor(places, 9) is None


def test_find_competitor_not_flagged_just_over_review_delta():
    # Rank gap satisfied (10), but review delta is 51 -> just past the "within 50" threshold.
    places = [_place("Competitor", 151)] + [_place(f"Filler {i}", 100) for i in range(9)] + [_place("Lead", 100)]
    competitor = ld.find_competitor(places, 10)
    assert competitor is None


def test_find_competitor_returns_none_when_lead_is_top_ranked():
    places = [_place("Lead", 100)]
    assert ld.find_competitor(places, 0) is None


# --- build_email(): dashboard-editable subject/body templates (2026-08-25) ---
# Subject/body used to be hardcoded f-strings in this function; now they're
# file-backed (email_templates/cold_subject_*.md, cold_body.md) same as
# every other piece of copy in the dashboard's Content editor, rendered
# via hp_template.render()'s [TOKEN] substitution.

def test_build_email_broken_reason_uses_business_name_in_subject():
    subject, _ = ld.build_email("Acme Salon", None, "isn't loading at all.", "broken", "hair salons")
    assert "Acme Salon" in subject


def test_build_email_slow_with_competitor_names_competitor_in_subject():
    competitor = {"name": "Rival Salon", "rank": 1, "reviews": 200, "delta": 50}
    subject, _ = ld.build_email("Acme Salon", competitor, "is slow.", "slow", "hair salons")
    assert "Acme Salon" in subject
    assert "Rival Salon" in subject


def test_build_email_slow_without_competitor_falls_back_no_competitor_name_leaked():
    subject, _ = ld.build_email("Acme Salon", None, "is slow.", "slow", "hair salons")
    assert "Acme Salon" in subject
    assert "[COMPETITOR_NAME]" not in subject  # unmatched-token regression: must not leak a literal token


@pytest.mark.parametrize("reason", ["broken", "slow", "seo", "quality"])
def test_build_email_every_reason_has_a_real_subject_file(reason):
    competitor = {"name": "Rival Salon", "rank": 1, "reviews": 200, "delta": 50} if reason == "slow" else None
    subject, _ = ld.build_email("Acme Salon", competitor, "needs work.", reason, "hair salons")
    assert subject  # non-empty, real file loaded, no crash


def test_build_email_body_includes_niche_and_middle_continuation():
    _, body = ld.build_email("Acme Salon", None, "looks outdated on mobile.", "quality", "pet spas")
    assert "pet spas near me" in body
    assert "looks outdated on mobile." in body
    assert "Cyril" in body  # sign-off still present


# --- build_email(): gap_reason-based subject (Enhanced Website-Based
# Lead Qualification epic, 2026-08-30) — used when reason is None (no
# PageSpeed issue) but the crawler found a missing capability. ---

@pytest.mark.parametrize("gap_reason", ["no_booking", "no_call_cta"])
def test_build_email_every_gap_reason_has_a_real_subject_file(gap_reason):
    subject, _ = ld.build_email("Acme Salon", None, "needs work.", None, "hair salons", gap_reason=gap_reason)
    assert subject
    assert "Acme Salon" in subject


def test_build_email_technical_reason_wins_subject_over_gap_reason_when_both_present():
    """Cyril's call: when both a PageSpeed issue and a crawler gap are
    present, the body blends both (generate_middle_continuation()) but
    the subject still comes from the technical reason's own file."""
    subject, _ = ld.build_email("Acme Salon", None, "needs work.", "broken", "hair salons", gap_reason="no_booking")
    assert subject == ld.build_email("Acme Salon", None, "needs work.", "broken", "hair salons")[0]


def test_build_email_reflects_dashboard_edits(tmp_path, monkeypatch):
    monkeypatch.setattr(ld, "EMAIL_TEMPLATES_DIR", str(tmp_path))
    (tmp_path / "cold_subject_broken.md").write_text("Edited subject for [BUSINESS_NAME]\n")
    (tmp_path / "cold_body.md").write_text("Edited body about [NICHE]: [MIDDLE_CONTINUATION]\n")
    subject, body = ld.build_email("Acme Salon", None, "is broken.", "broken", "gyms")
    assert subject == "Edited subject for Acme Salon"
    assert body == "Edited body about gyms: is broken."


# --- has_valid_mx_record(): zero-cost pre-send domain check, added
# 2026-08-27 in response to a real bounce (Example Barber Shop) —
# confirms a candidate email's domain has mail servers configured at all,
# before it ever enters the CRM. Deliberately MX-only, not a full SMTP
# mailbox probe (a paid/riskier check Cyril explicitly didn't want). ---

class _FakeMXAnswers(list):
    pass


def test_has_valid_mx_record_true_when_mx_records_exist(monkeypatch):
    monkeypatch.setattr(ld.DNS_RESOLVER, "resolve", lambda domain, kind: _FakeMXAnswers(["mx1.example.com"]))
    assert ld.has_valid_mx_record("owner@realbiz-fake.test") is True


def test_has_valid_mx_record_false_on_nxdomain(monkeypatch):
    def raise_nxdomain(domain, kind):
        raise ld.dns.resolver.NXDOMAIN()

    monkeypatch.setattr(ld.DNS_RESOLVER, "resolve", raise_nxdomain)
    assert ld.has_valid_mx_record("owner@this-domain-does-not-exist-fake.test") is False


def test_has_valid_mx_record_false_on_no_answer(monkeypatch):
    def raise_no_answer(domain, kind):
        raise ld.dns.resolver.NoAnswer()

    monkeypatch.setattr(ld.DNS_RESOLVER, "resolve", raise_no_answer)
    assert ld.has_valid_mx_record("owner@no-mail-configured-fake.test") is False


def test_has_valid_mx_record_fails_open_on_transient_resolver_error(monkeypatch):
    """A resolver timeout or network blip is not the same as "this domain
    has no mail" — must not discard an otherwise-good lead over a
    transient DNS hiccup."""
    def raise_timeout(domain, kind):
        raise dns.exception.Timeout()

    monkeypatch.setattr(ld.DNS_RESOLVER, "resolve", raise_timeout)
    assert ld.has_valid_mx_record("owner@realbiz-fake.test") is True


def test_has_valid_mx_record_false_for_malformed_input():
    assert ld.has_valid_mx_record(None) is False
    assert ld.has_valid_mx_record("") is False
    assert ld.has_valid_mx_record("not-an-email") is False


def test_has_valid_mx_record_queries_the_domain_only():
    captured = {}

    def fake_resolve(domain, kind):
        captured["domain"] = domain
        captured["kind"] = kind
        return _FakeMXAnswers(["mx1.example.com"])

    import unittest.mock
    with unittest.mock.patch.object(ld.DNS_RESOLVER, "resolve", fake_resolve):
        ld.has_valid_mx_record("owner@realbiz-fake.test")
    assert captured == {"domain": "realbiz-fake.test", "kind": "MX"}


# --- extract_email_from_html(): MX check gates which candidate wins ---

def test_extract_email_from_html_rejects_candidate_with_no_mx_record(monkeypatch):
    """A syntactically-fine email whose domain has no mail configured must
    not win — this is exactly the gap a business's real, valid-looking but
    dead domain would otherwise slip through as."""
    monkeypatch.setattr(ld, "has_valid_mx_record", lambda email: False)
    html = '<a href="mailto:owner@deaddomain-fake.test">Email us</a>'
    assert ld.extract_email_from_html(html) is None


def test_extract_email_from_html_accepts_candidate_with_valid_mx_record(monkeypatch):
    monkeypatch.setattr(ld, "has_valid_mx_record", lambda email: True)
    html = '<a href="mailto:owner@realbiz-fake.test">Email us</a>'
    assert ld.extract_email_from_html(html) == "owner@realbiz-fake.test"


def test_extract_email_from_html_falls_through_to_next_candidate_on_failed_mx(monkeypatch):
    """Same 'try next source' philosophy as the junk-denylist check —
    a bad-MX candidate doesn't abort the whole search, just that one
    candidate."""
    good_domains = {"realbiz-fake.test"}
    monkeypatch.setattr(ld, "has_valid_mx_record", lambda email: email.split("@")[-1] in good_domains)
    html = "Contact bad@deaddomain-fake.test or good@realbiz-fake.test"
    assert ld.extract_email_from_html(html) == "good@realbiz-fake.test"


# --- extract_visible_text() / scrape_website_content_snapshot(): real
# website content capture, added 2026-08-28 as raw material for Cyril's
# upcoming ghost-site content rewrite step (format not built yet — this is
# just the scraping + storage piece). ---

def test_extract_visible_text_strips_script_and_style_content():
    html = "<html><head><style>.x{color:red}</style></head><body><script>alert(1)</script><p>Real content here.</p></body></html>"
    text = ld.extract_visible_text(html)
    assert "Real content here." in text
    assert "alert" not in text
    assert "color:red" not in text


def test_extract_visible_text_strips_nav_and_footer():
    html = "<body><nav>Home About Contact</nav><main><p>The actual business copy.</p></main><footer>(c) 2026</footer></body>"
    text = ld.extract_visible_text(html)
    assert "The actual business copy." in text
    assert "Home About Contact" not in text
    assert "(c) 2026" not in text


def test_extract_visible_text_collapses_whitespace():
    html = "<body><p>Line one.</p>\n\n\n<p>   Line two.   </p></body>"
    text = ld.extract_visible_text(html)
    assert "  " not in text  # no double-spaces left over
    assert "Line one." in text and "Line two." in text


def test_scrape_website_content_snapshot_returns_none_for_no_url():
    assert ld.scrape_website_content_snapshot(None) is None
    assert ld.scrape_website_content_snapshot("") is None


def test_scrape_website_content_snapshot_combines_homepage_and_subpages(monkeypatch):
    class FakeResponse:
        def __init__(self, status_code, text):
            self.status_code = status_code
            self.text = text

    def fake_get(url, timeout=None, headers=None):
        if url == "https://realbiz-fake.test":
            return FakeResponse(200, "<body><p>Homepage copy.</p></body>")
        if url == "https://realbiz-fake.test/about":
            return FakeResponse(200, "<body><p>About us copy.</p></body>")
        return FakeResponse(404, "")

    monkeypatch.setattr(ld.requests, "get", fake_get)
    snapshot = ld.scrape_website_content_snapshot("https://realbiz-fake.test")
    assert "Homepage copy." in snapshot
    assert "About us copy." in snapshot


def test_scrape_website_content_snapshot_best_effort_on_page_failures(monkeypatch):
    """A slow/broken lead site must never block lead qualification —
    every page fetch failing just means an empty-but-non-crashing result."""
    def fake_get(url, timeout=None, headers=None):
        raise ld.requests.exceptions.ConnectionError("simulated failure")

    monkeypatch.setattr(ld.requests, "get", fake_get)
    assert ld.scrape_website_content_snapshot("https://deaddomain-fake.test") is None


def test_scrape_website_content_snapshot_truncated_to_max_chars(monkeypatch):
    class FakeResponse:
        status_code = 200
        text = f"<body><p>{'x' * 5000}</p></body>"

    monkeypatch.setattr(ld.requests, "get", lambda url, timeout=None, headers=None: FakeResponse())
    snapshot = ld.scrape_website_content_snapshot("https://realbiz-fake.test")
    assert len(snapshot) == ld.CONTENT_SNAPSHOT_MAX_CHARS


def test_scrape_website_content_snapshot_stays_under_notion_utf16_limit_with_emoji(monkeypatch):
    """Regression, found live 2026-09-02 (Chandler, AZ run): Notion's
    rich_text length limit is measured in UTF-16 code units, not Python
    characters. A page whose scraped text is exactly CONTENT_SNAPSHOT_MAX_CHARS
    Python characters but contains astral-plane characters (emoji encode as
    two UTF-16 units each) passed the old plain [:2000] slice but still got
    rejected by Notion's API with a validation_error, discarding an
    otherwise-qualifying lead outright rather than just losing snapshot text."""
    class FakeResponse:
        status_code = 200
        text = f"<body><p>{'😀' * 2000}</p></body>"

    monkeypatch.setattr(ld.requests, "get", lambda url, timeout=None, headers=None: FakeResponse())
    snapshot = ld.scrape_website_content_snapshot("https://realbiz-fake.test")
    assert len(snapshot.encode("utf-16-le")) // 2 <= ld.CONTENT_SNAPSHOT_MAX_CHARS


def test_truncate_to_utf16_units_does_not_split_a_surrogate_pair():
    text = "a" * 1999 + "😀"  # emoji is 2 UTF-16 units, would land exactly on the boundary
    truncated = ld._truncate_to_utf16_units(text, 2000)
    assert len(truncated.encode("utf-16-le")) // 2 <= 2000
    truncated.encode("utf-16-le").decode("utf-16-le")  # raises if a surrogate got split


# --- write_to_notion(): new Website Content Snapshot property ---

def test_write_to_notion_includes_content_snapshot_when_present(monkeypatch):
    captured = {}

    class FakeResponse:
        status_code = 200

    def fake_post(url, headers=None, json=None):
        captured["payload"] = json
        return FakeResponse()

    monkeypatch.setattr(ld.requests, "post", fake_post)
    place = {"displayName": {"text": "Acme Salon"}}
    contact = {"email": "owner@realbiz-fake.test", "social": None}
    ld.write_to_notion(place, None, "slow", None, contact, "Subject", "Body", "hair salons",
                        "Wichita, KS", content_snapshot="Real scraped copy about the business.")
    props = captured["payload"]["properties"]
    assert props["Website Content Snapshot"]["rich_text"][0]["text"]["content"] == "Real scraped copy about the business."


def test_write_to_notion_omits_content_snapshot_when_none(monkeypatch):
    captured = {}

    class FakeResponse:
        status_code = 200

    def fake_post(url, headers=None, json=None):
        captured["payload"] = json
        return FakeResponse()

    monkeypatch.setattr(ld.requests, "post", fake_post)
    place = {"displayName": {"text": "Acme Salon"}}
    contact = {"email": "owner@realbiz-fake.test", "social": None}
    ld.write_to_notion(place, None, "slow", None, contact, "Subject", "Body", "hair salons", "Wichita, KS")
    props = captured["payload"]["properties"]
    assert "Website Content Snapshot" not in props
