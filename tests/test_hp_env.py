#!/usr/bin/env python3
"""
Tests for scripts/hp_env.py — the Sprint 0 dev/production gating module.

Proves the AC from Part 3, Sprint 0 of the rebuild plan:
  - HP_ENV != production redirects every send to Cyril's own address,
    regardless of the lead's real address.
  - HP_ENV != production locks --live, no matter what's passed.
  - HP_ENV != production reads/writes only the TEST Notion database.

Runs hp_env.py as a real subprocess per case (not an in-process import +
monkeypatch) because the module does real work at import time — reading
HP_ENV, selecting a database ID, validating required keys are present —
and a subprocess is the only way to test different environments cleanly
without Python's module-cache getting in the way.

One quirk to know before touching these tests: hp_env.py always resolves
its own PROJECT_ROOT from its real file location, so it always loads the
*real* dev .env file — which has real values for everything. To test a
"missing key" case, don't remove the key from the subprocess's env (that
lets the real .env silently fill it back in, defeating the test); set it
to an empty string instead. python-dotenv's load_dotenv() only skips keys
that are already present in the environment, regardless of their value,
so an explicit empty string reliably wins over the .env file.
"""

import os
import subprocess
import sys

SCRIPTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")

BASE_ENV = {
    "PATH": os.environ.get("PATH", ""),
    "NOTION_API_KEY": "test-notion-key",
    "NOTION_DATABASE_ID_PROD": "prod-db-id",
    "NOTION_DATA_SOURCE_ID_PROD": "prod-ds-id",
    "NOTION_DATABASE_ID_TEST": "test-db-id",
    "NOTION_DATA_SOURCE_ID_TEST": "test-ds-id",
    "DEV_OVERRIDE_EMAIL": "operator@example.com",
}


def run_hp_env(snippet, extra_env=None, blank=None):
    """
    Run `import hp_env` + snippet as a subprocess with a fully controlled
    environment. `blank` is a list of keys to force to an empty string
    (simulating "missing" safely — see module docstring).
    """
    env = dict(BASE_ENV)
    if extra_env:
        env.update(extra_env)
    for key in blank or []:
        env[key] = ""
    code = "import hp_env\n" + snippet
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=SCRIPTS_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )


# --- HP_ENV defaulting ---

def test_unset_hp_env_defaults_to_development_not_production():
    """Regression, found 2026-08-17 promoting to production: passing no
    HP_ENV at all in `extra_env` doesn't actually test the "nothing sets
    it anywhere" case — this file's own real .env always declares HP_ENV
    explicitly (development in dev, production in prod), and dotenv only
    skips keys already present in the subprocess's environment, so the
    real .env's value silently wins regardless. This test passed by pure
    coincidence in dev (its declared value happened to match the
    fallback being tested) and would have failed identically here had
    production's promotion happened before this fix — same `blank=`
    pattern this file's docstring already documents for exactly this
    problem. Checks the safety property that actually matters (never
    silently defaults to production) rather than the exact display
    string, since an explicit empty string is a stand-in for "truly
    absent", not a real value someone would want to see displayed."""
    result = run_hp_env("print(hp_env.HP_ENV); print(hp_env.IS_PRODUCTION)", blank=["HP_ENV"])
    assert result.returncode == 0, result.stderr
    # Not .strip().splitlines() — HP_ENV prints as an empty string here
    # (that's the whole point), and .strip() on the full stdout would
    # silently eat that leading blank line before it's ever split.
    lines = result.stdout.splitlines()
    assert lines[0] != "production"
    assert lines[1] == "False"


# --- Database selection ---

def test_development_reads_test_database():
    result = run_hp_env(
        "print(hp_env.NOTION_DATA_SOURCE_ID); print(hp_env.NOTION_DATABASE_ID)",
        extra_env={"HP_ENV": "development"},
    )
    assert result.returncode == 0, result.stderr
    lines = result.stdout.strip().splitlines()
    assert lines[0] == "test-ds-id"
    assert lines[1] == "test-db-id"


def test_production_reads_production_database():
    result = run_hp_env(
        "print(hp_env.NOTION_DATA_SOURCE_ID); print(hp_env.NOTION_DATABASE_ID)",
        extra_env={"HP_ENV": "production"},
    )
    assert result.returncode == 0, result.stderr
    lines = result.stdout.strip().splitlines()
    assert lines[0] == "prod-ds-id"
    assert lines[1] == "prod-db-id"


# --- Recipient override (Sprint 0 AC: "recipient is overridden ... regardless
# of the lead's real address") ---

def test_dev_recipient_is_always_overridden_regardless_of_real_address():
    result = run_hp_env(
        "print(hp_env.resolve_recipient('realowner@somebarbershop.com'))",
        extra_env={"HP_ENV": "development"},
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "operator@example.com"


def test_production_recipient_is_the_real_lead_address():
    result = run_hp_env(
        "print(hp_env.resolve_recipient('realowner@somebarbershop.com'))",
        extra_env={"HP_ENV": "production"},
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "realowner@somebarbershop.com"


# --- --live locking (Sprint 0 AC + Sprint 5 AC: the single most important
# test in the whole rebuild) ---

def test_live_flag_stays_locked_outside_production_no_matter_what_is_passed():
    non_production_values = ["development", "staging", "PRODUCTION", "Production", "prod"]
    for value in non_production_values:
        result = run_hp_env(
            "print(hp_env.live_allowed(True))",
            extra_env={"HP_ENV": value},
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "False", (
            f"HP_ENV={value!r} must lock --live, but live_allowed(True) returned "
            f"{result.stdout.strip()!r} — only the exact literal 'production' may unlock it."
        )


def test_live_flag_locked_in_production_unless_actually_passed():
    result = run_hp_env(
        "print(hp_env.live_allowed(False))",
        extra_env={"HP_ENV": "production"},
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "False"


def test_live_flag_only_true_when_production_and_flag_both_true():
    result = run_hp_env(
        "print(hp_env.live_allowed(True))",
        extra_env={"HP_ENV": "production"},
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "True"


# --- Fail loudly on missing config, rather than silently misbehaving ---

def test_missing_notion_api_key_fails_loudly_instead_of_running_blind():
    result = run_hp_env("print('should not reach here')", blank=["NOTION_API_KEY"])
    assert result.returncode != 0
    assert "NOTION_API_KEY" in (result.stdout + result.stderr)


def test_missing_test_database_id_fails_loudly_in_dev():
    result = run_hp_env(
        "print('should not reach here')",
        extra_env={"HP_ENV": "development"},
        blank=["NOTION_DATA_SOURCE_ID_TEST"],
    )
    assert result.returncode != 0
    assert "NOTION_DATA_SOURCE_ID_TEST" in (result.stdout + result.stderr)


def test_missing_production_database_id_fails_loudly_in_production():
    result = run_hp_env(
        "print('should not reach here')",
        extra_env={"HP_ENV": "production"},
        blank=["NOTION_DATABASE_ID_PROD"],
    )
    assert result.returncode != 0
    assert "NOTION_DATABASE_ID_PROD" in (result.stdout + result.stderr)
