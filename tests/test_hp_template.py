#!/usr/bin/env python3
"""
Tests for scripts/hp_template.py — the [TOKEN] bracket-notation template
renderer (2026-08-25) that replaced Python's str.format() {token} syntax
across every dashboard-editable email.

Covers the two real reasons this exists, not just cosmetic preference:
  - [TOKEN] reads as "paste this exact text" to a non-technical editor
    far more obviously than {token} does.
  - Unlike str.format(), a stray/unmatched bracket never raises — it's
    left in the output untouched, since a crashed send is worse than a
    visibly-wrong one that's easy to spot and fix.
"""

import os
import sys

SCRIPTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
sys.path.insert(0, SCRIPTS_DIR)

import hp_template  # noqa: E402


def test_render_substitutes_single_token():
    assert hp_template.render("Hi [BUSINESS_NAME]!", business_name="Acme") == "Hi Acme!"


def test_render_substitutes_repeated_token():
    result = hp_template.render("[BUSINESS_NAME] loves [BUSINESS_NAME].", business_name="Acme")
    assert result == "Acme loves Acme."


def test_render_substitutes_multiple_distinct_tokens():
    result = hp_template.render(
        "[BUSINESS_NAME] is in [NICHE].", business_name="Acme Salon", niche="hair salons"
    )
    assert result == "Acme Salon is in hair salons."


def test_render_leaves_unmatched_token_untouched_not_a_crash():
    # No exception, no KeyError — the literal token stays visible so a
    # missing kwarg is an obvious, fixable mistake instead of a dead send.
    result = hp_template.render("Hi [BUSINESS_NAME], see [MISSING_TOKEN].", business_name="Acme")
    assert result == "Hi Acme, see [MISSING_TOKEN]."


def test_render_treats_none_value_as_unmatched():
    result = hp_template.render("Rival: [COMPETITOR_NAME]", competitor_name=None)
    assert result == "Rival: [COMPETITOR_NAME]"


def test_render_ignores_stray_non_token_brackets():
    # Lowercase or non-identifier bracket contents were never a real
    # token to begin with — left alone rather than treated as an error.
    result = hp_template.render("See [this] and [not a token].")
    assert result == "See [this] and [not a token]."


def test_render_no_tokens_at_all_returns_text_unchanged():
    assert hp_template.render("Plain text, no tokens here.") == "Plain text, no tokens here."


def test_render_empty_kwargs_leaves_all_tokens():
    assert hp_template.render("[A] and [B]") == "[A] and [B]"


# --- tracking_link(): 2026-08-25, click-tracking ---

def test_tracking_link_embeds_page_id():
    assert hp_template.tracking_link("abc123") == "https://track.example.com/c?p=abc123"


def test_tracking_link_different_leads_get_different_links():
    # The whole point — one shared link couldn't identify who clicked.
    link_a = hp_template.tracking_link("lead-a")
    link_b = hp_template.tracking_link("lead-b")
    assert link_a != link_b
    assert "lead-a" in link_a
    assert "lead-b" in link_b
