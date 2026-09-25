#!/usr/bin/env python3
"""
Tests for scripts/ghost_site_builder.py.

Covers: project_slug_for()'s uniqueness/format contract, filtering
buildable leads down to niches with an approved template, the
build-one pipeline (data -> substitute -> deploy -> domain -> DNS ->
Notion write) and that a failure at any step stops before the next and
never writes Ghost Site URL, and that Ghost Site Status is never
touched here (ghost_site_sequence.py's job).
"""

import os
import sys

import pytest

SCRIPTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
sys.path.insert(0, SCRIPTS_DIR)

import ghost_site_builder as gsb  # noqa: E402


# --- project_slug_for() ---

def test_project_slug_for_is_lowercase_hyphenated():
    slug = gsb.project_slug_for("abc123def456", "Westgate Barber & Co.")
    assert slug.startswith("westgate-barber-co")
    assert slug == slug.lower()
    assert " " not in slug


def test_project_slug_for_differs_by_page_id():
    a = gsb.project_slug_for("aaaaaaaa1111", "Same Name")
    b = gsb.project_slug_for("bbbbbbbb2222", "Same Name")
    assert a != b


def test_project_slug_for_truncates_long_names():
    long_name = "A" * 100
    slug = gsb.project_slug_for("abc123def456", long_name)
    assert len(slug) <= 49  # 40 base + hyphen + 8 suffix


# --- build_one(): the full per-lead pipeline ---

def _lead(page_id="page-1", name="Westgate Barber"):
    return {"page_id": page_id, "name": name, "source_niche_location": "Barber Shops / Ann Arbor, MI"}


def _stub_render_js(monkeypatch):
    monkeypatch.setattr(gsb, "load_render_js", lambda: "/* render.js */")


def test_build_one_success_writes_only_ghost_site_url(monkeypatch):
    _stub_render_js(monkeypatch)
    monkeypatch.setattr(gsb.lead_data, "build_lead_json", lambda page_id: {"business_name": "Westgate Barber"})
    monkeypatch.setattr(gsb.cf, "deploy_site", lambda project, files: (True, "https://x.pages.dev", None))
    monkeypatch.setattr(gsb.cf, "attach_custom_domain", lambda project, domain: (True, ""))
    monkeypatch.setattr(gsb.cf, "create_dns_cname", lambda sub, target: (True, ""))

    written = {}
    monkeypatch.setattr(gsb, "write_ghost_site_url", lambda page_id, url: written.setdefault(page_id, url) or True)

    run_calls = []
    gsb.build_one(_lead(), "<html><body></body></html>", None, run_calls)

    assert run_calls == [("built", "page-1", True)]
    assert written["page-1"].startswith("https://westgate-barber")
    assert written["page-1"].endswith(".example.com")


def test_build_one_deploys_index_render_js_and_lead_json_without_css(monkeypatch):
    _stub_render_js(monkeypatch)
    monkeypatch.setattr(gsb.lead_data, "build_lead_json", lambda page_id: {"business_name": "Westgate Barber"})
    monkeypatch.setattr(gsb.cf, "attach_custom_domain", lambda project, domain: (True, ""))
    monkeypatch.setattr(gsb.cf, "create_dns_cname", lambda sub, target: (True, ""))
    monkeypatch.setattr(gsb, "write_ghost_site_url", lambda page_id, url: True)

    deployed = {}
    def fake_deploy(project, files):
        deployed.update(dict(files))
        return True, "https://x.pages.dev", None
    monkeypatch.setattr(gsb.cf, "deploy_site", fake_deploy)

    gsb.build_one(_lead(), "<html><body></body></html>", None, [])

    assert set(deployed) == {"index.html", "render.js", "lead.json"}
    assert b"Westgate Barber" in deployed["lead.json"]


def test_build_one_includes_style_css_when_provided(monkeypatch):
    _stub_render_js(monkeypatch)
    monkeypatch.setattr(gsb.lead_data, "build_lead_json", lambda page_id: {"business_name": "Westgate Barber"})
    monkeypatch.setattr(gsb.cf, "attach_custom_domain", lambda project, domain: (True, ""))
    monkeypatch.setattr(gsb.cf, "create_dns_cname", lambda sub, target: (True, ""))
    monkeypatch.setattr(gsb, "write_ghost_site_url", lambda page_id, url: True)

    deployed = {}
    def fake_deploy(project, files):
        deployed.update(dict(files))
        return True, "https://x.pages.dev", None
    monkeypatch.setattr(gsb.cf, "deploy_site", fake_deploy)

    gsb.build_one(_lead(), "<html><body></body></html>", b".x{color:red}", [])

    assert deployed["style.css"] == b".x{color:red}"


def test_build_one_stops_on_deploy_failure_no_notion_write(monkeypatch):
    _stub_render_js(monkeypatch)
    monkeypatch.setattr(gsb.lead_data, "build_lead_json", lambda page_id: {"business_name": "Westgate Barber"})
    monkeypatch.setattr(gsb.cf, "deploy_site", lambda project, files: (False, None, "deploy exploded"))

    def should_not_attach(*a, **k):
        raise AssertionError("attach_custom_domain should not be called after deploy failure")
    monkeypatch.setattr(gsb.cf, "attach_custom_domain", should_not_attach)

    def should_not_write(*a, **k):
        raise AssertionError("write_ghost_site_url should not be called after deploy failure")
    monkeypatch.setattr(gsb, "write_ghost_site_url", should_not_write)

    run_calls = []
    gsb.build_one(_lead(), "<html></html>", None, run_calls)
    assert run_calls == [("deploy_failed", "page-1", False)]


def test_build_one_stops_on_domain_attach_failure(monkeypatch):
    _stub_render_js(monkeypatch)
    monkeypatch.setattr(gsb.lead_data, "build_lead_json", lambda page_id: {"business_name": "Westgate Barber"})
    monkeypatch.setattr(gsb.cf, "deploy_site", lambda project, files: (True, "https://x.pages.dev", None))
    monkeypatch.setattr(gsb.cf, "attach_custom_domain", lambda project, domain: (False, "boom"))

    def should_not_dns(*a, **k):
        raise AssertionError("create_dns_cname should not be called after domain attach failure")
    monkeypatch.setattr(gsb.cf, "create_dns_cname", should_not_dns)

    run_calls = []
    gsb.build_one(_lead(), "<html></html>", None, run_calls)
    assert run_calls == [("domain_attach_failed", "page-1", False)]


def test_build_one_skips_when_lead_data_unavailable(monkeypatch):
    monkeypatch.setattr(gsb.lead_data, "build_lead_json", lambda page_id: None)

    def should_not_deploy(*a, **k):
        raise AssertionError("deploy_site should not be called when lead data is missing")
    monkeypatch.setattr(gsb.cf, "deploy_site", should_not_deploy)

    run_calls = []
    gsb.build_one(_lead(), "<html></html>", None, run_calls)
    assert run_calls == [("fetch_failed", "page-1", False)]


# --- main(): niche filtering against approved templates ---

def test_main_skips_leads_without_approved_template(monkeypatch, tmp_path):
    template_path = tmp_path / "index.html"
    template_path.write_text('<html><body><h1 data-slot="business_name"></h1></body></html>')

    monkeypatch.setattr(gsb, "load_state", lambda: {
        "Barber Shops": {"status": "approved", "template_path": str(template_path)},
    })
    monkeypatch.setattr(gsb, "get_buildable_leads", lambda: [
        {"page_id": "p1", "name": "Shop A", "source_niche_location": "Barber Shops / X, Y"},
        {"page_id": "p2", "name": "Cafe B", "source_niche_location": "Coffee Shops / X, Y"},  # no approved template
    ])

    built_pages = []
    monkeypatch.setattr(gsb, "build_one", lambda lead, html, css_bytes, run_calls: (
        built_pages.append(lead["page_id"]), run_calls.append(("built", lead["page_id"], True))
    ))

    gsb.main()
    assert built_pages == ["p1"]


def test_main_skips_niche_with_no_template_file_on_disk(monkeypatch):
    monkeypatch.setattr(gsb, "load_state", lambda: {
        "Barber Shops": {"status": "approved", "template_path": "/nonexistent/path.html"},
    })
    monkeypatch.setattr(gsb, "get_buildable_leads", lambda: [
        {"page_id": "p1", "name": "Shop A", "source_niche_location": "Barber Shops / X, Y"},
    ])

    def should_not_build(*a, **k):
        raise AssertionError("build_one should not be called when template file is missing")
    monkeypatch.setattr(gsb, "build_one", should_not_build)

    gsb.main()  # should not raise


def test_main_loads_css_for_niche_when_present(monkeypatch, tmp_path):
    template_path = tmp_path / "index.html"
    template_path.write_text('<html><body><h1 data-slot="business_name"></h1></body></html>')
    css_path = tmp_path / "style.css"
    css_path.write_text(".x{color:red}")

    monkeypatch.setattr(gsb, "load_state", lambda: {
        "Barber Shops": {"status": "approved", "template_path": str(template_path), "css_path": str(css_path)},
    })
    monkeypatch.setattr(gsb, "get_buildable_leads", lambda: [
        {"page_id": "p1", "name": "Shop A", "source_niche_location": "Barber Shops / X, Y"},
    ])

    captured = {}
    monkeypatch.setattr(gsb, "build_one", lambda lead, html, css_bytes, run_calls: (
        captured.setdefault("css_bytes", css_bytes), run_calls.append(("built", lead["page_id"], True))
    ))

    gsb.main()
    assert captured["css_bytes"] == b".x{color:red}"


def test_main_css_none_when_niche_has_no_css_path(monkeypatch, tmp_path):
    template_path = tmp_path / "index.html"
    template_path.write_text('<html><body><h1 data-slot="business_name"></h1></body></html>')

    monkeypatch.setattr(gsb, "load_state", lambda: {
        "Barber Shops": {"status": "approved", "template_path": str(template_path)},
    })
    monkeypatch.setattr(gsb, "get_buildable_leads", lambda: [
        {"page_id": "p1", "name": "Shop A", "source_niche_location": "Barber Shops / X, Y"},
    ])

    captured = {}
    monkeypatch.setattr(gsb, "build_one", lambda lead, html, css_bytes, run_calls: (
        captured.setdefault("css_bytes", css_bytes), run_calls.append(("built", lead["page_id"], True))
    ))

    gsb.main()
    assert captured["css_bytes"] is None


# --- inject_purchase_banner(): Sprint 22 follow-up, auto-stamped on every site ---
# Follow-up 2026-08-16: "Click here to purchase" opens an in-page modal
# (name/email/phone/message, POSTs to Web3Forms) instead of navigating
# away — interim until a real Razorpay checkout replaces it.

def test_inject_purchase_banner_inserts_after_body_tag():
    html = "<html><head></head><body><h1>Real content</h1></body></html>"
    result = gsb.inject_purchase_banner(html, "Westgate Barber")
    assert result.index("<body>") < result.index("sitescout-demo-banner") < result.index("<h1>")


def test_inject_purchase_banner_handles_body_tag_with_attributes():
    html = '<html><body class="dark" id="page"><h1>Content</h1></body></html>'
    result = gsb.inject_purchase_banner(html, "Westgate Barber")
    assert result.index('<body class="dark" id="page">') < result.index("sitescout-demo-banner")


def test_inject_purchase_banner_falls_back_when_no_body_tag():
    html = "<h1>No body tag at all</h1>"
    result = gsb.inject_purchase_banner(html, "Westgate Barber")
    assert "sitescout-demo-banner" in result
    assert result.endswith(html)


def test_inject_purchase_banner_says_demo_purposes_only():
    result = gsb.inject_purchase_banner("<html><body></body></html>", "Westgate Barber")
    assert "demo purposes only" in result
    assert "Click here to purchase" in result


def test_purchase_banner_reserves_body_space_for_its_own_height():
    """Regression, found live 2026-08-27 on a real deployed barber-shops
    ghost site: the banner is position:fixed (deliberately, to sit on top
    of arbitrary template CSS), which takes it out of document flow —
    nothing pushed the template's own header down, so the two rendered
    stacked on top of each other at top:0. Measuring the banner's actual
    rendered height at runtime (not a fixed padding guess) also survives a
    business name long enough to wrap the banner onto two lines."""
    result = gsb.build_purchase_banner("Westgate Barber")
    assert 'getElementById("sitescout-demo-banner")' in result
    assert "document.body.style.paddingTop" in result
    assert "offsetHeight" in result


# --- build_purchase_banner(): the modal itself ---

def test_build_purchase_banner_includes_business_name_in_heading():
    result = gsb.build_purchase_banner("Westgate Barber")
    assert "Interested in Westgate Barber?" in result


def test_build_purchase_banner_html_escapes_business_name():
    result = gsb.build_purchase_banner("A & B <Test>")
    heading_line = next(line for line in result.splitlines() if line.startswith("<h2"))
    assert "Interested in A &amp; B &lt;Test&gt;?" in heading_line
    assert "<Test>" not in heading_line


def test_build_purchase_banner_js_escapes_quotes_in_business_name():
    result = gsb.build_purchase_banner('The "Best" Cuts')
    assert 'Purchase interest: The \\"Best\\" Cuts' in result


def test_build_purchase_banner_defuses_script_close_in_business_name():
    """A business name containing a literal </script> must never be able
    to prematurely close the real <script> tag this ends up inside —
    the HTML parser sees that byte sequence before any JS-level quoting
    matters, regardless of how the string is otherwise escaped."""
    result = gsb.build_purchase_banner("Evil</script><script>alert(1)</script>")
    subject_start = result.index('data.append("subject"')
    subject_section = result[subject_start:subject_start + 150]
    assert "</script>" not in subject_section


def test_build_purchase_banner_uses_none_when_no_business_name():
    result = gsb.build_purchase_banner(None)
    assert "Interested in this business?" in result


def test_build_purchase_banner_embeds_web3forms_key():
    result = gsb.build_purchase_banner("Westgate Barber", web3forms_access_key="test-key-123")
    assert 'data.append("access_key", "test-key-123")' in result


def test_build_purchase_banner_includes_form_fields():
    result = gsb.build_purchase_banner("Westgate Barber")
    assert 'name="name"' in result
    assert 'name="email"' in result
    assert 'name="phone"' in result
    assert 'name="message"' in result
    assert "api.web3forms.com/submit" in result


# --- inject_asset_references(): the <link>/<script> wiring Cyril never has to do himself ---

def test_inject_asset_references_adds_render_script_before_body_close():
    html = "<html><head></head><body><h1>Content</h1></body></html>"
    result = gsb.inject_asset_references(html, has_css=False)
    assert result.index("<h1>Content</h1>") < result.index('<script src="render.js">') < result.index("</body>")


def test_inject_asset_references_skips_css_link_when_no_css():
    html = "<html><head></head><body></body></html>"
    result = gsb.inject_asset_references(html, has_css=False)
    assert "style.css" not in result


def test_inject_asset_references_adds_css_link_in_head_when_css_present():
    html = "<html><head><title>X</title></head><body></body></html>"
    result = gsb.inject_asset_references(html, has_css=True)
    assert result.index("<title>X</title>") < result.index('<link rel="stylesheet" href="style.css">') < result.index("</head>")


def test_inject_asset_references_falls_back_when_no_head_tag():
    html = "<html><body></body></html>"
    result = gsb.inject_asset_references(html, has_css=True)
    assert 'href="style.css"' in result


def test_inject_asset_references_falls_back_when_no_body_close_tag():
    html = "<html><body>"
    result = gsb.inject_asset_references(html, has_css=False)
    assert 'src="render.js"' in result


def test_build_one_injects_banner_into_deployed_html(monkeypatch):
    _stub_render_js(monkeypatch)
    monkeypatch.setattr(gsb.lead_data, "build_lead_json", lambda pid: {"business_name": "Westgate Barber"})

    deployed_files = {}
    def fake_deploy(project, files):
        deployed_files.update(dict(files))
        return True, "https://x.pages.dev", None
    monkeypatch.setattr(gsb.cf, "deploy_site", fake_deploy)
    monkeypatch.setattr(gsb.cf, "attach_custom_domain", lambda project, domain: (True, ""))
    monkeypatch.setattr(gsb.cf, "create_dns_cname", lambda sub, target: (True, ""))
    monkeypatch.setattr(gsb, "write_ghost_site_url", lambda page_id, url: True)

    gsb.build_one(_lead(), '<html><body><h1 data-slot="business_name"></h1></body></html>', None, [])

    deployed_html = deployed_files["index.html"].decode()
    assert "sitescout-demo-banner" in deployed_html
    assert '<script src="render.js"></script>' in deployed_html
