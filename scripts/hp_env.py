#!/usr/bin/env python3
"""
Shared environment/config gating for SiteScout scripts — dev vs production.

Every script that touches Notion or sends email imports this instead of
reading os.environ directly. HP_ENV controls three things, everywhere:

  1. Which Notion database gets read/written (NOTION_DATA_SOURCE_ID)
  2. What address an email actually reaches (resolve_recipient)
  3. Whether --live has any effect at all (live_allowed)

Default is "development" — an unset HP_ENV is locked/safe, never accidentally
production. Loads secrets from a .env file in the project root via
python-dotenv, so scripts run correctly whether launched by hand or by
launchd (which injects env vars directly and doesn't need the .env file).
"""

import os

from dotenv import load_dotenv

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

load_dotenv(os.path.join(PROJECT_ROOT, ".env"))

HP_ENV = os.environ.get("HP_ENV", "development")
IS_PRODUCTION = HP_ENV == "production"

NOTION_API_KEY = os.environ.get("NOTION_API_KEY")

_PROD_DATA_SOURCE_ID = os.environ.get("NOTION_DATA_SOURCE_ID_PROD")
_TEST_DATA_SOURCE_ID = os.environ.get("NOTION_DATA_SOURCE_ID_TEST")
NOTION_DATA_SOURCE_ID = _PROD_DATA_SOURCE_ID if IS_PRODUCTION else _TEST_DATA_SOURCE_ID

# Distinct from NOTION_DATA_SOURCE_ID: the Notion API wants the *database*
# page ID (not the data-source/collection ID) as parent.database_id when
# creating new pages. Querying uses the data source ID; creating uses this.
_PROD_DATABASE_ID = os.environ.get("NOTION_DATABASE_ID_PROD")
_TEST_DATABASE_ID = os.environ.get("NOTION_DATABASE_ID_TEST")
NOTION_DATABASE_ID = _PROD_DATABASE_ID if IS_PRODUCTION else _TEST_DATABASE_ID

DEV_OVERRIDE_EMAIL = os.environ.get("DEV_OVERRIDE_EMAIL", "operator@example.com")

if not NOTION_API_KEY:
    raise SystemExit("ERROR: Missing NOTION_API_KEY in environment.")

if not NOTION_DATA_SOURCE_ID:
    _missing_key = "NOTION_DATA_SOURCE_ID_PROD" if IS_PRODUCTION else "NOTION_DATA_SOURCE_ID_TEST"
    raise SystemExit(
        f"ERROR: Missing Notion data source ID for HP_ENV={HP_ENV!r} "
        f"(need {_missing_key} in environment)."
    )

if not NOTION_DATABASE_ID:
    _missing_key = "NOTION_DATABASE_ID_PROD" if IS_PRODUCTION else "NOTION_DATABASE_ID_TEST"
    raise SystemExit(
        f"ERROR: Missing Notion database ID for HP_ENV={HP_ENV!r} "
        f"(need {_missing_key} in environment)."
    )


def resolve_recipient(real_email):
    """
    Dev/test mode: every send is redirected here regardless of the lead's
    real address — a second, independent line of defense on top of
    live_allowed(), not something that only matters when a live send would
    otherwise be reachable.
    """
    return real_email if IS_PRODUCTION else DEV_OVERRIDE_EMAIL


def live_allowed(cli_live_flag):
    """
    --live only has effect in production. Outside production it is always
    locked/ignored, no matter what the caller passes.
    """
    return IS_PRODUCTION and cli_live_flag
