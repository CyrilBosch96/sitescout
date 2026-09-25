#!/usr/bin/env python3
"""
LLM wrapper — SiteScout (2026-08-20)

Replaces direct calls to a locally-running Ollama server (previously
duplicated in inbox_monitoring.py and lead_discovery_and_evaluation_
Contactsearch.py) with Gemini's free-tier API, as part of the move to an
always-on cloud VM too small (1GB RAM, Always Free tier) to run a local
9B model. One call site, one place to swap providers again later if
needed — callers keep their own prompt-building and fail-open/fail-closed
behavior; this module only ever returns text or None.

Model changed to gemini-3.1-flash-lite 2026-09-01, found live: the
originally-used gemini-3.6-flash has a free-tier quota of just 20
requests PER DAY (GenerateRequestsPerDayPerProjectPerModel-FreeTier,
confirmed via the real 429 error body, not the per-minute limit it
initially looked like) — a single lead-discovery run processing
qualifying leads (2 Gemini calls each — generate_middle_continuation +
generate_hero_line) blows through an entire day's quota in minutes.
gemini-3.1-flash-lite is a stable (non-preview) lite model with a much
larger free daily allowance, verified live to produce equivalent
quality output for this project's short-form prompts.

Retries on 429 (rate limit) added 2026-09-01, found live the same day:
without a retry, every momentary rate-limit hit (a real short-term RPM
burst, still possible even on the higher-quota model) looked identical
to a real content-generation failure, discarding real qualifying leads
(uncached, so retried on a later run, but wastefully — a real run on
the previous model showed a 52-79% failure rate purely from hitting
its daily cap, zero leads written despite dozens genuinely
qualifying)."""

import time

import os

import requests

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GEMINI_MODEL = "gemini-3.1-flash-lite"
API_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"

MAX_RATE_LIMIT_RETRIES = 3
RATE_LIMIT_BACKOFF_SECONDS = 6


def generate(prompt, timeout=60):
    """Returns the model's text response, or None on any failure — no
    API key configured, network error, a non-200/429 response, or an
    empty/malformed response. Matches the exact "text or None" shape
    both call sites already built their own fallback behavior around.

    A 429 (rate limit) is retried with a fixed backoff up to
    MAX_RATE_LIMIT_RETRIES times before giving up — every other non-200
    status still fails immediately, since those aren't a "wait and it'll
    work" situation."""
    if not GEMINI_API_KEY:
        return None
    for attempt in range(MAX_RATE_LIMIT_RETRIES + 1):
        try:
            resp = requests.post(
                API_URL,
                params={"key": GEMINI_API_KEY},
                json={"contents": [{"parts": [{"text": prompt}]}]},
                timeout=timeout,
            )
            if resp.status_code == 429:
                if attempt < MAX_RATE_LIMIT_RETRIES:
                    time.sleep(RATE_LIMIT_BACKOFF_SECONDS * (attempt + 1))
                    continue
                return None
            if resp.status_code != 200:
                return None
            candidates = resp.json().get("candidates", [])
            if not candidates:
                return None
            parts = candidates[0].get("content", {}).get("parts", [])
            if not parts:
                return None
            return parts[0].get("text", "").strip()
        except Exception:
            return None
    return None
