#!/usr/bin/env python3
"""
Ghost Site Builder — SiteScout (Sprint 17, Track B)
Usage: python3 ghost_site_builder.py

Daily cron (via orchestrator.py). For every lead sitting at Ghost Site
Status "Selected" with no Ghost Site URL yet, whose niche has an
"approved" template (see ghost_template_request.py's state machine —
pending_template -> pending_confirmation -> approved): builds that
lead's real lead.json (Sprint 2/4 data only, ghost_site_lead_data.py),
deploys it alongside the niche's template (its HTML plus an optional
separate CSS file) and the one shared render.js as its own Cloudflare
Pages project, attaches a {slug}.example.com subdomain, and writes
ONLY Ghost Site URL back to Notion.

Multi-file deploy (Sprint 17 architectural fix, 2026-08-16): a niche's
template is Cyril's real Paper AI export — HTML with data-slot
attributes marking real data spots, plus an optional separate CSS file
— never a single flattened file with {{token}} text substitution (an
earlier design that didn't survive contact with how Paper AI actually
exports, or with the one already-built real template, Sprint 16's
barber-shops design). Data gets into the page via the SAME mechanism
Sprint 16 already built and proved: the browser fetches lead.json at
load time and a shared, already-vetted render.js (never anything from
Cyril's own upload — that gets stripped by ghost_template_processing.py
regardless) populates [data-slot] elements via textContent. This script
auto-injects the <link>/<script> references to those assets plus the
purchase banner into the template HTML at build time — Cyril never
needs to wire any of that up himself.

Deliberately does NOT touch Ghost Site Status, Sequence Day, or Site
Expiry Date — ghost_site_sequence.py already owns the Selected->Live
transition the moment it sees a Selected lead with a URL filled in
(same trigger condition, built before this script existed, and still
correct now that this script is what actually fills the URL in). This
script's only job is: template + data -> live URL -> one Notion write.

Every deployed site gets a fixed "demo purposes only, click to purchase"
banner stamped onto it at build time (Sprint 22 follow-up, Cyril's call
2026-08-14) — auto-injected here rather than left for him to add by
hand per template, so it's guaranteed present and applies retroactively
to any future template with zero extra work on his part. Links to the
real example.com for now (no checkout flow exists yet).

One Pages project per lead (not one shared project with multiple
files) so Sprint 19 (expiry/resurrection) can expire a site by deleting
its project outright.

Not gated by --live/DRY_RUN like cold_email.py or ghost_site_sequence.py
— nothing here is a lead-facing send. Deploying a site and writing its
URL to Notion has no external visibility on its own; the lead only ever
sees it once ghost_site_sequence.py sends Day 1, and that send already
has its own live gate.
"""

import html
import json
import os
import re

import requests

import hp_env
import cloudflare_pages as cf
import ghost_site_lead_data as lead_data
from ghost_template_request import STATE_FILE, TEMPLATES_DIR, extract_niche, load_state

NOTION_API_KEY = hp_env.NOTION_API_KEY
NOTION_DATA_SOURCE_ID = hp_env.NOTION_DATA_SOURCE_ID

ROOT_DOMAIN = "example.com"
WEB3FORMS_ACCESS_KEY = os.environ.get("WEB3FORMS_ACCESS_KEY")
SHARED_ASSETS_DIR = os.path.join(hp_env.PROJECT_ROOT, "ghost-sites", "shared")


def load_render_js():
    """The one canonical, already-vetted rendering script every build
    injects — read fresh every call (not cached at import time) so an
    edit to it takes effect on the very next build, same file-backed
    pattern as every other template in this project."""
    with open(os.path.join(SHARED_ASSETS_DIR, "render.js")) as f:
        return f.read()


def inject_asset_references(html, has_css):
    """Adds the <link>/<script> tags the template needs to actually load
    its assets — Cyril's upload never has to include these itself. The
    stylesheet link goes in <head> (before first paint); the render
    script goes right before </body> (after the DOM it operates on)."""
    if has_css:
        head_match = re.search(r"</head>", html, re.IGNORECASE)
        css_tag = '<link rel="stylesheet" href="style.css">'
        if head_match:
            html = html[:head_match.start()] + css_tag + html[head_match.start():]
        else:
            html = css_tag + html

    script_tag = '<script src="render.js"></script>'
    body_close_match = re.search(r"</body>", html, re.IGNORECASE)
    if body_close_match:
        html = html[:body_close_match.start()] + script_tag + html[body_close_match.start():]
    else:
        html = html + script_tag
    return html


# Built with unique __TOKEN__ placeholders + .replace(), deliberately not
# .format() or an f-string — this block is full of real CSS/JS braces
# ({...}) that would collide with either, same reasoning documented in
# ghost_template_processing.py for the (now-retired) {{token}} contract.
_PURCHASE_BANNER_TEMPLATE = """
<div id="sitescout-demo-banner" style="position:fixed;top:0;left:0;right:0;z-index:2147483647;background:#16181a;color:#f2efe9;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;font-size:14px;line-height:1.4;padding:10px 16px;text-align:center;box-shadow:0 2px 8px rgba(0,0,0,0.25);">
This site was created for demo purposes only.
<button id="sitescout-purchase-btn" type="button" style="background:none;border:none;padding:0;color:#f2efe9;text-decoration:underline;font-weight:600;font-size:14px;font-family:inherit;cursor:pointer;">Click here to purchase this website</button>
</div>
<div id="sitescout-purchase-modal" style="display:none;position:fixed;inset:0;z-index:2147483647;background:rgba(0,0,0,0.6);align-items:center;justify-content:center;padding:16px;">
<div style="background:#fff;color:#16181a;max-width:420px;width:100%;border-radius:8px;padding:24px;position:relative;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;box-sizing:border-box;">
<button id="sitescout-modal-close" type="button" style="position:absolute;top:12px;right:14px;background:none;border:none;font-size:22px;line-height:1;cursor:pointer;color:#666;">&times;</button>
<h2 style="margin:0 0 8px;font-size:20px;">Interested in __BUSINESS_NAME_HTML__?</h2>
<p style="margin:0 0 16px;font-size:14px;color:#444;">Leave your details and we'll be in touch to get this site live for real — free preview, no obligation.</p>
<form id="sitescout-purchase-form">
<input type="text" name="name" placeholder="Your name" required style="width:100%;box-sizing:border-box;padding:10px;margin-bottom:10px;border:1px solid #ccc;border-radius:4px;font-size:14px;">
<input type="email" name="email" placeholder="Your email" required style="width:100%;box-sizing:border-box;padding:10px;margin-bottom:10px;border:1px solid #ccc;border-radius:4px;font-size:14px;">
<input type="tel" name="phone" placeholder="Phone (optional)" style="width:100%;box-sizing:border-box;padding:10px;margin-bottom:10px;border:1px solid #ccc;border-radius:4px;font-size:14px;">
<textarea name="message" placeholder="Anything else? (optional)" rows="3" style="width:100%;box-sizing:border-box;padding:10px;margin-bottom:10px;border:1px solid #ccc;border-radius:4px;font-size:14px;resize:vertical;"></textarea>
<button type="submit" style="width:100%;padding:12px;background:#16181a;color:#f2efe9;border:none;border-radius:4px;font-size:14px;font-weight:600;cursor:pointer;">Send request</button>
</form>
<p id="sitescout-form-status" style="display:none;margin:12px 0 0;font-size:14px;"></p>
</div>
</div>
<script>
(function () {
  // The banner is position:fixed (deliberately — it must stay on top of
  // arbitrary template CSS this script has never seen), which takes it out
  // of document flow entirely. Nothing pushes the template's own header
  // down to make room, so the two render stacked on top of each other at
  // top:0. Found live 2026-08-27 on the barber-shops template. Measuring
  // the banner's actual rendered height (rather than a fixed padding
  // guess) also survives a business name long enough to wrap the banner
  // onto two lines.
  var banner = document.getElementById("sitescout-demo-banner");
  if (banner) {
    document.body.style.paddingTop = banner.offsetHeight + "px";
  }
  var btn = document.getElementById("sitescout-purchase-btn");
  var modal = document.getElementById("sitescout-purchase-modal");
  var closeBtn = document.getElementById("sitescout-modal-close");
  var form = document.getElementById("sitescout-purchase-form");
  var status = document.getElementById("sitescout-form-status");
  function open() { modal.style.display = "flex"; }
  function close() { modal.style.display = "none"; }
  btn.addEventListener("click", open);
  closeBtn.addEventListener("click", close);
  modal.addEventListener("click", function (e) { if (e.target === modal) close(); });
  form.addEventListener("submit", function (e) {
    e.preventDefault();
    var data = new FormData(form);
    data.append("access_key", "__WEB3FORMS_ACCESS_KEY__");
    data.append("subject", "Purchase interest: __BUSINESS_NAME_JS__");
    fetch("https://api.web3forms.com/submit", {
      method: "POST", body: data, headers: { Accept: "application/json" },
    })
      .then(function (r) { return r.json(); })
      .then(function (res) {
        form.style.display = "none";
        status.style.display = "block";
        status.textContent = res.success
          ? "Thanks! We'll be in touch soon."
          : "Something went wrong — please try again.";
      })
      .catch(function () {
        form.style.display = "none";
        status.style.display = "block";
        status.textContent = "Something went wrong — please try again.";
      });
  });
})();
</script>
"""


def build_purchase_banner(business_name, web3forms_access_key=WEB3FORMS_ACCESS_KEY):
    """A fixed, always-on-top bar stamped onto every ghost site at build
    time (Cyril's call, 2026-08-14), with a "Click here to purchase"
    button that opens an in-page modal (Cyril's call, 2026-08-16) rather
    than navigating away — a name/email/phone/message form that POSTs
    straight from the browser to Web3Forms (no backend of ours needed),
    which emails Cyril. Interim until a real Razorpay checkout replaces
    it. Auto-injected rather than something Cyril adds by hand per
    template, so it can never be forgotten and applies retroactively to
    any future template with zero extra work. Inline-styled and
    namespaced deliberately, since this gets dropped into arbitrary
    template CSS this script has never seen."""
    escaped_name_html = html.escape(business_name or "this business")
    # JSON string escaping doubles as safe JS string escaping, EXCEPT for
    # one thing JSON doesn't need to care about: a literal "</script>"
    # substring would close the real <script> tag this ends up embedded
    # in, regardless of any JS-level quoting — the HTML parser sees it
    # before the JS parser ever runs. Escaping the slash defuses that.
    escaped_name_js = json.dumps(business_name or "this business")[1:-1].replace("</", "<\\/")
    return (
        _PURCHASE_BANNER_TEMPLATE
        .replace("__BUSINESS_NAME_HTML__", escaped_name_html)
        .replace("__BUSINESS_NAME_JS__", escaped_name_js)
        .replace("__WEB3FORMS_ACCESS_KEY__", web3forms_access_key or "")
    )


def inject_purchase_banner(page_html, business_name):
    """Inserts the banner+modal right after the opening <body> tag. Falls
    back to prepending it if no <body> tag is found (shouldn't happen
    for a real template, but never silently skip it over a malformed
    document)."""
    banner = build_purchase_banner(business_name)
    match = re.search(r"<body[^>]*>", page_html, re.IGNORECASE)
    if not match:
        return banner + page_html
    insert_at = match.end()
    return page_html[:insert_at] + banner + page_html[insert_at:]


def slugify(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def project_slug_for(page_id, business_name):
    base = slugify(business_name)[:40].strip("-") or "site"
    suffix = re.sub(r"[^a-f0-9]", "", page_id.lower())[:8] or "00000000"
    return f"{base}-{suffix}"


def get_buildable_leads():
    """Selected leads with no Ghost Site URL yet."""
    url = f"https://api.notion.com/v1/data_sources/{NOTION_DATA_SOURCE_ID}/query"
    headers = {
        "Authorization": f"Bearer {NOTION_API_KEY}",
        "Notion-Version": "2025-09-03",
        "Content-Type": "application/json",
    }
    body = {
        "filter": {
            "and": [
                {"property": "Ghost Site Status", "select": {"equals": "Selected"}},
                {"property": "Ghost Site URL", "url": {"is_empty": True}},
            ]
        }
    }
    leads = []
    cursor = None
    has_more = True
    while has_more:
        if cursor:
            body["start_cursor"] = cursor
        resp = requests.post(url, headers=headers, json=body)
        if resp.status_code != 200:
            print(f"Notion query error: {resp.status_code} {resp.text[:200]}")
            break
        data = resp.json()
        for page in data.get("results", []):
            props = page.get("properties", {})
            title = props.get("Business Name", {}).get("title", [])
            loc = props.get("Source Niche/Location", {}).get("rich_text", [])
            leads.append({
                "page_id": page["id"],
                "name": title[0]["text"]["content"] if title else "Unknown",
                "source_niche_location": loc[0]["text"]["content"] if loc else "",
            })
        has_more = data.get("has_more", False)
        cursor = data.get("next_cursor")
    return leads


def write_ghost_site_url(page_id, url_value):
    url = f"https://api.notion.com/v1/pages/{page_id}"
    headers = {
        "Authorization": f"Bearer {NOTION_API_KEY}",
        "Notion-Version": "2025-09-03",
        "Content-Type": "application/json",
    }
    resp = requests.patch(url, headers=headers, json={
        "properties": {"Ghost Site URL": {"url": url_value}}
    })
    return resp.status_code == 200


def load_template_html(template_path):
    with open(template_path) as f:
        return f.read()


def load_template_css(css_path):
    if not css_path or not os.path.exists(css_path):
        return None
    with open(css_path, "rb") as f:
        return f.read()


def build_one(lead, template_html, css_bytes, run_calls):
    page_id = lead["page_id"]
    name = lead["name"]

    data = lead_data.build_lead_json(page_id)
    if data is None:
        print(f"  {name} -> SKIPPED (could not fetch lead page)")
        run_calls.append(("fetch_failed", page_id, False))
        return

    final_html = inject_asset_references(template_html, has_css=css_bytes is not None)
    final_html = inject_purchase_banner(final_html, data.get("business_name", name))

    project_slug = project_slug_for(page_id, name)
    subdomain = f"{project_slug}.{ROOT_DOMAIN}"

    deploy_files = [
        ("index.html", final_html.encode()),
        ("render.js", load_render_js().encode()),
        ("lead.json", json.dumps(data).encode()),
    ]
    if css_bytes is not None:
        deploy_files.append(("style.css", css_bytes))

    ok, pages_url, err = cf.deploy_site(project_slug, deploy_files)
    if not ok:
        print(f"  {name} -> DEPLOY FAILED: {err}")
        run_calls.append(("deploy_failed", page_id, False))
        return

    ok, err = cf.attach_custom_domain(project_slug, subdomain)
    if not ok:
        print(f"  {name} -> DOMAIN ATTACH FAILED: {err[:200]}")
        run_calls.append(("domain_attach_failed", page_id, False))
        return

    ok, err = cf.create_dns_cname(subdomain, f"{project_slug}.pages.dev")
    if not ok:
        print(f"  {name} -> DNS CREATE FAILED: {err[:200]}")
        run_calls.append(("dns_failed", page_id, False))
        return

    final_url = f"https://{subdomain}"
    ok = write_ghost_site_url(page_id, final_url)
    if not ok:
        print(f"  {name} -> built {final_url} but FAILED to write to Notion")
        run_calls.append(("notion_write_failed", page_id, False))
        return

    print(f"  {name} -> {final_url} (pages.dev: {pages_url})")
    run_calls.append(("built", page_id, True))


def main():
    state = load_state()
    approved_niches = {niche: entry for niche, entry in state.items() if entry["status"] == "approved"}

    leads = get_buildable_leads()
    print(f"Found {len(leads)} Selected leads with no Ghost Site URL yet.")
    print(f"Approved templates for: {', '.join(approved_niches) if approved_niches else '(none)'}\n")

    template_cache = {}
    run_calls = []
    built = 0

    for lead in leads:
        niche = extract_niche(lead["source_niche_location"])
        if not niche or niche not in approved_niches:
            continue

        if niche not in template_cache:
            template_path = approved_niches[niche].get("template_path")
            if not template_path or not os.path.exists(template_path):
                print(f"  {lead['name']} -> SKIPPED (no template file on disk for {niche})")
                continue
            html = load_template_html(template_path)
            css = load_template_css(approved_niches[niche].get("css_path"))
            template_cache[niche] = (html, css)

        template_html, css_bytes = template_cache[niche]
        build_one(lead, template_html, css_bytes, run_calls)
        if run_calls and run_calls[-1][2]:
            built += 1

    print(f"\n--- Summary ---\nSites built: {built}")
    return {"notes": f"built={built} actions={run_calls}"}


if __name__ == "__main__":
    import hp_runlog
    hp_runlog.run_wrapped("ghost_site_builder", main)
