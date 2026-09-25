#!/usr/bin/env python3
"""
Tests for scripts/hp_llm.py — the Gemini API wrapper that replaced direct
local-Ollama calls (2026-08-20), needed for the move to a cloud VM too
small to run a local model.

Covers: generate() returns text on a real-shaped 200, and None (never an
exception) on every failure mode a caller might see — missing API key,
non-200, empty candidates, malformed response, network exception. Callers
(inbox_monitoring.py, lead_discovery_and_evaluation_Contactsearch.py)
depend on "text or None" being the only two possible outcomes.
"""

import os
import sys

import pytest

SCRIPTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
sys.path.insert(0, SCRIPTS_DIR)

import hp_llm  # noqa: E402


@pytest.fixture(autouse=True)
def fake_api_key(monkeypatch):
    monkeypatch.setattr(hp_llm, "GEMINI_API_KEY", "fake-test-key")


def _gemini_response(text):
    return {"candidates": [{"content": {"parts": [{"text": text}]}}]}


def test_generate_returns_text_on_success(monkeypatch):
    class FakeResponse:
        status_code = 200

        def json(self):
            return _gemini_response("Hello world")

    monkeypatch.setattr(hp_llm.requests, "post", lambda url, params=None, json=None, timeout=None: FakeResponse())
    assert hp_llm.generate("say hi") == "Hello world"


def test_generate_strips_whitespace(monkeypatch):
    class FakeResponse:
        status_code = 200

        def json(self):
            return _gemini_response("  padded text  \n")

    monkeypatch.setattr(hp_llm.requests, "post", lambda url, params=None, json=None, timeout=None: FakeResponse())
    assert hp_llm.generate("x") == "padded text"


def test_generate_returns_none_without_api_key(monkeypatch):
    monkeypatch.setattr(hp_llm, "GEMINI_API_KEY", None)

    def should_not_be_called(*a, **k):
        raise AssertionError("requests.post called with no API key configured")

    monkeypatch.setattr(hp_llm.requests, "post", should_not_be_called)
    assert hp_llm.generate("x") is None


def test_generate_returns_none_on_non_200(monkeypatch):
    class FakeResponse:
        status_code = 500

        def json(self):
            return {}

    monkeypatch.setattr(hp_llm.requests, "post", lambda url, params=None, json=None, timeout=None: FakeResponse())
    assert hp_llm.generate("x") is None


def test_generate_returns_none_on_empty_candidates(monkeypatch):
    class FakeResponse:
        status_code = 200

        def json(self):
            return {"candidates": []}

    monkeypatch.setattr(hp_llm.requests, "post", lambda url, params=None, json=None, timeout=None: FakeResponse())
    assert hp_llm.generate("x") is None


def test_generate_returns_none_on_missing_parts(monkeypatch):
    class FakeResponse:
        status_code = 200

        def json(self):
            return {"candidates": [{"content": {"parts": []}}]}

    monkeypatch.setattr(hp_llm.requests, "post", lambda url, params=None, json=None, timeout=None: FakeResponse())
    assert hp_llm.generate("x") is None


def test_generate_returns_none_on_network_exception(monkeypatch):
    def fake_post(url, params=None, json=None, timeout=None):
        raise ConnectionError("network down")

    monkeypatch.setattr(hp_llm.requests, "post", fake_post)
    assert hp_llm.generate("x") is None


# --- 429 rate-limit retry, added 2026-09-01: found live that the free
# tier caps at 20 requests/minute, and a batch of qualifying leads (2
# Gemini calls each) blows through that fast — every momentary
# rate-limit hit looked identical to a real content-gen failure and
# discarded a genuinely qualifying lead. ---

def test_generate_retries_on_429_and_succeeds(monkeypatch):
    sleeps = []
    monkeypatch.setattr(hp_llm.time, "sleep", lambda s: sleeps.append(s))

    calls = {"count": 0}

    class RateLimited:
        status_code = 429

        def json(self):
            return {}

    class Success:
        status_code = 200

        def json(self):
            return _gemini_response("recovered")

    def fake_post(url, params=None, json=None, timeout=None):
        calls["count"] += 1
        return RateLimited() if calls["count"] == 1 else Success()

    monkeypatch.setattr(hp_llm.requests, "post", fake_post)
    result = hp_llm.generate("x")
    assert result == "recovered"
    assert calls["count"] == 2
    assert len(sleeps) == 1  # backed off exactly once before the retry that succeeded


def test_generate_returns_none_after_exhausting_429_retries(monkeypatch):
    monkeypatch.setattr(hp_llm.time, "sleep", lambda s: None)

    calls = {"count": 0}

    class RateLimited:
        status_code = 429

        def json(self):
            return {}

    def fake_post(url, params=None, json=None, timeout=None):
        calls["count"] += 1
        return RateLimited()

    monkeypatch.setattr(hp_llm.requests, "post", fake_post)
    result = hp_llm.generate("x")
    assert result is None
    assert calls["count"] == hp_llm.MAX_RATE_LIMIT_RETRIES + 1  # the original try + all retries


def test_generate_429_backoff_increases_each_retry(monkeypatch):
    sleeps = []
    monkeypatch.setattr(hp_llm.time, "sleep", lambda s: sleeps.append(s))

    class RateLimited:
        status_code = 429

        def json(self):
            return {}

    monkeypatch.setattr(hp_llm.requests, "post", lambda url, params=None, json=None, timeout=None: RateLimited())
    hp_llm.generate("x")
    assert sleeps == sorted(sleeps)  # non-decreasing backoff
    assert len(sleeps) == hp_llm.MAX_RATE_LIMIT_RETRIES


def test_generate_non_429_failure_does_not_retry(monkeypatch):
    """A real failure (not a rate limit) must fail immediately — retrying
    a genuine 500 or malformed response wouldn't help and would just
    slow every other caller down for no benefit."""
    monkeypatch.setattr(hp_llm.time, "sleep", lambda s: (_ for _ in ()).throw(AssertionError("should not sleep/retry on non-429")))

    calls = {"count": 0}

    class ServerError:
        status_code = 500

        def json(self):
            return {}

    def fake_post(url, params=None, json=None, timeout=None):
        calls["count"] += 1
        return ServerError()

    monkeypatch.setattr(hp_llm.requests, "post", fake_post)
    assert hp_llm.generate("x") is None
    assert calls["count"] == 1


def test_generate_passes_api_key_as_param(monkeypatch):
    captured = {}

    class FakeResponse:
        status_code = 200

        def json(self):
            return _gemini_response("ok")

    def fake_post(url, params=None, json=None, timeout=None):
        captured["params"] = params
        captured["json"] = json
        return FakeResponse()

    monkeypatch.setattr(hp_llm.requests, "post", fake_post)
    hp_llm.generate("my prompt")
    assert captured["params"] == {"key": "fake-test-key"}
    assert captured["json"] == {"contents": [{"parts": [{"text": "my prompt"}]}]}
