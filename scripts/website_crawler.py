#!/usr/bin/env python3
"""
Website Crawler + Booking/Call-CTA Detection — SiteScout
(2026-08-30, CMS detection removed 2026-08-31)

Enhanced Website-Based Lead Qualification epic. Augments PageSpeed
Insights (which only measures technical performance) with real functional
signals: does this business already have online booking and a usable
click-to-call path? A fast site with neither is a much stronger, more
concrete sales pitch than "your site is slow." CMS detection was removed
entirely (Cyril's call, 2026-08-31) — CMS presence stopped being a useful
signal once most small-business sites turned out to be built on
Wix/Squarespace regardless of how sophisticated the business's actual web
presence is; booking presence alone now drives qualification.

Deliberately public-signals-only: never touches authenticated/private
areas, never submits forms, never books an appointment, never makes any
write to the prospect's site. Read-only GET requests, same as the
existing find_contact_info()/scrape_website_content_snapshot() in
lead_discovery_and_evaluation_Contactsearch.py.

Headless-browser mobile-layout checks (User Story 5's "Should") are
deliberately NOT built here — Cyril's call, 2026-08-30 — the production
VM is a 1GB RAM e2-micro, and a headless Chromium instance typically
needs 300-500MB+, a real risk to the whole pipeline for a "Should", not a
"Must". PageSpeed remains the sole mobile signal.
"""

import re
import time
from urllib.parse import urljoin, urlparse

import requests

CRAWL_TIMEOUT_SECONDS = 10
MAX_PAGES_PER_SITE = 5
USER_AGENT = "Mozilla/5.0 (compatible; SiteScoutBot/1.0)"

# Internal links worth following beyond the homepage — service, booking,
# contact, and location pages are where booking/call-CTA signals that
# aren't on the homepage tend to live. Matched against link text and href
# path, case-insensitive.
RELEVANT_LINK_KEYWORDS = (
    "service", "book", "appointment", "schedule", "contact",
    "location", "about",
)

# Found live 2026-08-31: a real qualifying lead (foxandash.com) had a
# genuine, working "Book an Appointment" nav button whose destination
# never appeared anywhere in the homepage's static HTML at all — it's
# rendered by client-side JavaScript, which this crawler deliberately
# doesn't execute (Cyril's call, 2026-08-30: skip headless rendering for
# now). Since the destination page itself DID have a real Squire booking
# link once reached directly, the fix isn't detection — it's reaching
# the page at all. Rather than build full JS rendering, probe a short
# list of common booking-page URL slugs directly on every site,
# regardless of whether any link to them was actually found. Cheap
# (a handful of extra requests, still bounded by MAX_PAGES_PER_SITE) and
# closes exactly this failure mode without the bigger headless-browser
# investment.
COMMON_BOOKING_PATH_GUESSES = (
    "/book-an-appointment", "/book-now", "/book-online", "/book",
    "/booking", "/appointments", "/schedule", "/schedule-appointment",
)


def _is_relevant_link(href, link_text):
    haystack = f"{href} {link_text}".lower()
    return any(kw in haystack for kw in RELEVANT_LINK_KEYWORDS)


def _same_site(base_netloc, candidate_url):
    return urlparse(candidate_url).netloc == base_netloc


def crawl_website(website_url):
    """Homepage plus up to MAX_PAGES_PER_SITE-1 relevant internal pages.
    Returns {"status": "Success"|"Failed"|"Skipped", "pages": {url: html},
    "crawled_at": iso timestamp}. Loop-safe (a visited-URL set, a hard page
    cap) and never follows an off-site link, so a business's own site
    can't redirect the crawl into someone else's domain indefinitely."""
    from datetime import datetime, timezone
    crawled_at = datetime.now(timezone.utc).isoformat()

    if not website_url:
        return {"status": "Skipped", "pages": {}, "crawled_at": crawled_at}

    pages = {}
    visited = set()
    to_visit = [website_url]
    base_netloc = urlparse(website_url).netloc

    while to_visit and len(pages) < MAX_PAGES_PER_SITE:
        url = to_visit.pop(0)
        if url in visited:
            continue
        visited.add(url)

        try:
            resp = requests.get(
                url, timeout=CRAWL_TIMEOUT_SECONDS,
                headers={"User-Agent": USER_AGENT},
            )
        except Exception:
            continue

        if resp.status_code != 200:
            continue

        pages[url] = resp.text

        # Only look for more links from the homepage itself — avoids an
        # unbounded breadth-first crawl of an entire site when we only
        # ever want a handful of relevant pages.
        if url != website_url:
            continue

        for match in re.finditer(r'<a\s[^>]*href=["\']([^"\'#]+)["\'][^>]*>(.*?)</a>', resp.text, re.IGNORECASE | re.DOTALL):
            href, link_text = match.group(1), re.sub(r"<[^>]+>", "", match.group(2))
            if not _is_relevant_link(href, link_text):
                continue
            full_url = urljoin(website_url, href)
            if _same_site(base_netloc, full_url) and full_url not in visited:
                to_visit.append(full_url)

        # Real discovered links (above) are queued first and win the
        # limited page budget — these guesses are a lower-confidence
        # fallback for JS-rendered nav the real-link scan can't see.
        for guess_path in COMMON_BOOKING_PATH_GUESSES:
            guess_url = urljoin(website_url, guess_path)
            if guess_url not in visited and guess_url not in to_visit:
                to_visit.append(guess_url)

        time.sleep(0.2)

    if not pages:
        return {"status": "Failed", "pages": {}, "crawled_at": crawled_at}
    return {"status": "Success", "pages": pages, "crawled_at": crawled_at}


# --- Booking detection ---

# Reuses the exact same known-booking-platform domain family already
# maintained in lead_discovery_and_evaluation_Contactsearch.py's
# EMAIL_DENYLIST_SNIPPETS (those domains show up there specifically
# because leads' booking-confirmation emails leak them) — one list, two
# uses, instead of a second copy drifting out of sync with the first.
BOOKING_PLATFORM_DOMAINS = {
    "fresha.com": "Fresha",
    "calendly.com": "Calendly",
    "vagaro.com": "Vagaro",
    "glossgenius.com": "GlossGenius",
    "acuityscheduling.com": "Acuity Scheduling",
    "squareup.com/appointments": "Square Appointments",
    "square.site": "Square Appointments",
    "booksy.com": "Booksy",
    "schedulicity.com": "Schedulicity",
    "mindbodyonline.com": "Mindbody",
    "setmore.com": "Setmore",
    "styleseat.com": "StyleSeat",
    # Found live 2026-08-31: a real qualifying lead (barbershopoldtown.com)
    # had a genuine booking link to Squire, a barbershop-specific booking
    # platform, that this list simply didn't cover.
    "getsquire.com": "Squire",
}

# Website-builder-native booking features (Wix Bookings, etc.) aren't a
# link to any external domain at all — the builder renders its own
# booking UI client-side/server-rendered from its own asset host, so
# BOOKING_PLATFORM_DOMAINS' href/src-only check structurally can't see
# them. Found live 2026-08-31: a real qualifying Wix-built lead
# (ogsbarbershop.com) had a genuine, fully-functional native Wix
# Bookings widget that was invisible to the domain-link check — verified
# by fetching the real page and finding Wix's own internal service name
# for the booking widget bundle in its asset URLs. Matched as a raw
# substring anywhere in the page (not restricted to href/src), since the
# marker lives inside an inline JSON config blob, not an HTML attribute.
NATIVE_BOOKING_MARKERS = {
    "bookings-widget-viewer": "Wix Bookings",
    # Found live 2026-09-01: a real qualifying lead had a genuine,
    # fully-configured self-hosted booking page (a real /booking nav
    # link, an actual "Book an Appointment" button, real deposit/
    # cancellation-policy config) on franpos.com — a booking platform
    # not covered by BOOKING_PLATFORM_DOMAINS, and unreachable by that
    # check anyway since the booking page lives on the business's own
    # domain (a relative href, not an external one). enableAppointments
    # is the real per-business config flag confirming it's actually on,
    # not just a link/nav-item that happens to say "booking".
    "\"enableAppointments\":true": "Online Booking",
}

# Square's own website builder (square.site) exposes real, active
# Appointments config with a "type" field whose value contains
# "appointment" — but the exact shape varies per surface: a booking
# BUTTON uses {"type":"squareAppointment",...}, a nav MENU ITEM uses
# {"type":"appointments",...}. Found live 2026-09-01, checking two more
# real qualifying leads by hand: the first-tried single exact marker
# (Square's APPOINTMENTS_SET_UP flag) missed both — one had the button
# shape, one had the nav-item shape, neither had that flag anywhere on
# the page. This regex catches both shapes (and any future minor
# variant) with one rule. Verified live against 5 real sites: matches
# all 3 with genuine active booking, zero false positives on the 2
# without.
NATIVE_BOOKING_TYPE_RE = re.compile(r'"type"\s*:\s*"[^"]*appointment', re.IGNORECASE)

BOOKING_CTA_TEXT_RE = re.compile(r"book\s*(now|an?\s*appointment|online)|schedule\s*(now|an?\s*appointment)", re.IGNORECASE)


def detect_booking(pages):
    """Returns (status, provider, source_url). Deliberately does NOT treat
    the word "appointment" alone as evidence — only a link/embed pointing
    at a known booking platform domain, or a known website-builder-native
    booking widget, counts as "Detected". A page that merely mentions
    booking without a real integration is "Not Detected", not "Detected"
    — the spec is explicit that a generic contact form or the mere word
    "appointment" must not be conflated with a real booking system."""
    if not pages:
        return "Booking Unknown", None, None

    for url, html in pages.items():
        # Real integration evidence: any href/src pointing at a known
        # booking platform, wherever it lives — a button, a link, an
        # iframe embed, or a script tag loading their widget.
        for match in re.finditer(r'(?:href|src)=["\']([^"\']+)["\']', html, re.IGNORECASE):
            link = match.group(1)
            for domain, provider in BOOKING_PLATFORM_DOMAINS.items():
                if domain in link.lower():
                    return "Booking Detected", provider, link

        # Native builder-hosted booking features — no href/src to check,
        # the marker just has to appear anywhere on the page.
        for marker, provider in NATIVE_BOOKING_MARKERS.items():
            if marker in html:
                return "Booking Detected", provider, url

        if NATIVE_BOOKING_TYPE_RE.search(html):
            return "Booking Detected", "Square Appointments", url

    return "Booking Not Detected", None, None


# --- Call CTA detection ---

CALL_CTA_TEXT_RE = re.compile(r"call\s*(us|now|today)\b", re.IGNORECASE)
PHONE_TEXT_RE = re.compile(r'(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}')


def detect_call_cta(pages):
    """Returns (status, has_tel_link, phone_source). A visible phone
    number alone is NOT the same as a usable call CTA (the spec is
    explicit about this) — only a real tel: link counts as "Detected".
    A page with just plain-text digits and no tel: link and no
    call-oriented CTA text is "Not Detected", even though a number is
    technically present, since there's no evidence it's actually
    clickable/tappable."""
    if not pages:
        return "Call CTA Unknown", False, None

    has_tel_link = False
    has_call_text = False
    for html in pages.values():
        if re.search(r'href=["\']tel:', html, re.IGNORECASE):
            has_tel_link = True
        if CALL_CTA_TEXT_RE.search(html):
            has_call_text = True

    if has_tel_link:
        return "Call CTA Detected", True, "tel: link"
    if has_call_text:
        # Call-oriented CTA text present but no real tel: link behind it —
        # a weaker signal than a real tel: link, but still real evidence
        # of an intended (if not fully wired) call path, so this counts
        # as detected rather than "not detected" outright.
        return "Call CTA Detected", False, "call CTA text, no tel: link"

    return "Call CTA Not Detected", False, None


def crawl_and_qualify(website_url):
    """Runs the full crawl + both remaining detectors (booking, call CTA)
    for one lead's website. CMS detection was removed entirely from this
    pipeline (Cyril's call, 2026-08-31) — CMS presence stopped being a
    useful signal once most small-business sites turned out to be built
    on Wix/Squarespace regardless of how sophisticated the business's
    actual web presence is. Returns a flat dict ready to merge into a
    lead's qualification record.

    "Skipped" (no website URL at all) is deliberately NOT the same as
    "Failed" (a real URL that couldn't be reached) — a business with no
    website at all confidently has no booking system running on it
    (there's no site for one to exist on), so this counts as a real "Not
    Detected", not "Unknown". "Failed" (site exists but unreachable right
    now) stays genuinely Unknown — there could be a real booking system
    we just couldn't see this time. Found live 2026-08-30: without this
    distinction, a no-website lead would sit in "Unknown" and get
    skipped/retried forever instead of correctly qualifying (no booking
    system is the strongest possible qualifying signal)."""
    crawl_result = crawl_website(website_url)
    pages = crawl_result["pages"]

    if crawl_result["status"] == "Skipped":
        return {
            "crawl_status": "Skipped", "crawled_at": crawl_result["crawled_at"],
            "pages_crawled": [],
            "booking_status": "Booking Not Detected", "booking_provider": None, "booking_source": None,
            "call_cta_status": "Call CTA Not Detected", "call_cta_has_tel_link": False, "call_cta_evidence": None,
        }

    booking_status, booking_provider, booking_source = detect_booking(pages)
    call_status, has_tel_link, call_evidence = detect_call_cta(pages)

    return {
        "crawl_status": crawl_result["status"],
        "crawled_at": crawl_result["crawled_at"],
        "pages_crawled": list(pages.keys()),
        "booking_status": booking_status,
        "booking_provider": booking_provider,
        "booking_source": booking_source,
        "call_cta_status": call_status,
        "call_cta_has_tel_link": has_tel_link,
        "call_cta_evidence": call_evidence,
    }
