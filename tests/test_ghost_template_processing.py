#!/usr/bin/env python3
"""
Tests for scripts/ghost_template_processing.py — the mechanical template
upload pipeline (unbundling Paper AI exports + script-stripping), plus
placeholder substitution.

Verified against the real Elite Barbers.html export from Sprint 16, not
just a synthetic fixture — that's what caught the actual bundler format
in the first place.
"""

import os
import sys

import pytest

SCRIPTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
sys.path.insert(0, SCRIPTS_DIR)

import ghost_template_processing as gtp  # noqa: E402

# Point this at a real Paper AI template export to run the export tests.
REAL_EXPORT_PATH = os.environ.get("GHOST_TEMPLATE_EXPORT", "Elite Barbers.html")


# --- is_bundler_export() / unbundle() ---

def test_is_bundler_export_true_for_bundler_format():
    html = '<script type="__bundler/template">"content"</script>'
    assert gtp.is_bundler_export(html) is True


def test_is_bundler_export_false_for_plain_html():
    assert gtp.is_bundler_export("<html><body>Hello</body></html>") is False


def test_unbundle_extracts_json_encoded_content():
    html = '<script type="__bundler/template">"<div>Hello</div>"</script>'
    assert gtp.unbundle(html) == "<div>Hello</div>"


def test_unbundle_raises_when_tag_missing():
    with pytest.raises(ValueError):
        gtp.unbundle("<html>no bundler tag here</html>")


@pytest.mark.skipif(not os.path.exists(REAL_EXPORT_PATH), reason="real Paper AI export not present on this machine")
def test_unbundle_real_paper_ai_export():
    with open(REAL_EXPORT_PATH) as f:
        raw = f.read()
    assert gtp.is_bundler_export(raw) is True
    unbundled = gtp.unbundle(raw)
    assert "Elite Barbers" in unbundled
    assert len(unbundled) > 10000


# --- strip_scripts() ---

def test_strip_scripts_removes_all_script_tags():
    html = "<div>keep me</div><script>alert(1)</script><p>also keep</p><script src='x.js'></script>"
    result = gtp.strip_scripts(html)
    assert "<script" not in result.lower()
    assert "keep me" in result
    assert "also keep" in result


def test_strip_scripts_handles_multiline_script_content():
    html = "<div>a</div><script>\nfunction f() {\n  return 1;\n}\n</script><div>b</div>"
    result = gtp.strip_scripts(html)
    assert "function f" not in result
    assert "<div>a</div>" in result and "<div>b</div>" in result


# --- process_template_upload(): full pipeline ---

def test_process_template_upload_unbundles_and_strips():
    html = '<script type="__bundler/template">"<div>Real</div><script>bad()<\\/script>"</script>'
    result = gtp.process_template_upload(html)
    assert "<script" not in result.lower()
    assert "Real" in result


def test_process_template_upload_passes_through_plain_html_unchanged_except_scripts():
    html = "<div>Plain</div><script>bad()</script>"
    result = gtp.process_template_upload(html)
    assert "Plain" in result
    assert "<script" not in result.lower()


@pytest.mark.skipif(not os.path.exists(REAL_EXPORT_PATH), reason="real Paper AI export not present on this machine")
def test_process_template_upload_real_export_has_no_scripts_left():
    with open(REAL_EXPORT_PATH) as f:
        raw = f.read()
    result = gtp.process_template_upload(raw)
    assert result.lower().count("<script") == 0


# --- find_data_slots(): the data-slot attribute contract ---

def test_find_data_slots_finds_plain_slots():
    html = '<h1 data-slot="business_name"></h1><p data-slot="phone"></p>'
    assert gtp.find_data_slots(html) == {"business_name", "phone"}


def test_find_data_slots_finds_all_four_attribute_forms():
    html = (
        '<h1 data-slot="business_name"></h1>'
        '<a data-slot-href="phone_tel"></a>'
        '<ul data-slot-list="hours"></ul>'
        '<section data-slot-hidden-if-empty="review_count"></section>'
    )
    assert gtp.find_data_slots(html) == {"business_name", "phone_tel", "hours", "review_count"}


def test_find_data_slots_empty_when_none_present():
    assert gtp.find_data_slots("<div>no data slots here</div>") == set()


def test_find_data_slots_ignores_unrelated_attributes():
    html = '<div data-testid="business_name" data-slot="phone"></div>'
    assert gtp.find_data_slots(html) == {"phone"}
