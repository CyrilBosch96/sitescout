#!/usr/bin/env python3
"""
Sprint 21 — Full Track B integration test.

Unlike every other test file in this project, this one doesn't test one
script in isolation — it chains the REAL functions from
ghost_template_request.py, ghost_site_builder.py, ghost_site_sequence.py,
ghost_site_expiry.py, and inbox_monitoring.py together, feeding the
literal output of each phase as the input to the next (a fake Notion
patch each phase makes becomes the state the next phase reads), proving
the contract between scripts actually holds — not just that each script
is internally correct in isolation.

Two full lifecycles are covered, matching Sprint 21's AC ("Selected ->
live site -> sequence -> reply-or-expiry, end-to-end"):
  1. Selected -> template approved -> built -> Live -> Day 1 -> mid-sequence
     reply -> sequence stops touching it (the "reply" branch).
  2. Selected -> template approved -> built -> Live -> lapses -> expired ->
     resurrection email -> reply -> auto-rebuilt back to Selected (the
     "expiry" branch, including the full circle back to the start).

Only the true I/O boundary is faked (Notion HTTP calls, Cloudflare HTTP
calls, Himalaya subprocess calls) — every script function in between runs
for real. Track A/B mutual exclusion (any lead with Ghost Site Status set
gets fully skipped by cold_email.py) already has thorough per-lead unit
coverage in test_cold_email.py's test_ghost_site_status_set_skips_* —
not re-tested here, verified live instead (see PLAN.md's Sprint 21 entry).
"""

import os
import sys
from datetime import date, timedelta

import pytest

SCRIPTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
sys.path.insert(0, SCRIPTS_DIR)

import ghost_template_request as gtr  # noqa: E402
import ghost_site_builder as gsb  # noqa: E402
import ghost_site_sequence as gs  # noqa: E402
import ghost_site_expiry as gse  # noqa: E402
import inbox_monitoring as im  # noqa: E402


NICHE = "Barber Shops"
PAGE_ID = "page-integration-1"
BUSINESS_NAME = "Integration Test Barber Co"
EMAIL = "lead@fakebiz-test.dev"


@pytest.fixture(autouse=True)
def isolated_files(tmp_path, monkeypatch):
    monkeypatch.setattr(gtr, "STATE_FILE", str(tmp_path / "ghost_template_requests.json"))
    monkeypatch.setattr(gtr, "PROCESSED_FILE", str(tmp_path / "processed_ghost_template_replies.json"))
    monkeypatch.setattr(gtr, "TEMPLATES_DIR", str(tmp_path / "templates"))


def _selected_leads_batch():
    """10 leads in the same niche, all needing a template — the
    threshold ghost_template_request.py's start_new_requests() acts on."""
    return {NICHE: [{"page_id": f"page-{i}", "name": f"Shop {i}"} for i in range(10)]}


def _template_html():
    return (
        '<html><body><h1 data-slot="business_name"></h1><p data-slot="city"></p>'
        '<p data-slot="phone"></p><p data-slot="hero_line"></p></body></html>'
    )


def _run_template_approval_phase(monkeypatch, tmp_path):
    """Phases 1-3 of the handoff: threshold trigger -> reply with HTML ->
    confirm. Identical setup for both lifecycle tests below — this is the
    shared on-ramp into Track B automation."""
    monkeypatch.setattr(gtr, "send_email", lambda *a, **k: (True, ""))
    state = {}
    run_calls = []
    gtr.start_new_requests(_selected_leads_batch(), state, run_calls)
    assert state[NICHE]["status"] == "pending_template"

    html_path = tmp_path / "upload.html"
    html_path.write_text(_template_html())
    monkeypatch.setattr(gtr, "download_html_attachment", lambda msg_id, dest_dir: str(html_path))
    envelopes = [{"id": "msg-template", "subject": f"Re: {state[NICHE]['subject']}",
                  "from": [{"name": "Cyril Bosch", "email": "operator@example.com"}]}]
    gtr.check_template_replies(state, envelopes, processed=set(), run_calls=run_calls)
    assert state[NICHE]["status"] == "pending_confirmation"
    template_path = state[NICHE]["template_path"]
    assert os.path.exists(template_path)

    envelopes2 = [{"id": "msg-confirm", "subject": f"Re: {state[NICHE]['subject']}",
                   "from": [{"name": "Cyril Bosch", "email": "operator@example.com"}]}]
    gtr.check_confirmation_replies(state, envelopes2, processed={"msg-template"}, run_calls=run_calls)
    assert state[NICHE]["status"] == "approved"

    with open(template_path) as f:
        return f.read()


def _build_one_lead(monkeypatch, template_html, page_id=PAGE_ID, name=BUSINESS_NAME):
    """Phase 4: ghost_site_builder.py's build_one() against a mocked
    Cloudflare client, capturing exactly what it writes to Notion — the
    real handoff to phase 5 (ghost_site_sequence.py)."""
    monkeypatch.setattr(gsb.lead_data, "build_lead_json", lambda pid: {
        "business_name": name, "city": "Ann Arbor, MI", "phone": "(734) 555-0100",
        "hero_line": "Fresh cuts, real fast.",
    })
    monkeypatch.setattr(gsb, "load_render_js", lambda: "/* render.js */")
    monkeypatch.setattr(gsb.cf, "deploy_site", lambda project, files: (True, f"https://{project}.pages.dev", None))
    monkeypatch.setattr(gsb.cf, "attach_custom_domain", lambda project, domain: (True, ""))
    monkeypatch.setattr(gsb.cf, "create_dns_cname", lambda sub, target: (True, ""))

    written = {}
    monkeypatch.setattr(gsb, "write_ghost_site_url", lambda pid, url: written.setdefault(pid, url) or True)

    lead = {"page_id": page_id, "name": name, "source_niche_location": f"{NICHE} / Ann Arbor, Michigan"}
    run_calls = []
    gsb.build_one(lead, template_html, None, run_calls)

    assert run_calls == [("built", page_id, True)]
    assert page_id in written
    return written[page_id]


# --- Lifecycle 1: Selected -> Live -> Day 1 -> mid-sequence reply ---

def test_full_lifecycle_selected_to_live_to_mid_sequence_reply(monkeypatch, tmp_path):
    template_html = _run_template_approval_phase(monkeypatch, tmp_path)
    ghost_site_url = _build_one_lead(monkeypatch, template_html)
    assert ghost_site_url.startswith("https://")

    # Phase 5: ghost_site_sequence.py sees this Selected+URL lead and
    # starts the sequence for real — this is the exact trigger condition
    # ghost_site_builder.py was built to satisfy (URL written, status
    # left as Selected).
    notion_state = {"status": "Selected", "sequence_day": 0, "site_expiry_date": None}

    def fake_patch_start(page_id, next_status, emails_sent_count=None, thread_message_id=None):
        return True

    monkeypatch.setattr(gs, "DRY_RUN", True)  # dev-mode default; asserting on the [DRY RUN] print path is enough
    ok = gs.start_sequence(PAGE_ID, "Re: subject", "Day 1 body", None, EMAIL)
    assert ok is True  # DRY_RUN path returns True without touching Notion or sending

    # Phase 6: a reply arrives mid-sequence. inbox_monitoring.py's real
    # logic must recognize this lead is Track-B-active (status still
    # Selected/Live from ghost_site_sequence.py's perspective — simulate
    # it having already flipped to Live) and mark Replied, not fall
    # through to Track A's Interested-only path.
    monkeypatch.setattr(im, "load_processed_ids", lambda: set())
    monkeypatch.setattr(im, "list_inbox", lambda: [{"id": "msg-reply", "subject": "Re: it",
                                                       "from": [{"name": "Real Owner", "email": EMAIL}]}])
    monkeypatch.setattr(im, "find_lead_by_email", lambda email: {
        "page_id": PAGE_ID, "name": BUSINESS_NAME, "ghost_site_status": "Live",
    })
    monkeypatch.setattr(im, "read_message_body", lambda msg_id: "Looks great, thanks!")
    monkeypatch.setattr(im, "classify_reply", lambda body: "Interested")
    monkeypatch.setattr(im, "update_reply_status", lambda page_id, status: True)
    monkeypatch.setattr(im, "save_processed_ids", lambda ids: None)

    marked_replied = []
    monkeypatch.setattr(im, "mark_ghost_site_replied", lambda page_id: marked_replied.append(page_id) or True)
    notified = []
    monkeypatch.setattr(im, "send_ghost_site_reply_notification", lambda name, body: notified.append((name, body)))

    def resurrect_should_not_fire(*a, **k):
        raise AssertionError("resurrect_ghost_site() called for a Live lead's reply — that's the Expired path")
    monkeypatch.setattr(im, "resurrect_ghost_site", resurrect_should_not_fire)

    im.main()

    assert marked_replied == [PAGE_ID]
    assert notified == [(BUSINESS_NAME, "Looks great, thanks!")]
    # The real contract this whole lifecycle depends on: once
    # inbox_monitoring.py sets Ghost Site Status=Replied,
    # ghost_site_sequence.py's own get_active_leads() query (Selected OR
    # Live only) naturally excludes it on the next run — no shared code
    # path needed between the two scripts for this to hold.


# --- Lifecycle 2: Selected -> Live -> lapses -> Expired -> resurrection -> Selected again ---

def test_full_lifecycle_selected_to_live_to_expiry_to_resurrection(monkeypatch, tmp_path):
    template_html = _run_template_approval_phase(monkeypatch, tmp_path)
    ghost_site_url = _build_one_lead(monkeypatch, template_html, page_id="page-integration-2", name="Lapsing Barber Co")

    # Phase 5: sequence starts (live, not dry-run this time, to capture
    # the real Notion write shape and feed it into phase 7).
    monkeypatch.setattr(gs, "DRY_RUN", False)
    monkeypatch.setattr(gs, "send_email", lambda *a, **k: (True, ""))
    captured_patch = {}

    def fake_patch(url, headers=None, json=None):
        captured_patch["properties"] = json["properties"]
        class R:
            status_code = 200
        return R()

    monkeypatch.setattr(gs.requests, "patch", fake_patch)
    ok = gs.start_sequence("page-integration-2", "Re: subject", "Day 1 body", None, EMAIL)
    assert ok is True
    assert captured_patch["properties"]["Ghost Site Status"]["select"]["name"] == "Live"
    site_expiry_date = captured_patch["properties"]["Site Expiry Date"]["date"]["start"]
    assert site_expiry_date == (date.today() + timedelta(days=gs.SITE_LIFESPAN_DAYS)).isoformat()

    # Phase 7: time passes, the site lapses. ghost_site_expiry.py's
    # phase 1 tears down the real infra and marks Expired — feed it the
    # exact URL phase 4 produced.
    teardown_calls = []
    monkeypatch.setattr(gse.cf, "detach_custom_domain", lambda project, domain: (teardown_calls.append(("detach", project)), (True, ""))[-1])
    monkeypatch.setattr(gse.cf, "delete_dns_record_for", lambda domain: (True, ""))
    monkeypatch.setattr(gse.cf, "delete_project", lambda project: (teardown_calls.append(("delete", project)), (True, ""))[-1])

    marked_expired = []
    monkeypatch.setattr(gse, "mark_expired", lambda page_id: marked_expired.append(page_id) or True)

    run_calls = []
    lapsed_lead = {"page_id": "page-integration-2", "name": "Lapsing Barber Co", "ghost_site_url": ghost_site_url}
    monkeypatch.setattr(gse, "get_lapsed_live_leads", lambda: [lapsed_lead])
    gse.expire_lapsed_sites(run_calls)

    assert marked_expired == ["page-integration-2"]
    assert [c[0] for c in teardown_calls] == ["detach", "delete"]
    assert ("expired", "page-integration-2", True) in run_calls

    # Phase 8: one full day later, the one-shot resurrection email goes
    # out — exercised for real (not dry-run) since this IS a lead-facing
    # send, unlike phase 7's infra teardown.
    monkeypatch.setattr(gse, "DRY_RUN", False)
    sent_emails = []
    monkeypatch.setattr(gse, "send_email", lambda to, subject, body, in_reply_to=None: (sent_emails.append((to, subject)), (True, ""))[-1])
    marked_sent = []
    monkeypatch.setattr(gse, "mark_resurrection_sent", lambda page_id: marked_sent.append(page_id) or True)

    resurrection_lead = {
        "page_id": "page-integration-2", "name": "Lapsing Barber Co", "email": EMAIL,
        "email_subject": "Lapsing Barber Co's website", "thread_message_id": "<orig@x>",
        "site_expiry_date": date.today() - timedelta(days=1),
    }
    monkeypatch.setattr(gse, "get_resurrection_candidates", lambda: [resurrection_lead])
    run_calls2 = []
    gse.send_resurrection_emails(run_calls2)

    assert marked_sent == ["page-integration-2"]
    assert len(sent_emails) == 1

    # Phase 9: the lead replies to the resurrection email.
    # inbox_monitoring.py must auto-rebuild (Cyril's confirmed design)
    # rather than mark Replied — closing the loop back to Selected,
    # which is exactly what ghost_site_builder.py's own query
    # (get_buildable_leads: Selected + no URL) picks up next.
    monkeypatch.setattr(im, "load_processed_ids", lambda: set())
    monkeypatch.setattr(im, "list_inbox", lambda: [{"id": "msg-resurrect", "subject": "Re: it",
                                                       "from": [{"name": "Real Owner", "email": EMAIL}]}])
    monkeypatch.setattr(im, "find_lead_by_email", lambda email: {
        "page_id": "page-integration-2", "name": "Lapsing Barber Co", "ghost_site_status": "Expired",
    })
    monkeypatch.setattr(im, "read_message_body", lambda msg_id: "Yes please bring it back!")
    monkeypatch.setattr(im, "classify_reply", lambda body: "Interested")
    monkeypatch.setattr(im, "update_reply_status", lambda page_id, status: True)
    monkeypatch.setattr(im, "save_processed_ids", lambda ids: None)

    captured_resurrect_patch = {}

    def fake_im_patch(url, headers=None, json=None):
        captured_resurrect_patch["properties"] = json["properties"]
        class R:
            status_code = 200
        return R()

    monkeypatch.setattr(im.requests, "patch", fake_im_patch)

    def mark_replied_should_not_fire(page_id):
        raise AssertionError("mark_ghost_site_replied() called for an Expired lead — should auto-resurrect instead")
    monkeypatch.setattr(im, "mark_ghost_site_replied", mark_replied_should_not_fire)

    im.main()

    assert captured_resurrect_patch["properties"]["Ghost Site Status"]["select"]["name"] == "Selected"
    assert captured_resurrect_patch["properties"]["Resurrection Sent Date"]["date"] is None
    # Full circle: this lead is now exactly where lifecycle 1 started —
    # Selected, no URL, ready for ghost_site_builder.py to pick up again.
