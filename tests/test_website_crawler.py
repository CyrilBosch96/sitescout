#!/usr/bin/env python3
"""
Tests for scripts/website_crawler.py — Enhanced Website-Based Lead
Qualification epic (crawler, booking detection, call CTA detection; CMS
detection removed entirely 2026-08-31).

Covers:
  - Crawler is loop-safe (visited-URL set, hard page cap), never follows
    off-site links, and reports Success/Failed/Skipped correctly.
  - Booking detection requires a real link/embed to a known platform, not
    just the word "appointment" or a generic contact form.
  - Call CTA detection requires a real tel: link (or at minimum
    call-oriented CTA text) — a plain-text phone number alone is not
    enough.
"""

import os
import sys

import pytest

SCRIPTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
sys.path.insert(0, SCRIPTS_DIR)

import website_crawler as wc  # noqa: E402


# --- crawl_website(): AC "reachable", "Success/Failed/Skipped", loop-safety ---

def test_crawl_website_skipped_for_no_url():
    result = wc.crawl_website(None)
    assert result["status"] == "Skipped"
    assert result["pages"] == {}


def test_crawl_website_success_fetches_homepage(monkeypatch):
    class FakeResponse:
        status_code = 200
        text = "<html><body>Homepage content, no relevant links here.</body></html>"

    monkeypatch.setattr(wc.requests, "get", lambda url, timeout=None, headers=None: FakeResponse())
    result = wc.crawl_website("https://realbiz-fake.test")
    assert result["status"] == "Success"
    assert "https://realbiz-fake.test" in result["pages"]


def test_crawl_website_failed_when_homepage_unreachable(monkeypatch):
    def fake_get(url, timeout=None, headers=None):
        raise wc.requests.exceptions.ConnectionError("simulated failure")

    monkeypatch.setattr(wc.requests, "get", fake_get)
    result = wc.crawl_website("https://deaddomain-fake.test")
    assert result["status"] == "Failed"
    assert result["pages"] == {}


def test_crawl_website_follows_relevant_internal_links(monkeypatch):
    homepage_html = '<html><body><a href="/services">Our Services</a> <a href="/random-page">Random</a></body></html>'
    services_html = "<html><body>Services page content.</body></html>"

    def fake_get(url, timeout=None, headers=None):
        class FakeResponse:
            status_code = 200
            text = services_html if "services" in url else homepage_html
        return FakeResponse()

    monkeypatch.setattr(wc.requests, "get", fake_get)
    result = wc.crawl_website("https://realbiz-fake.test")
    assert "https://realbiz-fake.test" in result["pages"]
    assert "https://realbiz-fake.test/services" in result["pages"]
    # "Random" link doesn't match any relevant keyword — must not be followed.
    assert not any("random-page" in url for url in result["pages"])


def test_crawl_website_never_follows_offsite_links(monkeypatch):
    homepage_html = '<html><body><a href="https://booking-provider-fake.test/book">Book Now</a></body></html>'

    calls = []

    def fake_get(url, timeout=None, headers=None):
        calls.append(url)
        class FakeResponse:
            status_code = 200
            text = homepage_html
        return FakeResponse()

    monkeypatch.setattr(wc.requests, "get", fake_get)
    wc.crawl_website("https://realbiz-fake.test")
    assert not any("booking-provider-fake.test" in c for c in calls)


def test_crawl_website_probes_common_booking_paths_not_linked_from_homepage(monkeypatch):
    """Regression, found live 2026-08-31: a real qualifying lead
    (foxandash.com) had a genuine, working "Book an Appointment" nav
    button whose destination never appeared anywhere in the homepage's
    static HTML — it's rendered by client-side JavaScript, which this
    crawler deliberately doesn't execute. The actual booking page itself
    (once reached) had a real booking platform link. Fix: probe a short
    list of common booking-page URL slugs directly, regardless of
    whether any link to them was found."""
    homepage_html = "<html><body>No links to any booking page here at all.</body></html>"
    booking_page_html = '<html><body><a href="https://getsquire.com/booking/brands/some-shop">Book Here</a></body></html>'

    def fake_get(url, timeout=None, headers=None):
        class FakeResponse:
            status_code = 200
            text = booking_page_html if "book-an-appointment" in url else homepage_html
        return FakeResponse()

    monkeypatch.setattr(wc.requests, "get", fake_get)
    result = wc.crawl_website("https://realbiz-fake.test")
    assert "https://realbiz-fake.test/book-an-appointment" in result["pages"]


def test_crawl_website_real_relevant_links_take_priority_over_path_guesses(monkeypatch):
    """Real discovered links must win the limited page budget over the
    lower-confidence path guesses — guesses are a fallback, not equal
    priority."""
    links = "".join(f'<a href="/service-{i}">Service {i}</a>' for i in range(wc.MAX_PAGES_PER_SITE))
    homepage_html = f"<html><body>{links}</body></html>"

    fetched = []

    def fake_get(url, timeout=None, headers=None):
        fetched.append(url)
        class FakeResponse:
            status_code = 200
            text = homepage_html if url == "https://realbiz-fake.test" else "<html><body>page</body></html>"
        return FakeResponse()

    monkeypatch.setattr(wc.requests, "get", fake_get)
    result = wc.crawl_website("https://realbiz-fake.test")
    assert not any(guess in url for url in result["pages"] for guess in wc.COMMON_BOOKING_PATH_GUESSES)


def test_crawl_website_respects_max_pages_cap(monkeypatch):
    # 10 relevant-looking links on the homepage — must still cap at
    # MAX_PAGES_PER_SITE total fetches (homepage included), not fetch all 10.
    links = "".join(f'<a href="/service-{i}">Service {i}</a>' for i in range(10))
    homepage_html = f"<html><body>{links}</body></html>"

    def fake_get(url, timeout=None, headers=None):
        class FakeResponse:
            status_code = 200
            text = homepage_html if url == "https://realbiz-fake.test" else "<html><body>page</body></html>"
        return FakeResponse()

    monkeypatch.setattr(wc.requests, "get", fake_get)
    result = wc.crawl_website("https://realbiz-fake.test")
    assert len(result["pages"]) <= wc.MAX_PAGES_PER_SITE


# --- detect_booking(): AC "real integration only, not the word 'appointment'" ---

def test_detect_booking_unknown_when_no_pages():
    status, provider, source = wc.detect_booking({})
    assert status == "Booking Unknown"


def test_detect_booking_detected_via_known_platform_link():
    pages = {"https://x.test": '<a href="https://fresha.com/book/salon-xyz">Book an appointment</a>'}
    status, provider, source = wc.detect_booking(pages)
    assert status == "Booking Detected"
    assert provider == "Fresha"


def test_detect_booking_detected_via_iframe_embed():
    pages = {"https://x.test": '<iframe src="https://widget.calendly.com/salon-xyz"></iframe>'}
    status, provider, source = wc.detect_booking(pages)
    assert status == "Booking Detected"
    assert provider == "Calendly"


def test_detect_booking_not_detected_for_bare_appointment_text():
    """The word "appointment" alone, with no real booking integration,
    must not be treated as booking detected — this is an explicit
    Must-Not in the spec."""
    pages = {"https://x.test": "<p>Call us to schedule an appointment today!</p>"}
    status, provider, source = wc.detect_booking(pages)
    assert status == "Booking Not Detected"
    assert provider is None


def test_detect_booking_not_detected_for_generic_contact_form():
    """A generic contact form is not appointment booking — explicit
    Must-Not in the spec."""
    pages = {"https://x.test": '<form action="/contact"><input name="message"></form>'}
    status, provider, source = wc.detect_booking(pages)
    assert status == "Booking Not Detected"


def test_detect_booking_detected_via_squire_link():
    """Regression, found live 2026-08-31: a real qualifying lead
    (barbershopoldtown.com) had a genuine booking link to Squire
    (getsquire.com), a real barbershop-specific booking platform that
    was simply missing from BOOKING_PLATFORM_DOMAINS."""
    pages = {"https://x.test": '<a href="https://getsquire.com/booking/brands/some-shop">Book Now</a>'}
    status, provider, source = wc.detect_booking(pages)
    assert status == "Booking Detected"
    assert provider == "Squire"


def test_detect_booking_detected_via_native_wix_bookings_widget():
    """Regression, found live 2026-08-31: a real qualifying Wix-built
    lead (ogsbarbershop.com) had a genuine, fully functional native Wix
    Bookings widget rendered from Wix's own asset host, not a link to
    any external domain — the href/src-only check structurally couldn't
    see it. Verified live against the real page: Wix's own internal
    service name for the booking widget bundle appears in an inline
    JSON config blob, not an HTML attribute, so this must match as a
    raw substring anywhere on the page."""
    pages = {
        "https://x.test": (
            '<script>var config = {"staticBaseUrl":'
            '"https://static.parastorage.com/services/bookings-widget-viewer/1.1788.0/"};</script>'
        )
    }
    status, provider, source = wc.detect_booking(pages)
    assert status == "Booking Detected"
    assert provider == "Wix Bookings"


def test_detect_booking_detected_via_square_appointment_button_type():
    """Regression, found live 2026-09-01: a real qualifying lead built on
    Square's own website builder (square.site) had a genuine, active
    Square Appointments "Book now" button — real per-business config
    (type:"squareAppointment", a real Square location ID), not template
    boilerplate. The first-tried single exact marker (Square's
    APPOINTMENTS_SET_UP flag) missed this real lead entirely — verified
    live it simply wasn't present anywhere on the page despite the real
    booking button being there, so a generalized "type":"*appointment*"
    regex is used instead of one narrow exact-string marker."""
    pages = {
        "https://x.test": (
            '{"actionButton":{"link":{"squareAppointment":{"locationId":"abc123"}},'
            '"type":"squareAppointment"},"label":"BOOK NOW"}'
        )
    }
    status, provider, source = wc.detect_booking(pages)
    assert status == "Booking Detected"
    assert provider == "Square Appointments"


def test_detect_booking_detected_via_square_appointments_nav_item_type():
    """Regression, found live 2026-09-01: a second real qualifying lead
    (also square.site) had its booking evidence in a nav-menu-item shape
    — {"type":"appointments",...} — a different JSON shape from the
    booking-button case above, neither matching Square's
    APPOINTMENTS_SET_UP flag. The generalized regex catches both shapes
    with one rule."""
    pages = {
        "https://x.test": '{"navigation":[{"link":{"appointments":true},"type":"appointments","title":"Book"}]}'
    }
    status, provider, source = wc.detect_booking(pages)
    assert status == "Booking Detected"
    assert provider == "Square Appointments"


def test_detect_booking_detected_via_self_hosted_enable_appointments_flag():
    """Regression, found live 2026-09-01: a real qualifying lead had a
    genuine, fully-configured self-hosted booking page (a real /booking
    nav link, an actual "Book an Appointment" button, real deposit/
    cancellation-policy config) on a platform (franpos.com) not covered
    by BOOKING_PLATFORM_DOMAINS — and unreachable by that check anyway,
    since the booking page lives on the business's own domain (a
    relative href, not an external one)."""
    pages = {
        "https://x.test": (
            '<a href="/booking">Booking</a>'
            '<script>var config = {"depositPercentage":null,"enableAppointments":true,"enableRewards":false};</script>'
        )
    }
    status, provider, source = wc.detect_booking(pages)
    assert status == "Booking Detected"
    assert provider == "Online Booking"


def test_detect_booking_square_site_hosting_alone_is_not_detected():
    """A business's site merely being hosted on square.site must not, by
    itself, mean they have Appointments configured — that platform-wide
    constant (PUBLIC_SQUARE_APPTS_URL_BASE) is present on every
    square.site site regardless of whether the merchant actually set up
    booking. Verified live against 2 real non-booking sites: neither the
    constant nor the "type":"*appointment*" regex false-positives on it."""
    pages = {
        "https://x.test": (
            "<script>window.PUBLIC_SQUARE_URL_BASE = 'squareup.com'; "
            "window.PUBLIC_SQUARE_APPTS_URL_BASE = 'app.squareup.com';</script>"
        )
    }
    status, provider, source = wc.detect_booking(pages)
    assert status == "Booking Not Detected"
    assert provider is None


def test_detect_booking_native_marker_does_not_override_generic_appointment_text():
    """A page that merely mentions 'appointment' with no native marker
    and no known platform link still correctly returns Not Detected —
    the native-marker check must not accidentally widen what counts as
    evidence beyond the two verified signal types."""
    pages = {"https://x.test": "<p>Book your appointment today and experience the difference!</p>"}
    status, provider, source = wc.detect_booking(pages)
    assert status == "Booking Not Detected"
    assert provider is None


# --- detect_call_cta(): AC "tel: links, phone number alone is not enough" ---

def test_detect_call_cta_unknown_when_no_pages():
    status, has_tel, evidence = wc.detect_call_cta({})
    assert status == "Call CTA Unknown"


def test_detect_call_cta_detected_via_tel_link():
    pages = {"https://x.test": '<a href="tel:+15551234567">Call Now</a>'}
    status, has_tel, evidence = wc.detect_call_cta(pages)
    assert status == "Call CTA Detected"
    assert has_tel is True


def test_detect_call_cta_not_detected_for_plain_text_phone_number():
    """A phone number displayed as plain text, with no tel: link and no
    call-oriented CTA text, must not be treated as a usable call CTA —
    explicit Must-Not in the spec."""
    pages = {"https://x.test": "<p>Reach us at 555-123-4567 during business hours.</p>"}
    status, has_tel, evidence = wc.detect_call_cta(pages)
    assert status == "Call CTA Not Detected"
    assert has_tel is False


def test_detect_call_cta_detected_via_call_text_without_tel_link():
    pages = {"https://x.test": "<button>Call Us Today</button><p>555-123-4567</p>"}
    status, has_tel, evidence = wc.detect_call_cta(pages)
    assert status == "Call CTA Detected"
    assert has_tel is False  # weaker signal — text present, but no real tel: link


# --- crawl_and_qualify(): end-to-end wiring ---

def test_crawl_and_qualify_combines_all_signals(monkeypatch):
    homepage_html = (
        '<html><head></head>'
        '<body><a href="tel:+15551234567">Call Now</a>'
        '<a href="https://fresha.com/book/x">Book Now</a></body></html>'
    )

    def fake_get(url, timeout=None, headers=None):
        class FakeResponse:
            status_code = 200
            text = homepage_html
        return FakeResponse()

    monkeypatch.setattr(wc.requests, "get", fake_get)
    result = wc.crawl_and_qualify("https://realbiz-fake.test")
    assert result["crawl_status"] == "Success"
    assert result["booking_status"] == "Booking Detected"
    assert result["booking_provider"] == "Fresha"
    assert result["call_cta_status"] == "Call CTA Detected"


def test_crawl_and_qualify_no_website_confidently_qualifies_not_detected():
    """Regression, found live 2026-08-30: a business with no website at
    all was sitting in "Unknown" (skipped/retried forever) instead of a
    real "Not Detected" — no website confidently means no booking system
    exists, since there's no site for one to run on. This is distinct
    from "Failed" (a real URL that just couldn't be reached this time),
    which stays genuinely Unknown."""
    result = wc.crawl_and_qualify(None)
    assert result["crawl_status"] == "Skipped"
    assert result["booking_status"] == "Booking Not Detected"
    assert result["call_cta_status"] == "Call CTA Not Detected"


def test_crawl_and_qualify_never_raises_on_total_failure(monkeypatch):
    def fake_get(url, timeout=None, headers=None):
        raise wc.requests.exceptions.Timeout("simulated")

    monkeypatch.setattr(wc.requests, "get", fake_get)
    result = wc.crawl_and_qualify("https://deaddomain-fake.test")
    assert result["crawl_status"] == "Failed"
    assert result["booking_status"] == "Booking Unknown"
    assert result["call_cta_status"] == "Call CTA Unknown"
