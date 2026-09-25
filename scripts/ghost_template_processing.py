#!/usr/bin/env python3
"""
Ghost Site template processing — SiteScout (Track B)

Turns a raw template upload (an email attachment, or a direct dashboard
upload) into a clean HTML file ready to use as a niche template.

Two things happen, both purely mechanical — no content judgment, nothing
here decides what's "real" data vs. fabricated (that's still a human
call, see the AUDIT/PLAN notes on why a confirmation step exists around
this):

1. Unbundling. Paper AI's "downloadable HTML" isn't plain markup — it's
   a self-contained "bundler" format (compressed assets referenced by
   UUID, reconstructed into blob URLs by an unpacking script at load
   time, so the file works standalone anywhere). The real page content
   ships as a JSON-encoded string inside a
   <script type="__bundler/template"> tag; everything else in the file
   is asset packaging, not content. Confirmed by hand while converting
   the first template (Sprint 16, Elite Barbers) — not guessed at.

2. Script-stripping. Every <script>...</script> block gets removed,
   including external references (a <script src="..."> with no inline
   body still matches). Any interactive widget (a booking calendar,
   live availability, etc.) arrives via JS — stripping it removes the
   mechanism outright rather than trying to detect and fix broken or
   fabricated interactive behavior. ghost_site_builder.py injects its
   own canonical, already-vetted render.js at build time (Sprint 17
   follow-up, 2026-08-16) — nothing from an upload is ever trusted to
   supply its own script.

Data-slot contract for the automated path: Cyril marks real data spots
in Paper AI with data-slot attributes (data-slot="business_name",
data-slot-href="phone_tel", data-slot-list="hours",
data-slot-hidden-if-empty="review_count") — a real DOM attribute rather
than a text placeholder, matching how the shared render.js actually
finds and populates them at page-load time. This replaced an earlier
{{token}}-text-substitution design (Sprint 18a) that only ever supported
a single flattened HTML file with no separate CSS/JS — abandoned once it
turned out to not match how Cyril's real Paper AI designs, or the one
already-built template (Sprint 16), actually work.
"""

import json
import re

BUNDLER_TEMPLATE_MARKER = "__bundler/template"

DATA_SLOT_ATTRS = ("data-slot", "data-slot-href", "data-slot-list", "data-slot-hidden-if-empty")
DATA_SLOT_RE = re.compile(r'\b(?:' + "|".join(re.escape(a) for a in DATA_SLOT_ATTRS) + r')="([^"]+)"')


def is_bundler_export(html_text):
    return BUNDLER_TEMPLATE_MARKER in html_text


def unbundle(html_text):
    """Extracts the real page HTML out of a Paper AI bundler export.
    Raises ValueError if the expected script tag isn't present."""
    match = re.search(
        r'<script type="__bundler/template">(.*?)</script>', html_text, re.DOTALL
    )
    if not match:
        raise ValueError(
            "No __bundler/template script tag found — not a recognized Paper AI export"
        )
    return json.loads(match.group(1).strip())


def strip_scripts(html_text):
    return re.sub(r"<script\b[^>]*>.*?</script>", "", html_text, flags=re.DOTALL | re.IGNORECASE)


def process_template_upload(html_text):
    """Full mechanical pipeline: unbundle if it's a Paper AI export,
    then strip every script tag. Returns clean HTML with no embedded
    JS — ghost_site_builder.py adds the real render.js reference and
    lead.json wiring at build time."""
    if is_bundler_export(html_text):
        html_text = unbundle(html_text)
    return strip_scripts(html_text)


def find_data_slots(html_text):
    """Returns the set of data-slot attribute values present, for
    reporting back to Cyril what data fields a template actually
    references before he confirms it. Covers all four attribute forms
    the shared render.js understands."""
    return set(DATA_SLOT_RE.findall(html_text))
