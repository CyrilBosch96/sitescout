#!/usr/bin/env python3
"""
Lead Discovery (+ competitor identification), Website Evaluation (Box 1 ONLY),
and Outreach Generation (contact-finding + content drafting) — combined.
SiteScout

Full consolidated version — incorporates: Place ID cache, real PageSpeed
data, timezone detection, exhausted-flag fix, thinking-mode fix, and the
final locked story-format email content.
"""

import sys
import os
import re
import time
import json
import requests
import dns.resolver
from bs4 import BeautifulSoup
from urllib.parse import urlparse

import hp_env
import hp_config
import hp_llm
import hp_template
import hp_lock
import hp_api_budget
import geo_grid
import website_crawler
import opportunity_scoring

# Zero-cost pre-send check (2026-08-27): confirm a candidate email's domain
# actually has mail servers configured at all, before it ever enters the
# CRM — cheaper and less risky than an SMTP mailbox probe (which some
# receiving servers rate-limit or flag as abuse-adjacent), at the cost of
# not catching a real domain whose specific mailbox doesn't exist, or a
# domain whose mail server exists but times out (like the real Ford's
# Barber Shop Tulsa bounce this was built in response to — bounce_check.py
# is the safety net for that case; this is the cheap gate in front of it).
DNS_RESOLVER = dns.resolver.Resolver()
DNS_RESOLVER.timeout = 5
DNS_RESOLVER.lifetime = 5


def has_valid_mx_record(email):
    """True if the email's domain resolves to at least one MX record.
    Fails open on any DNS error other than a definitive "no mail here"
    answer (NXDOMAIN/NoAnswer) — a transient resolver timeout or network
    blip shouldn't discard an otherwise-good lead."""
    if not email or "@" not in email:
        return False
    domain = email.split("@")[-1]
    try:
        answers = DNS_RESOLVER.resolve(domain, "MX")
        return len(answers) > 0
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
        return False
    except Exception:
        return True

EMAIL_TEMPLATES_DIR = os.path.join(hp_env.PROJECT_ROOT, "email_templates")

RANK_THRESHOLD = hp_config.COMPETITOR_RANK_THRESHOLD
REVIEW_DELTA_THRESHOLD = hp_config.COMPETITOR_REVIEW_DELTA_THRESHOLD
MIN_QUALIFYING_LEADS = hp_config.LEAD_DISCOVERY_MIN_QUALIFYING_LEADS
MAX_TOTAL_CHECKED = hp_config.LEAD_DISCOVERY_MAX_TOTAL_CHECKED

GOOGLE_API_KEY = os.environ.get("GOOGLE_PLACES_API_KEY")
NOTION_API_KEY = hp_env.NOTION_API_KEY
NOTION_DATA_SOURCE_ID = hp_env.NOTION_DATA_SOURCE_ID
NOTION_DATABASE_ID = hp_env.NOTION_DATABASE_ID

CACHE_FILE = os.path.join(hp_env.PROJECT_ROOT, "checked_places_cache.json")
STATE_FILE = os.path.join(hp_env.PROJECT_ROOT, "state.json")

EMAIL_DENYLIST_SNIPPETS = [
    "noreply", "no-reply", "example.com", "yourdomain", "domain.com",
    "email.com", "sentry", "wixpress", "godaddy.com", "schema.org",
    ".png", ".jpg", ".gif", ".svg", "wordpress.com",
    "booksy.com", "squareup.com", "schedulicity.com", "vagaro.com",
    "fresha.com", "styleseat.com", "glossgenius.com", "mindbodyonline.com",
    "setmore.com", "calendly.com", "mystore.com", "yourwebsite.com",
    "mysite.com",
]

# Local-parts real businesses essentially never use as their actual contact
# address — a placeholder giveaway independent of domain. Exact match only,
# not a substring check, so "forexample@realbiz.com" isn't false-flagged.
# Found live (Sprint 14): "example@mysite.com" slipped through the domain
# denylist above since the domain alone wasn't on it.
JUNK_LOCAL_PARTS = {"example"}

# Broader net for the whole "your{noun}.tld" family of website-builder
# template placeholder domains (found live: info@yourwebsite.com slipped
# through the literal snippet list above before "yourwebsite.com" was
# added to it) — catches yoursite.com, yourbusiness.com, etc. too.
GENERIC_PLACEHOLDER_DOMAIN_RE = re.compile(
    r"^your(website|domain|site|business|company|store|shop)\.[a-z.]+$", re.IGNORECASE
)

STATE_TO_TIMEZONE = {
    "CA": "PT", "NV": "PT", "OR": "PT", "WA": "PT", "AK": "PT", "HI": "PT",
    "AZ": "MT", "CO": "MT", "ID": "MT", "MT": "MT", "NM": "MT", "UT": "MT", "WY": "MT",
    "AL": "CT", "AR": "CT", "IL": "CT", "IA": "CT", "KS": "CT", "LA": "CT", "MN": "CT",
    "MS": "CT", "MO": "CT", "ND": "CT", "NE": "CT", "OK": "CT", "SD": "CT", "TX": "CT", "WI": "CT", "TN": "CT",
    "CT": "ET", "DE": "ET", "FL": "ET", "GA": "ET", "IN": "ET", "KY": "ET", "ME": "ET",
    "MD": "ET", "MA": "ET", "MI": "ET", "NH": "ET", "NJ": "ET", "NY": "ET", "NC": "ET",
    "OH": "ET", "PA": "ET", "RI": "ET", "SC": "ET", "VT": "ET", "VA": "ET", "WV": "ET", "DC": "ET",
}

if not GOOGLE_API_KEY:
    print("ERROR: Missing GOOGLE_PLACES_API_KEY in environment.")
    sys.exit(1)


def get_timezone_from_address(formatted_address):
    if not formatted_address:
        return None
    match = re.search(r",\s*([A-Z]{2})\s+\d{5}", formatted_address)
    if not match:
        return None
    return STATE_TO_TIMEZONE.get(match.group(1))


def load_cache():
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE) as f:
            return json.load(f)
    return {}


def save_cache(cache):
    os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
    with open(CACHE_FILE, "w") as f:
        json.dump(cache, f, indent=2)


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"active_niche": None, "active_location": None, "exhausted": False, "last_asked_at": None}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def mark_exhausted_if_needed(qualifying_found, niche, location):
    if qualifying_found < MIN_QUALIFYING_LEADS:
        state = load_state()
        if state.get("active_niche") == niche and state.get("active_location") == location:
            state["exhausted"] = True
            save_state(state)
            print(f"Marked '{niche}' in '{location}' as exhausted in state.json.")
        else:
            print(f"Note: state.json's active job ('{state.get('active_niche')}' in '{state.get('active_location')}') "
                  f"doesn't match what was just searched ('{niche}' in '{location}') — not updating state.json.")


def determine_skip_reason(place, existing_names, seen_names_this_run, cache):
    """
    Extracted from main()'s loop so the cache/dedup skip decision is
    unit-testable without invoking the full discovery pipeline (Places
    search, PageSpeed, contact scraping, content generation). Behavior
    identical to what was previously inline.

    Returns "duplicate", "cached", or None (don't skip).
    """
    name = place.get("displayName", {}).get("text", "Unknown")
    place_id = place.get("id", name)
    if name in existing_names or name in seen_names_this_run:
        return "duplicate"
    if place_id in cache:
        return "cached"
    return None


class PlacesBudgetExhausted(Exception):
    """Raised instead of making a real Places API call once the hard
    monthly call budget (Cyril's explicit call, 2026-08-31: never reach
    Google's real 5,000/month free-tier boundary) has been reached.
    Deliberately distinct from a genuine "no more results" — a caller
    catching this must never mark a real market as exhausted just
    because this month's own call budget ran out; the correct response
    is to stop for the rest of the calendar month and let the same
    niche/location retry once the budget resets."""
    pass


def search_places_page(niche, location, page_token=None, lat=None, lng=None, radius_meters=None):
    """lat/lng/radius_meters (all required together) restrict the search
    to a real geographic cell (converted to a rectangle, the only shape
    Text Search's locationRestriction actually accepts — see
    geo_grid.cell_to_rectangle()) instead of Google's own free-text
    interpretation of `location` — used by the grid-based search
    (geo_grid.py) to get real coverage of a large market past Text
    Search's ~60-result-per-query cap. Every real call is checked
    against, and counted against, the hard monthly Places API budget
    (hp_api_budget.py) — see PlacesBudgetExhausted."""
    if not hp_api_budget.can_make_call():
        raise PlacesBudgetExhausted()

    url = "https://places.googleapis.com/v1/places:searchText"
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": GOOGLE_API_KEY,
        "X-Goog-FieldMask": "places.id,places.displayName,places.formattedAddress,"
                             "places.rating,places.userRatingCount,"
                             "places.websiteUri,places.nationalPhoneNumber,"
                             "places.regularOpeningHours,"
                             "nextPageToken",
    }
    if lat is not None and lng is not None:
        # A grid-cell search: the rectangle itself supplies the geography,
        # so the text query stays to just the niche — combining it with
        # "in {location}" here would fight the rectangle's own
        # restriction. Found live 2026-08-31: locationRestriction only
        # accepts a "rectangle" shape for Text Search, not "circle" (a
        # 400 INVALID_ARGUMENT otherwise) — see geo_grid.cell_to_rectangle().
        body = {
            "textQuery": niche, "pageSize": 20,
            "locationRestriction": {"rectangle": geo_grid.cell_to_rectangle(lat, lng, radius_meters)},
        }
    else:
        body = {"textQuery": f"{niche} in {location}", "pageSize": 20}
    if page_token:
        body["pageToken"] = page_token
    resp = requests.post(url, headers=headers, json=body)
    resp.raise_for_status()
    hp_api_budget.record_call()
    data = resp.json()
    return data.get("places", []), data.get("nextPageToken")


def discover_places(niche, location, stop_reason):
    """Yields every place found for one niche/location, one at a time.
    Real market coverage past Text Search's own ~60-result cap comes
    from subdividing the city into a grid of search circles (geo_grid.py)
    — geocodes `location` once, then runs a full paginated search per
    grid cell. Falls back to a single ungridded broad query (the
    original pre-2026-08-31 behavior) if geocoding fails for any reason,
    so a geocoding miss degrades gracefully instead of blocking Discovery
    entirely.

    `stop_reason` is a caller-owned dict this function writes to before
    returning, so main() can tell a genuinely exhausted market apart
    from hitting this month's hard Places API budget — the two must
    never be treated the same way (see PlacesBudgetExhausted).

    Yields (place, page_places) pairs, not just place — find_competitor()
    ranks a lead against the other businesses returned in the same page,
    so callers need that page's full list alongside each place, the same
    context the original single-broad-query loop always had."""
    bounds = geo_grid.get_city_bounds(location, GOOGLE_API_KEY)
    # None (no lat/lng restriction) means "run the original single
    # broad-query search" — the grid loop below treats this exactly like
    # a one-cell grid.
    grid_points = geo_grid.generate_grid_points(bounds, hp_config.DISCOVERY_GRID_CELL_RADIUS_METERS) if bounds else [None]

    for point in grid_points:
        lat, lng = point if point else (None, None)
        page_token = None
        while True:
            try:
                if lat is not None:
                    places, next_token = search_places_page(
                        niche, location, page_token, lat=lat, lng=lng,
                        radius_meters=hp_config.DISCOVERY_GRID_CELL_RADIUS_METERS,
                    )
                else:
                    places, next_token = search_places_page(niche, location, page_token)
            except PlacesBudgetExhausted:
                print("Places API monthly call budget reached — stopping for the rest of the month (not marking this market exhausted).")
                stop_reason["reason"] = "budget_exhausted"
                return

            if not places:
                break
            for place in places:
                yield place, places
            if not next_token:
                break
            page_token = next_token
            time.sleep(2)

    stop_reason["reason"] = "market_exhausted"


def find_competitor(all_places, lead_index):
    lead = all_places[lead_index]
    lead_reviews = lead.get("userRatingCount", 0)
    for i in range(0, lead_index):
        candidate = all_places[i]
        candidate_reviews = candidate.get("userRatingCount", 0)
        rank_gap = lead_index - i
        if rank_gap >= RANK_THRESHOLD and abs(candidate_reviews - lead_reviews) <= REVIEW_DELTA_THRESHOLD:
            return {
                "name": candidate.get("displayName", {}).get("text", ""),
                "rank": i + 1,
                "reviews": candidate_reviews,
                "delta": candidate_reviews - lead_reviews,
            }
    return None


def get_pagespeed_data(website_url):
    if not website_url:
        return None
    api_url = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"
    params = [
        ("url", website_url), ("key", GOOGLE_API_KEY), ("strategy", "mobile"),
        ("category", "performance"), ("category", "seo"),
        ("category", "accessibility"), ("category", "best-practices"),
    ]
    try:
        resp = requests.get(api_url, params=params, timeout=30)
        if resp.status_code != 200:
            return None
        lh = resp.json()["lighthouseResult"]
        categories = lh.get("categories", {})
        audits = lh.get("audits", {})
        scores, audit_refs = {}, {}
        for key in ("performance", "seo", "accessibility", "best-practices"):
            if key in categories and categories[key].get("score") is not None:
                scores[key] = categories[key]["score"]
                audit_refs[key] = categories[key].get("auditRefs", [])
        if not scores:
            return None
        return {"scores": scores, "audits": audits, "audit_refs": audit_refs}
    except Exception:
        return None


def get_top_findings(category_key, pdata, max_findings=2):
    refs = pdata["audit_refs"].get(category_key, [])
    audits = pdata["audits"]
    scored = []
    for ref in refs:
        audit = audits.get(ref.get("id"))
        if not audit:
            continue
        score = audit.get("score")
        if score is None or score >= 0.9:
            continue
        title = audit.get("title", "")
        display = audit.get("displayValue", "")
        scored.append((ref.get("weight", 0), f"{title}: {display}" if display else title))
    scored.sort(key=lambda x: -x[0])
    return [text for _, text in scored[:max_findings]]


def get_real_load_time(pdata):
    lcp = pdata["audits"].get("largest-contentful-paint")
    if lcp and lcp.get("displayValue"):
        return lcp["displayValue"]
    return None


MIN_SIGNALS_FAILED = hp_config.MIN_SIGNALS_FAILED
SAME_SITE_RECHECK_DELAY = hp_config.PAGESPEED_RECHECK_DELAY_SECONDS

PERFORMANCE_THRESHOLD = hp_config.PAGESPEED_PERFORMANCE_THRESHOLD
SEO_THRESHOLD = hp_config.PAGESPEED_SEO_THRESHOLD
ACCESSIBILITY_THRESHOLD = hp_config.PAGESPEED_ACCESSIBILITY_THRESHOLD
BEST_PRACTICES_THRESHOLD = hp_config.PAGESPEED_BEST_PRACTICES_THRESHOLD
LCP_THRESHOLD_MS = hp_config.CORE_WEB_VITALS_LCP_THRESHOLD_MS
CLS_THRESHOLD = hp_config.CORE_WEB_VITALS_CLS_THRESHOLD
TBT_THRESHOLD_MS = hp_config.CORE_WEB_VITALS_TBT_THRESHOLD_MS


def check_signals(pdata):
    """Checks all 7 signals (4 category scores + 3 individual Core Web Vitals metrics).
    Returns (total_failed_count, {"speed": [...], "seo": [...], "quality": [...]})."""
    scores = pdata["scores"]
    audits = pdata["audits"]
    failed = {"speed": [], "seo": [], "quality": []}

    if scores.get("performance", 1) < PERFORMANCE_THRESHOLD:
        failed["speed"].append("performance_score")
    if scores.get("seo", 1) < SEO_THRESHOLD:
        failed["seo"].append("seo_score")
    if scores.get("accessibility", 1) < ACCESSIBILITY_THRESHOLD:
        failed["quality"].append("accessibility_score")
    if scores.get("best-practices", 1) < BEST_PRACTICES_THRESHOLD:
        failed["quality"].append("best_practices_score")

    lcp = audits.get("largest-contentful-paint", {}).get("numericValue")
    if lcp is not None and lcp > LCP_THRESHOLD_MS:
        failed["speed"].append("lcp")

    cls = audits.get("cumulative-layout-shift", {}).get("numericValue")
    if cls is not None and cls > CLS_THRESHOLD:
        failed["quality"].append("cls")

    tbt = audits.get("total-blocking-time", {}).get("numericValue")
    if tbt is not None and tbt > TBT_THRESHOLD_MS:
        failed["speed"].append("tbt")

    total_failed = len(failed["speed"]) + len(failed["seo"]) + len(failed["quality"])
    return total_failed, failed


def direct_site_reachable(url):
    try:
        resp = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        return resp.status_code == 200 and len(resp.text) > 200
    except Exception:
        return False


def median_pdata_for_check(pdata_list):
    all_score_keys = set()
    for pdata in pdata_list:
        all_score_keys.update(pdata["scores"].keys())

    median_scores = {}
    for key in all_score_keys:
        values = [pdata["scores"].get(key) for pdata in pdata_list if pdata["scores"].get(key) is not None]
        if values:
            median_scores[key] = sorted(values)[len(values) // 2]

    median_audits = {}
    for audit_id in ("largest-contentful-paint", "cumulative-layout-shift", "total-blocking-time"):
        values = [pdata["audits"].get(audit_id, {}).get("numericValue") for pdata in pdata_list]
        values = [v for v in values if v is not None]
        if values:
            median_audits[audit_id] = {"numericValue": sorted(values)[len(values) // 2]}

    return {"scores": median_scores, "audits": median_audits, "audit_refs": pdata_list[0]["audit_refs"]}


def classify_lead(place):
    website = place.get("websiteUri")
    if not website:
        return False, None, None

    pdata1 = get_pagespeed_data(website)

    if pdata1 is None:
        if direct_site_reachable(website):
            return False, None, None
        return True, None, "broken"

    # No preliminary screen on run 1 alone — every lead gets the full 4-run
    # process, so the final decision is never based on a single measurement
    # (the whole point of this sprint's User Story), and there's no per-call
    # cost on PageSpeed Insights to justify skipping runs to save money.
    more_runs = [pdata1]
    for _ in range(3):
        time.sleep(SAME_SITE_RECHECK_DELAY)
        pdata_next = get_pagespeed_data(website)
        if pdata_next is None:
            # Retry once before excluding this run — a transient failure
            # shouldn't cost a data point out of the median.
            time.sleep(SAME_SITE_RECHECK_DELAY)
            pdata_next = get_pagespeed_data(website)
        if pdata_next is not None:
            more_runs.append(pdata_next)

    if len(more_runs) < 3:
        return False, pdata1, None

    kept_runs = more_runs[1:]
    median_pdata = median_pdata_for_check(kept_runs)

    total_failed_median, failed_median = check_signals(median_pdata)
    if total_failed_median < MIN_SIGNALS_FAILED:
        return False, median_pdata, None

    if len(failed_median["speed"]) >= len(failed_median["quality"]) and len(failed_median["speed"]) >= len(failed_median["seo"]):
        reason = "slow"
    elif len(failed_median["quality"]) >= len(failed_median["seo"]):
        reason = "quality"
    else:
        reason = "seo"
    # Return the median data, not pdata1 — the median is what actually
    # determined this classification, and it's what downstream copy
    # generation should quote as evidence, not a single (possibly fluky) run.
    return True, median_pdata, reason


def is_tracking_id(candidate):
    local_part = candidate.split("@")[0]
    return bool(re.match(r"^[a-f0-9]{20,}$", local_part, re.IGNORECASE))


def is_generic_placeholder_domain(candidate):
    domain = candidate.split("@")[-1] if "@" in candidate else candidate
    return bool(GENERIC_PLACEHOLDER_DOMAIN_RE.match(domain))


def validate_email(candidate):
    """True if candidate is a plausible business contact email, False if it
    matches the junk-platform denylist, looks like a tracking ID local-part,
    is a website-builder template placeholder domain (your{noun}.tld), or
    has a local-part real businesses don't actually use (e.g. "example")."""
    if not candidate:
        return False
    if any(bad in candidate.lower() for bad in EMAIL_DENYLIST_SNIPPETS):
        return False
    if is_tracking_id(candidate):
        return False
    if is_generic_placeholder_domain(candidate):
        return False
    local_part = candidate.split("@")[0].lower()
    if local_part in JUNK_LOCAL_PARTS:
        return False
    return True


def extract_email_from_html(html):
    mailto_match = re.search(r'mailto:([\w.+-]+@[\w-]+\.[\w.-]+)', html)
    if mailto_match:
        candidate = mailto_match.group(1)
        if validate_email(candidate) and has_valid_mx_record(candidate):
            return candidate
    plain_matches = re.findall(r'[\w.+-]+@[\w-]+\.[a-zA-Z]{2,}', html)
    for candidate in plain_matches:
        if validate_email(candidate) and has_valid_mx_record(candidate):
            return candidate
    return None


def extract_social_from_html(html):
    social_match = re.search(r'(https?://(?:www\.)?(?:instagram\.com|facebook\.com)/[\w.\-/]+)', html)
    return social_match.group(1) if social_match else None


def find_contact_info(website_url):
    result = {"email": None, "social": None}
    if not website_url:
        return result
    try:
        resp = requests.get(website_url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        if resp.status_code == 200:
            html = resp.text
            result["email"] = extract_email_from_html(html)
            result["social"] = extract_social_from_html(html)
    except Exception:
        pass
    if result["email"]:
        return result
    parsed = urlparse(website_url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    for path in ("/contact", "/contact-us", "/contactus", "/pages/contact"):
        try:
            resp = requests.get(base + path, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
            if resp.status_code == 200:
                html = resp.text
                found_email = extract_email_from_html(html)
                if found_email:
                    result["email"] = found_email
                if not result["social"]:
                    result["social"] = extract_social_from_html(html)
                if result["email"]:
                    break
        except Exception:
            continue
    return result


# Real material for Cyril's ghost-site content rewrite step (2026-08-28):
# once he shares the target format, this raw snapshot gets rewritten by an
# LLM into that shape and used to build the actual ghost site, replacing
# the current Google-Places-derived data + AI-generated Hero Line. Kept
# deliberately raw/uncurated here — the rewrite step is a separate,
# not-yet-built piece, this just needs to capture genuine signal about the
# business from their own site.
CONTENT_SNAPSHOT_MAX_CHARS = 2000
# Notion's rich_text length limit is measured in UTF-16 code units, not
# Python characters. Found live 2026-09-02 (Chandler, AZ run): a plain
# Python [:2000] slice can still land over Notion's 2000-unit cap when
# the scraped page text contains astral-plane characters (emoji, some
# symbols), which encode as two UTF-16 units each — the Notion write
# then failed outright with a validation_error, discarding an otherwise-
# qualifying lead entirely rather than just losing a bit of snapshot text.


def _truncate_to_utf16_units(text, max_units):
    if len(text.encode("utf-16-le")) // 2 <= max_units:
        return text
    truncated = text[:max_units]
    while len(truncated.encode("utf-16-le")) // 2 > max_units:
        truncated = truncated[:-1]
    return truncated


CONTENT_SNAPSHOT_PATHS = (
    "/about", "/about-us", "/aboutus",
    "/services", "/our-services",
    "/contact", "/contact-us", "/contactus", "/pages/contact",
)


def extract_visible_text(html):
    """Real rendered-page text a visitor would actually see — strips
    script/style/nav/footer noise rather than regex-stripping tags, since
    <script>/<style> content would otherwise leak raw JS/CSS into the
    snapshot."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "noscript"]):
        tag.decompose()
    text = soup.get_text(separator=" ", strip=True)
    return re.sub(r"\s+", " ", text).strip()


def scrape_website_content_snapshot(website_url):
    """Homepage plus common About/Services/Contact paths — broader than
    find_contact_info()'s own page fetches (which stop as soon as an email
    is found, since that's all it needs), because this needs real content
    signal even from pages find_contact_info() never had to visit.
    Best-effort only: any page that fails to load is silently skipped,
    same as find_contact_info() — a slow/broken lead site must never block
    lead qualification."""
    if not website_url:
        return None
    pieces = []
    try:
        resp = requests.get(website_url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        if resp.status_code == 200:
            text = extract_visible_text(resp.text)
            if text:
                pieces.append(text)
    except Exception:
        pass

    parsed = urlparse(website_url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    for path in CONTENT_SNAPSHOT_PATHS:
        try:
            resp = requests.get(base + path, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
            if resp.status_code == 200:
                text = extract_visible_text(resp.text)
                if text:
                    pieces.append(text)
        except Exception:
            continue

    if not pieces:
        return None
    return _truncate_to_utf16_units(" | ".join(pieces), CONTENT_SNAPSHOT_MAX_CHARS)


# Enhanced Website-Based Lead Qualification epic (2026-08-30) — the
# crawler-detected gap's evidence/consequence, keyed by
# opportunity_scoring.determine_primary_website_gap()'s return value.
# Kept as plain fragments (not full prompts) so they can be blended with
# an existing PageSpeed-based technical reason below.
GAP_EVIDENCE = {
    "no_booking": "doesn't offer any way to book an appointment online",
    "no_call_cta": "has no clickable way to call directly from a phone",
}
GAP_CONSEQUENCE = {
    "no_booking": "customers who'd rather book online than pick up the phone just go to a competitor who lets them",
    "no_call_cta": "mobile visitors have to manually dial instead of tapping to call",
}


def generate_middle_continuation(business_name, niche, competitor, pdata, reason, gap_reason=None):
    """Generates a grammatical CONTINUATION of the fixed lead-in "Right now, your website " —
    not a standalone sentence. Grounded in real data, ends with the concrete
    negative consequence for the business. Never uses em dashes.

    reason is the old PageSpeed-based technical issue (broken/slow/seo/
    quality), or None if PageSpeed found nothing wrong. gap_reason is the
    crawler-detected missing-capability signal (no_booking/no_call_cta)
    from the Enhanced Website-Based Lead Qualification epic, or None.
    Cyril's call (2026-08-30): when both are present, mention both in one
    blended sentence rather than picking just one — a genuinely slow site
    AND a missing booking system are both real."""

    no_em_dash_rule = "Do NOT use em dashes (the — character) anywhere in your output. Write like a real person texting, using periods and commas only."

    technical_evidence = None
    technical_consequence = None
    if reason == "broken":
        technical_evidence = "is completely down or failing to load at all"
        technical_consequence = f'people searching for "{niche} near me" hit an error and assume the business is closed, so they go elsewhere'
    elif reason == "slow":
        real_time = get_real_load_time(pdata) or "several seconds"
        findings = get_top_findings("performance", pdata, max_findings=1)
        evidence = findings[0] if findings else "a slow load time"
        technical_evidence = f"has a real measured load time of {real_time}, and this specific issue: {evidence}"
        technical_consequence = "this is turning visitors away before they see what the business offers"
    elif reason == "seo":
        findings = get_top_findings("seo", pdata, max_findings=2)
        evidence = "; ".join(findings) if findings else "weak search visibility"
        technical_evidence = f"has these real SEO problems: {evidence}"
        technical_consequence = "most people searching nearby never even see the business show up"
    elif reason == "quality":
        cat = "accessibility" if pdata["scores"].get("accessibility", 1) < pdata["scores"].get("best-practices", 1) else "best-practices"
        findings = get_top_findings(cat, pdata, max_findings=2)
        evidence = "; ".join(findings) if findings else "some outdated design and accessibility issues"
        technical_evidence = f"has these real issues: {evidence}"
        technical_consequence = "visitors get a dated, untrustworthy first impression and leave"

    gap_evidence = GAP_EVIDENCE.get(gap_reason)
    gap_consequence = GAP_CONSEQUENCE.get(gap_reason)

    if technical_evidence and gap_evidence:
        prompt = f'''Complete this sentence naturally: "Right now, your website ___"
The website for "{business_name}", a {niche} business, {technical_evidence}. It also {gap_evidence}.
Write ONLY the completion (do not repeat "Right now, your website"), roughly 20-35 words, one or two sentences,
mentioning both real issues, ending with the real consequence: {technical_consequence}, and {gap_consequence}.
Casual, plain, human tone. {no_em_dash_rule}
Output ONLY the sentence completion. No greeting, no quotes, no markdown.'''
    elif gap_evidence:
        prompt = f'''Complete this sentence naturally: "Right now, your website ___"
The website for "{business_name}", a {niche} business, {gap_evidence}.
Write ONLY the completion (do not repeat "Right now, your website"), roughly 15-25 words, one sentence,
ending with the real consequence: {gap_consequence}.
Casual, plain, human tone. {no_em_dash_rule}
Output ONLY the sentence completion. No greeting, no quotes, no markdown.'''
    elif technical_evidence:
        prompt = f'''Complete this sentence naturally: "Right now, your website ___"
The website for "{business_name}", a {niche} business, {technical_evidence}.
Write ONLY the completion (do not repeat "Right now, your website"), roughly 15-25 words, one sentence,
ending with the real consequence: {technical_consequence}.
Casual, plain, human tone. {no_em_dash_rule}
Output ONLY the sentence completion. No greeting, no quotes, no markdown.'''
    else:
        # Neither a PageSpeed issue nor a crawler gap — nothing real to
        # write about. Shouldn't happen for a genuinely qualifying lead,
        # but never fabricate a reason that isn't backed by real data.
        return None

    text = hp_llm.generate(prompt, timeout=90)
    if text is not None:
        text = text.replace("—", ",")
        # Safety net: strip a duplicated lead-in if the model repeated it despite instructions
        text = re.sub(r"^right now,?\s+your website\s*", "", text, flags=re.IGNORECASE).strip()
        return text
    return None

def generate_hero_line(business_name, niche, location):
    """Short, honest tagline for the business — what Sprint 17's ghost site displays.
    Grounded only in identity (name/niche/location), never a stat or claim not given here."""

    no_em_dash_rule = "Do NOT use em dashes (the — character) anywhere in your output."
    prompt = f"""Write one short, honest tagline (under 12 words) for "{business_name}", a {niche}
business in {location}. It will be displayed on a preview website for this business.
Do NOT invent or state any statistics, numbers, awards, years-in-business, or customer counts —
none were given, so write purely on identity and warmth (what the business does, who it's for).
Plain, human, welcoming tone, like a real local business's own tagline. {no_em_dash_rule}
Output ONLY the tagline. No quotes, no markdown, no explanation."""

    text = hp_llm.generate(prompt, timeout=90)
    if text is not None:
        text = text.replace("—", ",")
        return text.strip('"').strip()
    return None


COLD_SUBJECT_FILES = {
    "broken": "cold_subject_broken.md",
    "slow": "cold_subject_slow.md",
    "slow_no_competitor": "cold_subject_slow_no_competitor.md",
    "seo": "cold_subject_seo.md",
    "quality": "cold_subject_quality.md",
    # Enhanced Website-Based Lead Qualification epic (2026-08-30) — used
    # only when PageSpeed found no technical issue (reason is None) but
    # the crawler found a missing capability. When both a technical
    # reason and a gap_reason are present, the technical reason's own
    # subject file still wins (the body already blends both in that
    # case — see generate_middle_continuation()).
    "no_booking": "cold_subject_no_booking.md",
    "no_call_cta": "cold_subject_no_call_cta.md",
}


def load_email_template(filename):
    """Dashboard-editable content lives in email_templates/*.md, [TOKEN]
    placeholders rendered via hp_template.render() — same pattern
    cold_email.py's load_followup_text() already uses."""
    path = os.path.join(EMAIL_TEMPLATES_DIR, filename)
    with open(path) as f:
        return f.read().rstrip("\n")


def build_email(business_name, competitor, middle_continuation, reason, niche, gap_reason=None):
    """reason may be None (PageSpeed found nothing wrong) when the lead
    qualified purely on a crawler-detected gap_reason — in that case the
    subject comes from the gap's own template instead (Enhanced
    Website-Based Lead Qualification epic, 2026-08-30)."""
    if reason:
        subject_key = "slow_no_competitor" if reason == "slow" and not competitor else reason
    else:
        subject_key = gap_reason
    subject = hp_template.render(
        load_email_template(COLD_SUBJECT_FILES[subject_key]),
        business_name=business_name,
        competitor_name=competitor["name"] if competitor else None,
    )
    body = hp_template.render(
        load_email_template("cold_body.md"),
        niche=niche,
        middle_continuation=middle_continuation,
    )
    return subject, body


def format_business_hours(place):
    """Places API (New) returns hours as one human-readable line per weekday.
    No fabricating a summary — just join what Google actually returned."""
    descriptions = place.get("regularOpeningHours", {}).get("weekdayDescriptions", [])
    if not descriptions:
        return None
    return "; ".join(descriptions)


def get_existing_business_names():
    url = f"https://api.notion.com/v1/data_sources/{NOTION_DATA_SOURCE_ID}/query"
    headers = {
        "Authorization": f"Bearer {NOTION_API_KEY}",
        "Notion-Version": "2025-09-03",
        "Content-Type": "application/json",
    }
    names = set()
    has_more, cursor = True, None
    while has_more:
        body = {"start_cursor": cursor} if cursor else {}
        resp = requests.post(url, headers=headers, json=body)
        if resp.status_code != 200:
            break
        data = resp.json()
        for page in data.get("results", []):
            title = page.get("properties", {}).get("Business Name", {}).get("title", [])
            if title:
                names.add(title[0]["text"]["content"])
        has_more = data.get("has_more", False)
        cursor = data.get("next_cursor")
    return names


def write_to_notion(place, pdata, reason, competitor, contact, subject, body, niche, location, timezone=None, hero_line=None, content_snapshot=None, crawl_result=None, opportunity=None, qualification_reason=None):
    url = "https://api.notion.com/v1/pages"
    headers = {
        "Authorization": f"Bearer {NOTION_API_KEY}",
        "Notion-Version": "2025-09-03",
        "Content-Type": "application/json",
    }
    name = place.get("displayName", {}).get("text", "Unknown")
    properties = {
        "Business Name": {"title": [{"text": {"content": name}}]},
        "Box": {"select": {"name": "Box 1"}},
        "Contact Status": {"select": {"name": "Not Contacted"}},
        "Source Niche/Location": {"rich_text": [{"text": {"content": f"{niche} / {location}"}}]},
        "Email": {"email": contact["email"]},
    }
    if place.get("websiteUri"):
        properties["Website"] = {"url": place.get("websiteUri")}
    if place.get("nationalPhoneNumber"):
        properties["Phone"] = {"phone_number": place.get("nationalPhoneNumber")}
    if contact.get("social"):
        properties["Social Links"] = {"rich_text": [{"text": {"content": contact["social"]}}]}
    if timezone:
        properties["Timezone"] = {"select": {"name": timezone}}
    if place.get("rating") is not None:
        properties["Review Rating"] = {"number": place.get("rating")}
    if place.get("userRatingCount") is not None:
        properties["Review Count"] = {"number": place.get("userRatingCount")}
    hours_str = format_business_hours(place)
    if hours_str:
        properties["Business Hours"] = {"rich_text": [{"text": {"content": hours_str[:2000]}}]}
    # Services List intentionally left unwritten — Places API has no reliable
    # generic "services" field for arbitrary business types, and guessing
    # here would violate the no-fabricated-facts rule. Deferred to Sprint
    # 16/17's niche-template defaults instead of per-lead data.
    if content_snapshot:
        properties["Website Content Snapshot"] = {"rich_text": [{"text": {"content": content_snapshot}}]}

    # Enhanced Website-Based Lead Qualification epic (2026-08-30). CMS
    # Status/Name are no longer written (CMS detection removed entirely,
    # Cyril's call 2026-08-31) — those Notion properties stay in the
    # schema but go unused going forward.
    if crawl_result:
        properties["Crawl Status"] = {"select": {"name": crawl_result["crawl_status"]}}
        properties["Last Crawled"] = {"date": {"start": crawl_result["crawled_at"][:10]}}
        properties["Booking Status"] = {"select": {"name": crawl_result["booking_status"]}}
        if crawl_result.get("booking_provider"):
            properties["Booking Provider"] = {"rich_text": [{"text": {"content": crawl_result["booking_provider"][:200]}}]}
        properties["Call CTA Status"] = {"select": {"name": crawl_result["call_cta_status"]}}
    if opportunity:
        properties["Opportunity Score"] = {"number": opportunity["score"]}
        properties["Opportunity Category"] = {"select": {"name": opportunity["category"]}}
    if qualification_reason:
        properties["Qualification Reason"] = {"rich_text": [{"text": {"content": qualification_reason[:2000]}}]}

    if pdata:
        scores_str = ", ".join(f"{k}: {v}" for k, v in pdata["scores"].items())
        findings = get_top_findings(
            "performance" if reason == "slow" else ("seo" if reason == "seo" else "accessibility"),
            pdata, max_findings=2
        )
        findings_str = " | ".join(findings) if findings else "none"
    else:
        scores_str = "N/A (failed to load)"
        findings_str = "site did not load"

    properties["Website Findings"] = {"rich_text": [{"text": {"content": f"Reason: {reason}. Scores: {scores_str}. Findings: {findings_str}"[:2000]}}]}
    properties["Email Draft"] = {"rich_text": [{"text": {"content": body[:2000]}}]}
    properties["Email Subject"] = {"rich_text": [{"text": {"content": subject[:200]}}]}
    if hero_line:
        properties["Hero Line"] = {"rich_text": [{"text": {"content": hero_line[:200]}}]}

    if competitor:
        properties["Competitor Name"] = {"rich_text": [{"text": {"content": competitor["name"]}}]}
        properties["Competitor Map Pack Rank"] = {"number": competitor["rank"]}
        properties["Competitor Review Count"] = {"number": competitor["reviews"]}
        properties["Review Delta"] = {"number": competitor["delta"]}

    body_payload = {"parent": {"database_id": NOTION_DATABASE_ID}, "properties": properties}
    resp = requests.post(url, headers=headers, json=body_payload)
    if resp.status_code != 200:
        print(f"    Notion error: {resp.status_code} {resp.text[:200]}")
    return resp.status_code == 200


def main():
    # Found live 2026-09-01: this script never acquired the shared lock
    # itself — only orchestrator.py did, around its whole cycle — so a
    # standalone/manual invocation (e.g. `python3
    # lead_discovery_and_evaluation_Contactsearch.py ...` directly,
    # bypassing orchestrator.py) raced a concurrent orchestrator cycle
    # unprotected, and both independently snapshotted the CRM's existing
    # names before either had written anything, double-writing the same
    # qualifying businesses (same bug class hp_lock.py's docstring already
    # describes, just via a second, unguarded entry point). Acquiring here
    # too closes that gap; HELD_BY_PARENT_ENV_VAR skips it (and skips
    # releasing it) when orchestrator is the one invoking us, since it
    # already holds the lock for the whole cycle.
    held_by_parent = os.environ.get(hp_lock.HELD_BY_PARENT_ENV_VAR) == "1"
    if held_by_parent:
        return _run()
    if not hp_lock.acquire_lock():
        print("Another run appears to be in progress — refusing to run standalone and risk a duplicate-write race.")
        sys.exit(1)
    try:
        return _run()
    finally:
        hp_lock.release_lock()


def _run():
    if len(sys.argv) != 3:
        print("Usage: python3 lead_discovery_and_evaluation_Contactsearch.py \"<niche>\" \"<location>\"")
        sys.exit(1)

    # Found live 2026-08-30: GEMINI_API_KEY was never actually set (on
    # either the Mac or the VM) since hp_llm.py switched from local Ollama
    # to Gemini on 2026-08-20 — every generate_middle_continuation()/
    # generate_hero_line() call had been silently returning None for 10
    # days, discarding every single qualifying lead right before it would
    # have been written to Notion, with zero error and zero visibility (a
    # print statement nobody was watching, not an exception). A real
    # exception here — not sys.exit(), and deliberately inside main()
    # rather than at module import time — routes through
    # hp_runlog.run_wrapped()'s failure-alert email, so a missing/revoked
    # key now fires a real alert to Cyril immediately instead of silently
    # discarding leads for days.
    if not hp_llm.GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY is not set — every AI content generation call would silently fail and discard every lead.")

    niche, location = sys.argv[1], sys.argv[2]
    print(f"Target: {MIN_QUALIFYING_LEADS} new Box 1 leads (with email) for '{niche}' in '{location}'")

    existing_names = get_existing_business_names()
    cache = load_cache()
    print(f"{len(existing_names)} businesses already in CRM. {len(cache)} businesses in local check-cache.")

    counts = {
        "box1_written": 0, "skipped_duplicate": 0, "skipped_cached": 0,
        "skipped_website_unknown": 0, "discarded_website_disqualified": 0,
        "discarded_no_email": 0,
        "discarded_content_gen_failed": 0, "discarded_write_failed": 0,
    }
    qualifying_found = 0
    total_checked = 0
    seen_names_this_run = set()
    stop_reason = {"reason": None}

    for place, places in discover_places(niche, location, stop_reason):
        if qualifying_found >= MIN_QUALIFYING_LEADS or total_checked >= MAX_TOTAL_CHECKED:
            break

        # Keeps the orchestrator's run-lock fresh while this loop is
        # legitimately still working (see hp_lock.refresh_lock()) — a
        # large market plus per-site crawling can genuinely take
        # well past the lock's staleness threshold. Safe no-op when
        # run standalone with no lock held.
        hp_lock.refresh_lock()

        name = place.get("displayName", {}).get("text", "Unknown")
        place_id = place.get("id", name)

        skip_reason = determine_skip_reason(place, existing_names, seen_names_this_run, cache)
        if skip_reason == "duplicate":
            counts["skipped_duplicate"] += 1
            continue
        if skip_reason == "cached":
            counts["skipped_cached"] += 1
            continue

        seen_names_this_run.add(name)
        total_checked += 1

        # PageSpeed still runs for every candidate (Story 5's "must
        # retain PageSpeed" requirement) and its pdata/reason still
        # feed the email content and Notion record below, but as of
        # 2026-08-30 it no longer decides who gets emailed — that
        # decision is now opportunity_scoring.qualifies_by_website_signals()
        # (Cyril's rule, updated 2026-08-31: booking presence alone
        # decides it — a lead with real online booking already live
        # disqualifies, CMS is no longer part of the decision at all).
        _, pdata, reason = classify_lead(place)

        crawl_result = website_crawler.crawl_and_qualify(place.get("websiteUri"))
        pagespeed_mobile_is_poor = bool(
            pdata and pdata["scores"].get("performance") is not None
            and pdata["scores"]["performance"] < PERFORMANCE_THRESHOLD
        )
        opportunity = opportunity_scoring.calculate_opportunity_score(crawl_result, pagespeed_mobile_is_poor)
        qualification_reason = opportunity_scoring.generate_qualification_reason(
            name, crawl_result, pagespeed_mobile_is_poor, opportunity
        )
        website_qualifies = opportunity_scoring.qualifies_by_website_signals(crawl_result)

        if website_qualifies == "Unknown":
            # Cyril's explicit call (2026-08-30): never guess on an
            # Unknown signal in either direction. Deliberately left
            # uncached (like a content-gen/Notion-write failure) so a
            # future run retries once the crawl can actually confirm
            # booking status one way or the other.
            counts["skipped_website_unknown"] += 1
            print(f"  [{total_checked}] {name} -> website signals Unknown, skipped (not cached, will retry)")
            time.sleep(0.3)
            continue

        if website_qualifies == "Disqualify":
            counts["discarded_website_disqualified"] += 1
            cache[place_id] = "website_disqualified"
            save_cache(cache)
            print(f"  [{total_checked}] {name} -> disqualified (has online booking already), discarded")
            time.sleep(0.3)
            continue

        gap_reason = opportunity_scoring.determine_primary_website_gap(crawl_result)

        competitor = find_competitor(places, places.index(place))
        contact = find_contact_info(place.get("websiteUri"))
        content_snapshot = scrape_website_content_snapshot(place.get("websiteUri"))
        timezone = get_timezone_from_address(place.get("formattedAddress"))

        if not contact["email"]:
            counts["discarded_no_email"] += 1
            cache[place_id] = "no_email"
            save_cache(cache)
            print(f"  [{total_checked}] {name} -> qualifies ({reason}, {gap_reason}) but NO EMAIL FOUND, discarded")
            time.sleep(0.3)
            continue

        middle = generate_middle_continuation(name, niche, competitor, pdata, reason, gap_reason=gap_reason)
        if not middle:
            # Deliberately NOT cached — found live 2026-08-30, a
            # missing/revoked GEMINI_API_KEY made this fail for every
            # single candidate silently for 10 days. A single failure
            # is more likely transient (a rate limit, a bad response)
            # than "this business is permanently unwriteable", so
            # leave it uncached to retry on a future run rather than
            # blacklisting a genuinely good lead forever.
            counts["discarded_content_gen_failed"] += 1
            print(f"  [{total_checked}] {name} -> qualifies ({reason}, {gap_reason}) but CONTENT GENERATION FAILED, discarded (not cached, will retry)")
            time.sleep(0.3)
            continue
        subject, body = build_email(name, competitor, middle, reason, niche, gap_reason=gap_reason)
        hero_line = generate_hero_line(name, niche, location)

        success = write_to_notion(place, pdata, reason, competitor, contact, subject, body, niche, location, timezone, hero_line, content_snapshot, crawl_result, opportunity, qualification_reason)
        if not success:
            # Same reasoning as content-gen failures above: a Notion
            # write failure is more likely a transient API hiccup than
            # a permanent problem with this specific lead, so don't
            # cache it as "written" (which would silently and
            # permanently hide it from every future run) and don't
            # count it toward the day's qualifying quota.
            counts["discarded_write_failed"] += 1
            print(f"  [{total_checked}] {name} -> qualifies ({reason}, {gap_reason}) but NOTION WRITE FAILED, discarded (not cached, will retry)")
            time.sleep(0.3)
            continue

        counts["box1_written"] += 1
        qualifying_found += 1
        cache[place_id] = "written"
        save_cache(cache)

        print(f"  [{total_checked}] {name} -> qualifies ({reason}, {gap_reason}) (OK) [qualifying: {qualifying_found}/{MIN_QUALIFYING_LEADS}]")

        time.sleep(0.5)

    if stop_reason["reason"] == "budget_exhausted":
        print("Stopped: Places API monthly call budget reached (not a market-exhausted signal).")
    elif stop_reason["reason"] == "market_exhausted":
        print("No more results from Google Places across the whole search grid — market exhausted.")

    print("\n--- Summary ---")
    print(f"Box 1 leads found (with email): {qualifying_found}/{MIN_QUALIFYING_LEADS}")
    if qualifying_found < MIN_QUALIFYING_LEADS:
        print(f"Target not reached - market may be exhausted for this niche/location.")
    print(f"Total businesses checked: {total_checked}")
    print(f"Skipped (duplicates already in CRM): {counts['skipped_duplicate']}")
    print(f"Skipped (already in local cache): {counts['skipped_cached']}")
    print(f"Skipped (website signals Unknown): {counts['skipped_website_unknown']}")
    print(f"Discarded (website disqualified: has online booking already): {counts['discarded_website_disqualified']}")
    print(f"Discarded (qualifies, no email): {counts['discarded_no_email']}")
    print(f"Discarded (content generation failed): {counts['discarded_content_gen_failed']}")
    print(f"Discarded (Notion write failed): {counts['discarded_write_failed']}")

    # Never mark a real market exhausted just because this month's Places
    # API call budget ran out (Cyril's explicit call, 2026-08-31) — the
    # same niche/location should retry for real once the budget resets,
    # not get treated as a dead market and passed over for a new one.
    if stop_reason["reason"] != "budget_exhausted":
        mark_exhausted_if_needed(qualifying_found, niche, location)

    return {
        "leads_found": qualifying_found,
        # Found live 2026-08-30: this notes string never included
        # box1_written/qualifying_found at all — a run could silently
        # write zero real leads (every candidate discarded after
        # content-gen or Notion-write failures) while state.db still just
        # showed a plausible-looking "N checked, M cached, ..." line with
        # no visible sign anything was actually wrong. Now leads directly
        # with the number that actually matters.
        "notes": f"{niche} in {location}: {qualifying_found} written, {total_checked} checked, "
                 f"{counts['skipped_duplicate']} dup, {counts['skipped_cached']} cached, "
                 f"{counts['skipped_website_unknown']} website-unknown, "
                 f"{counts['discarded_website_disqualified']} website-disqualified, {counts['discarded_no_email']} no-email, "
                 f"{counts['discarded_content_gen_failed']} content-gen-failed, {counts['discarded_write_failed']} write-failed",
    }


if __name__ == "__main__":
    import hp_runlog
    hp_runlog.run_wrapped("lead_discovery_and_evaluation_Contactsearch", main)
