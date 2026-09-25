#!/usr/bin/env python3
"""
Queue Check — SiteScout
Usage: python3 queue_check.py
"""

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone

import hp_env
import hp_config

STATE_FILE = os.path.join(hp_env.PROJECT_ROOT, "state.json")
PROCESSED_FILE = os.path.join(hp_env.PROJECT_ROOT, "processed_queue_replies.json")
FROM_ADDRESS = hp_config.OPERATOR_EMAIL
ASK_SUBJECT = "What's the next city + niche?"

# Same reminder cadence ghost_template_request.py already uses. Found live
# 2026-08-28: the "no reply found -> send ask email" branch had no cooldown
# at all, so every 10-minute cron cycle overnight (no reply to catch it on)
# resent the exact same ask email — 79 copies in one night.
# 9 minutes, not 10: the orchestrator cron fires every 10 min with a few
# seconds of jitter, and a strict 10-minute gate would sometimes skip a
# cycle and effectively ask every 20. Cyril's call (2026-09-19): keep
# asking every cycle, around the clock, until he replies with the next
# city — the earlier 12h cooldown existed because the old bug sent 79
# copies in one night, but the cooldown itself is what made a stalled
# list go unnoticed for a day.
REMINDER_INTERVAL_HOURS = 9 / 60


def hours_since(iso_timestamp):
    if not iso_timestamp:
        return REMINDER_INTERVAL_HOURS + 1  # never asked before -> due immediately
    then = datetime.fromisoformat(iso_timestamp)
    return (datetime.now(timezone.utc) - then).total_seconds() / 3600


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"active_niche": None, "active_location": None, "exhausted": False, "last_asked_at": None}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def load_processed_ids():
    """
    IDs of replies already handled (successfully parsed or not). Without
    this, a job that exhausts weeks later would find and reprocess the
    *old* reply from last time — nothing else distinguishes it from a
    genuinely new one, since replying doesn't change the email's identity.
    """
    if os.path.exists(PROCESSED_FILE):
        with open(PROCESSED_FILE) as f:
            return set(json.load(f))
    return set()


def save_processed_ids(ids):
    os.makedirs(os.path.dirname(PROCESSED_FILE), exist_ok=True)
    with open(PROCESSED_FILE, "w") as f:
        json.dump(list(ids), f, indent=2)


def send_email(to_address, subject, body):
    message = f"From: {FROM_ADDRESS}\nTo: {to_address}\nSubject: {subject}\n\n{body}\n"
    result = subprocess.run(
        ["himalaya", "message", "send"],
        input=message, text=True, capture_output=True,
    )
    return result.returncode == 0, result.stderr


def get_unread_reply(processed_ids, sent_after=None):
    """Find a genuine, not-yet-handled human reply — has a display name set
    (unlike our own auto-sent messages, which never set one), and its ID
    isn't already in processed_ids from a previous cycle.

    `sent_after` (state["last_asked_at"], parsed) is a second, independent
    check on top of processed_ids — required after a real incident
    (2026-08-18): a reset that clears processed_queue_replies.json (or a
    promotion that never carried it over, an earlier version of the same
    bug) makes every already-answered reply look brand new again, since
    dedup was the ONLY thing distinguishing "new" from "answered weeks
    ago." A week-old reply ("Detroit, Michigan, coffee shops") got
    replayed as if Cyril had just sent it. A reply older than the most
    recent ask we actually sent can never be a genuine answer to that
    ask, regardless of dedup file state — so if we've never sent an ask
    at all (sent_after is None), no existing reply is ever valid; a fresh
    ask must go out first."""
    if sent_after is None:
        return None

    result = subprocess.run(
        ["himalaya", "envelope", "list", "--json"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"Error listing inbox: {result.stderr}")
        return None

    try:
        data = json.loads(result.stdout)
        envelopes = data.get("envelopes", [])
    except Exception as e:
        print(f"Could not parse inbox listing: {e}")
        return None

    for env in envelopes:
        msg_id = env.get("id")
        if str(msg_id) in processed_ids:
            continue
        subject = env.get("subject", "")
        if not ("next city" in subject.lower() and subject.lower().startswith("re:")):
            continue
        from_list = env.get("from", [])
        has_real_name = any(f.get("name") for f in from_list)
        if not has_real_name:
            continue
        env_date = _parse_envelope_date(env.get("date"))
        if env_date is None or env_date <= sent_after:
            continue
        return msg_id
    return None


def _parse_envelope_date(date_str):
    if not date_str:
        return None
    try:
        return datetime.fromisoformat(date_str)
    except ValueError:
        return None


def extract_reply_text(raw_output):
    """Strip MIME headers and quoted original message, return just the reply text."""
    parts = raw_output.split("\n\n", 1)
    body = parts[1] if len(parts) > 1 else raw_output

    # himalaya's `message read` prints a MIME part marker line ("[2]
    # text/plain (192 B)") plus its own indented Content-Type /
    # Content-Transfer-Encoding lines before the actual body, separated
    # from it by another blank line. Found live 2026-08-27: a real reply
    # ("Wichita, Kansas, Hair Salons") was silently rejected as
    # "unparseable" because parse_reply() read this marker line as the
    # reply's first line instead of the real text. Skip past it if present.
    body_lines = body.split("\n")
    if body_lines and re.match(r"^\[\d+\]\s+\S+/\S+", body_lines[0]):
        idx = 1
        while idx < len(body_lines) and body_lines[idx].strip():
            idx += 1
        body = "\n".join(body_lines[idx + 1:])

    lines = []
    for line in body.split("\n"):
        stripped = line.strip()
        if stripped.startswith(">"):
            break
        if re.match(r"^On .+ wrote:$", stripped):
            break
        lines.append(line)
    return "\n".join(lines).strip()


def read_message_body(message_id):
    result = subprocess.run(
        ["himalaya", "message", "read", str(message_id)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"Error reading message: {result.stderr}")
        return None
    return extract_reply_text(result.stdout)


MID_TIER_CITY_SUGGESTIONS = [
    ("Boise", "ID"), ("Chattanooga", "TN"), ("Knoxville", "TN"), ("Greenville", "SC"),
    ("Huntsville", "AL"), ("Spokane", "WA"), ("Des Moines", "IA"), ("Little Rock", "AR"),
    ("Lexington", "KY"), ("Fort Wayne", "IN"), ("Madison", "WI"), ("Colorado Springs", "CO"),
    ("Tucson", "AZ"), ("Albuquerque", "NM"), ("Omaha", "NE"), ("Richmond", "VA"),
    ("Birmingham", "AL"), ("Baton Rouge", "LA"), ("Lubbock", "TX"), ("Rochester", "NY"),
    ("Salt Lake City", "UT"), ("Reno", "NV"), ("Charleston", "SC"), ("Greensboro", "NC"),
]
# Cities already worked, so a suggestion is never one we've already run.
ALREADY_WORKED_CITIES = {
    "wichita", "chandler", "tulsa", "cincinnati", "kalamazoo", "savannah",
    "carmel", "frisco", "los angeles", "detroit", "kansas city", "new york",
}


def suggest_city(state):
    """Next mid-tier US city not yet worked, or None if the list is used up."""
    used = set(ALREADY_WORKED_CITIES)
    for loc in state.get("used_locations") or []:
        used.add(loc.split(",")[0].strip().lower())
    active = state.get("active_location") or ""
    if active:
        used.add(active.split(",")[0].strip().lower())
    for city, st in MID_TIER_CITY_SUGGESTIONS:
        if city.lower() not in used:
            return city, st
    return None


def build_ask_body(state):
    niche = state.get("active_niche") or "Home Inspectors"
    body = (
        "Reply as: City, State, Niche (or Niche, City, State)\n\n"
        "Examples: Wichita, Kansas, Hair Salons  —  or  —  Hair Salons, Wichita, Kansas"
    )
    suggestion = suggest_city(state)
    if suggestion:
        city, st = suggestion
        body += f"\n\nSuggested next city (mid-tier US market): {niche}, {city}, {st}\nJust reply with that line to accept it."
    return body


US_STATES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA",
    "colorado": "CO", "connecticut": "CT", "delaware": "DE", "florida": "FL", "georgia": "GA",
    "hawaii": "HI", "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA",
    "kansas": "KS", "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT", "vermont": "VT",
    "virginia": "VA", "washington": "WA", "west virginia": "WV", "wisconsin": "WI",
    "wyoming": "WY", "district of columbia": "DC",
}
US_STATE_ABBREVIATIONS = set(US_STATES.values())


def _is_us_state(text):
    normalized = text.strip().rstrip(".")
    return normalized.lower() in US_STATES or normalized.upper() in US_STATE_ABBREVIATIONS


def parse_reply(body):
    """3 comma-separated parts, one of which must be a real US state (full
    name or abbreviation) — city name alone is ambiguous (many US cities
    share names across different states), so state is required as an
    anchor.

    Order-tolerant, added 2026-09-07 after a real live incident: the
    original version rigidly required 'City, State, Niche' even though
    every OTHER convention in this project (the Notion 'Source Niche/
    Location' field, config, every conversation with Cyril) puts niche
    first. Cyril naturally replied 'Home inspectors, Tulsa, OK' — the
    old strict parser silently assigned city='Home inspectors',
    state='Tulsa', niche='OK', corrupting state.json and causing two
    days of cold emails to random unrelated Tulsa businesses (a church,
    Walgreens, City Hall...) under a garbled 'OK / Home inspectors,
    Tulsa' niche/location. Locating the state part by content instead of
    position handles both 'City, State, Niche' and 'Niche, City, State'
    (the two orders someone would actually type) without guessing wrong.
    A state found in the first position is genuinely ambiguous between
    the two conventions, so that case is deliberately left unparsed
    rather than guessed."""
    line = body.strip().split("\n")[0].strip()
    parts = [p.strip().rstrip(".") for p in line.split(",")]
    if len(parts) != 3 or not all(parts):
        return None

    state_positions = [i for i, p in enumerate(parts) if _is_us_state(p)]
    if len(state_positions) != 1 or state_positions[0] == 0:
        return None
    state_idx = state_positions[0]

    if state_idx == 1:
        city, state, niche = parts
    else:  # state_idx == 2
        niche, city, state = parts

    return f"{city}, {state}", niche


def main():
    state = load_state()
    low_supply = "--low-supply" in sys.argv

    # --low-supply: orchestrator found fewer fresh leads than the daily
    # send limit even though a job is still active — ask for the next city
    # early (Cyril's call, 2026-09-19) so tomorrow's 10 new emails are never
    # at risk, instead of only asking once a city is fully exhausted.
    if not low_supply and state["active_niche"] and state["active_location"] and not state["exhausted"]:
        print(f"Active job already set: {state['active_niche']} in {state['active_location']}. Nothing to do.")
        return {"notes": f"active job already set: {state['active_niche']} in {state['active_location']}"}

    print("No active job (or previous one exhausted). Checking for a reply...")
    processed = load_processed_ids()
    sent_after = _parse_envelope_date(state.get("last_asked_at"))
    reply_id = get_unread_reply(processed, sent_after=sent_after)

    if reply_id:
        print(f"Found candidate reply: id {reply_id}")
        body = read_message_body(reply_id)
        print(f"Extracted reply text: {body!r}")
        if body is None:
            print("Could not read the reply message.")
            return {"notes": f"found reply {reply_id} but could not read its body"}

        # Mark handled regardless of outcome — a malformed reply shouldn't
        # trigger a fresh "couldn't understand" email on every subsequent
        # run either; the same staleness bug applies to failures too.
        processed.add(str(reply_id))
        save_processed_ids(processed)

        parsed = parse_reply(body)
        if parsed:
            location, niche = parsed
            state["active_niche"] = niche
            state["active_location"] = location
            state["exhausted"] = False
            state.setdefault("used_locations", []).append(location)
            save_state(state)
            print(f"New active job set: {niche} in {location}")
            return {"notes": f"new active job set: {niche} in {location}"}
        else:
            print("Reply found but couldn't parse it. Sending 'couldn't understand' email.")
            body_text = (
                "Couldn't parse that — please resend as: City, State, Niche (or Niche, City, State)\n\n"
                "Examples: Wichita, Kansas, Hair Salons  —  or  —  Hair Salons, Wichita, Kansas"
            )
            success, err = send_email(hp_env.resolve_recipient(FROM_ADDRESS), f"Re: {ASK_SUBJECT}", body_text)
            if not success:
                print(f"Failed to send retry email: {err}")
            return {"notes": "reply found but unparseable, retry email sent" if success else "reply unparseable, retry email FAILED to send"}

    if hours_since(state.get("last_asked_at")) < REMINDER_INTERVAL_HOURS:
        print(f"No reply found, but already asked within the last {REMINDER_INTERVAL_HOURS}h — not resending yet.")
        return {"notes": f"no reply found, ask email not due yet (last asked {state.get('last_asked_at')})"}

    print("No reply found. Sending 'what's next' email.")
    body_text = build_ask_body(state)
    success, err = send_email(hp_env.resolve_recipient(FROM_ADDRESS), ASK_SUBJECT, body_text)
    if success:
        state["last_asked_at"] = datetime.now(timezone.utc).isoformat()
        save_state(state)
        print("Ask email sent.")
        return {"notes": "no active job, ask email sent"}
    else:
        print(f"Failed to send ask email: {err}")
        return {"notes": "no active job, ask email FAILED to send"}


if __name__ == "__main__":
    import hp_runlog
    hp_runlog.run_wrapped("queue_check", main)
