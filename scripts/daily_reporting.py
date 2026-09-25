#!/usr/bin/env python3
"""
Daily Reporting — SiteScout
Usage: python3 daily_reporting.py

Morning summary email covering yesterday's pipeline activity: new leads
emailed, follow-ups sent by stage, and replies needing review (Interested
only — Not Interested/Unsubscribed are already fully handled by Sprint 6's
classification, nothing left to review). Also includes Track A's window
onto Track B: previews activated this week and conversions this week
(Gap 7) — both genuinely week-scoped now, reading real Preview Sent
Date / Converted At timestamps.

No Notion dashboard write (Cyril's call, 2026-08-12) — Sprint 13 builds the
real dashboard later; an interim Notion structure now would likely just get
thrown away.
"""

import os
import subprocess
from datetime import date, timedelta

import requests

import hp_env
import hp_config

NOTION_API_KEY = hp_env.NOTION_API_KEY
NOTION_DATA_SOURCE_ID = hp_env.NOTION_DATA_SOURCE_ID
RECIPIENT_ADDRESS = hp_config.OPERATOR_EMAIL

# Contact Status a lead lands on right after each kind of send — the only
# path into "Follow 2" is a successful follow-up #1 send, etc. Same
# proxy-counting pattern as orchestrator.py's count_todays_new_sends().
FOLLOWUP_STAGE_STATUSES = {
    1: "Follow 2",
    2: "Follow 3",
    3: "Follow 4",
    4: "Lead Lost",
}
NEW_LEAD_STATUS = "Follow 1"


def send_email(to_address, subject, body):
    message = f"From: {RECIPIENT_ADDRESS}\nTo: {to_address}\nSubject: {subject}\n\n{body}\n"
    result = subprocess.run(
        ["himalaya", "message", "send"],
        input=message, text=True, capture_output=True,
    )
    return result.returncode == 0, result.stderr


def _query_count(filter_dict):
    url = f"https://api.notion.com/v1/data_sources/{NOTION_DATA_SOURCE_ID}/query"
    headers = {
        "Authorization": f"Bearer {NOTION_API_KEY}",
        "Notion-Version": "2025-09-03",
        "Content-Type": "application/json",
    }
    total = 0
    cursor = None
    has_more = True
    while has_more:
        body = {"filter": filter_dict}
        if cursor:
            body["start_cursor"] = cursor
        resp = requests.post(url, headers=headers, json=body)
        if resp.status_code != 200:
            print(f"Notion query error: {resp.status_code} {resp.text[:200]}")
            return total
        data = resp.json()
        total += len(data.get("results", []))
        has_more = data.get("has_more", False)
        cursor = data.get("next_cursor")
    return total


def count_status_on_date(status, target_date):
    return _query_count({
        "and": [
            {"property": "Contact Status", "select": {"equals": status}},
            {"property": "Last Contact Date", "date": {"equals": target_date.isoformat()}},
        ]
    })


def count_interested_replies_on_date(target_date):
    return _query_count({
        "and": [
            {"property": "Reply Status", "select": {"equals": "Interested"}},
            {"property": "Reply Received Date", "date": {"equals": target_date.isoformat()}},
        ]
    })


def count_previews_activated_since(since_date):
    return _query_count({
        "property": "Preview Sent Date", "date": {"on_or_after": since_date.isoformat()},
    })


def count_conversions_since(since_date):
    # Real week-scoped count, not a lifetime snapshot — Converted At (added
    # alongside the dashboard's "Mark Converted" button) gives conversions
    # the same real timestamp Preview Sent Date already gives previews.
    return _query_count({
        "property": "Converted At", "date": {"on_or_after": since_date.isoformat()},
    })


def build_report(target_date):
    followups = {n: count_status_on_date(status, target_date) for n, status in FOLLOWUP_STAGE_STATUSES.items()}
    week_start = target_date - timedelta(days=6)  # 7-day window ending on target_date, inclusive
    return {
        "date": target_date,
        "sent": count_status_on_date(NEW_LEAD_STATUS, target_date),
        "followups": followups,
        "replies_needing_review": count_interested_replies_on_date(target_date),
        "previews_activated_this_week": count_previews_activated_since(week_start),
        "conversions_this_week": count_conversions_since(week_start),
    }


def format_report(report):
    d = report["date"].strftime("%B %-d, %Y")
    lines = [
        f"Daily Pipeline Report — {d}",
        "",
        f"New leads emailed: {report['sent']}",
    ]
    for n in (1, 2, 3, 4):
        lines.append(f"Follow-up {n}: {report['followups'][n]}")
    lines.append(f"Replies needing review (Interested): {report['replies_needing_review']}")
    lines.append("")
    lines.append("Track B (this week):")
    lines.append(f"Previews activated: {report['previews_activated_this_week']}")
    lines.append(f"Conversions: {report['conversions_this_week']}")
    return "\n".join(lines)


def main():
    yesterday = date.today() - timedelta(days=1)
    report = build_report(yesterday)
    body = format_report(report)
    subject = f"Daily Pipeline Report — {yesterday.isoformat()}"

    print(body)

    success, err = send_email(hp_env.resolve_recipient(RECIPIENT_ADDRESS), subject, body)
    if success:
        print("\nReport email sent.")
    else:
        print(f"\nFailed to send report email: {err}")

    return {
        "emails_sent": 1 if success else 0,  # this script's own send, not yesterday's pipeline totals
        "notes": f"report for {yesterday.isoformat()}: sent={report['sent']}, "
                 f"followups={sum(report['followups'].values())}, "
                 f"replies_needing_review={report['replies_needing_review']}",
    }


if __name__ == "__main__":
    import hp_runlog
    hp_runlog.run_wrapped("daily_reporting", main)
