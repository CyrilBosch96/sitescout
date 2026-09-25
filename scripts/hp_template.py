#!/usr/bin/env python3
"""
Bracket-token template rendering — SiteScout (2026-08-25)

Every editable email template (Track A first touch + follow-ups, Track B
preview sequence + ops) uses [TOKEN_NAME] placeholders instead of Python's
str.format() {token} syntax. Two real reasons, not cosmetic:

1. Cyril edits these directly through the dashboard — [COMPETITOR_NAME]
   reads as "paste this exact text" far more obviously than
   {competitor_name} to someone who isn't writing Python.
2. str.format() raises on ANY stray brace in the copy (a business hour
   like "8am-{5pm}" typed by accident, or curly braces pasted in from
   somewhere else) — a single bad edit would crash the send entirely.
   render() never raises: an unmatched token is left exactly as written
   in the sent email, a visible, easy-to-spot mistake instead of a hard
   failure at send time.
"""

import re

_TOKEN_RE = re.compile(r"\[([A-Z0-9_]+)\]")

TRACKING_DOMAIN = "https://track.example.com"


def tracking_link(page_id):
    """The one place the click-tracking URL shape is defined — every lead
    gets their own link (their real Notion page ID in the query string),
    not one shared link, so a click identifies exactly who clicked."""
    return f"{TRACKING_DOMAIN}/c?p={page_id}"


def render(text, **kwargs):
    """kwargs are lowercase Python names (business_name=...), matched
    against [BUSINESS_NAME]-style tokens in the text. A token with no
    matching kwarg (or a value of None) is left untouched rather than
    raising or blanking it out."""
    values = {k.upper(): v for k, v in kwargs.items()}

    def _sub(match):
        token = match.group(1)
        if token in values and values[token] is not None:
            return str(values[token])
        return match.group(0)

    return _TOKEN_RE.sub(_sub, text)
