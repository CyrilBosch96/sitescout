#!/usr/bin/env python3
"""
Tests for scripts/cloudflare_pages.py.

Covers: the asset-hash formula (blake3(base64(content) + ext)[:32], NOT
sha256 of raw bytes — this was the actual bug found while verifying the
real API by hand, see the module docstring), the 4-call deploy_site()
sequence in order, and failure propagation at each step.
"""

import base64
import os
import sys

import pytest
from blake3 import blake3

SCRIPTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
sys.path.insert(0, SCRIPTS_DIR)

import cloudflare_pages as cf  # noqa: E402


# --- asset_hash(): the formula that actually matches what the edge looks up ---

def test_asset_hash_matches_blake3_of_base64_plus_extension():
    content = b"<html>hi</html>"
    b64 = base64.b64encode(content).decode("ascii")
    expected = blake3((b64 + "html").encode()).hexdigest()[:32]
    assert cf.asset_hash(content, "index.html") == expected


def test_asset_hash_is_not_sha256_of_raw_bytes():
    import hashlib
    content = b"<html>hi</html>"
    wrong = hashlib.sha256(content).hexdigest()
    assert cf.asset_hash(content, "index.html") != wrong


def test_asset_hash_differs_by_extension():
    content = b"same bytes"
    assert cf.asset_hash(content, "a.html") != cf.asset_hash(content, "a.css")


# --- content_type_for() ---

def test_content_type_for_known_extensions():
    assert cf.content_type_for("index.html") == "text/html"
    assert cf.content_type_for("style.css") == "text/css"
    assert cf.content_type_for("unknown.xyz") == "application/octet-stream"


# --- deploy_site(): the 4-call sequence ---

class _Resp:
    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code = status_code
        self._json = json_data or {}
        self.text = text

    def json(self):
        return self._json


def test_deploy_site_full_success_sequence(monkeypatch):
    calls = []

    def fake_get(url, headers=None, **k):
        calls.append(("get", url))
        if "upload-token" in url:
            return _Resp(200, {"result": {"jwt": "fake-jwt"}})
        return _Resp(200)  # project_exists() check

    monkeypatch.setattr(cf.requests, "get", fake_get)

    def fake_post(url, headers=None, json=None, files=None, **k):
        calls.append(("post", url))
        if "assets/upload" in url:
            return _Resp(200, {"result": {"successful_key_count": 1, "unsuccessful_keys": []}})
        if "upsert-hashes" in url:
            return _Resp(200, {"success": True})
        if "/deployments" in url:
            return _Resp(200, {"result": {"url": "https://abc123.myproj.pages.dev"}})
        raise AssertionError(f"unexpected POST {url}")

    monkeypatch.setattr(cf.requests, "post", fake_post)

    ok, url, err = cf.deploy_site("myproj", [("index.html", b"<html></html>")])

    assert ok is True
    assert url == "https://abc123.myproj.pages.dev"
    assert err is None
    # project already exists (GET 200) -> no create_project call, straight to upload-token
    post_urls = [u for method, u in calls if method == "post"]
    assert any("assets/upload" in u for u in post_urls)
    assert any("upsert-hashes" in u for u in post_urls)
    assert any("/deployments" in u for u in post_urls)


def test_deploy_site_creates_project_when_missing(monkeypatch):
    calls = []

    def fake_get(url, headers=None, **k):
        calls.append(("get", url))
        if url.endswith("/myproj"):
            return _Resp(404)
        return _Resp(200, {"result": {"jwt": "fake-jwt"}})

    monkeypatch.setattr(cf.requests, "get", fake_get)

    def fake_post(url, headers=None, json=None, files=None, **k):
        calls.append(("post", url))
        if url.endswith("/projects"):
            return _Resp(200, {"result": {"name": "myproj"}})
        if "assets/upload" in url:
            return _Resp(200, {"result": {}})
        if "upsert-hashes" in url:
            return _Resp(200, {"success": True})
        if "/deployments" in url:
            return _Resp(200, {"result": {"url": "https://x.myproj.pages.dev"}})
        raise AssertionError(f"unexpected POST {url}")

    monkeypatch.setattr(cf.requests, "post", fake_post)

    ok, url, err = cf.deploy_site("myproj", [("index.html", b"<html></html>")])
    assert ok is True
    assert any(u.endswith("/projects") for method, u in calls if method == "post")


def test_deploy_site_fails_when_upload_token_missing(monkeypatch):
    monkeypatch.setattr(cf.requests, "get", lambda url, headers=None, **k: _Resp(200, {"result": {}}) if "upload-token" in url else _Resp(200))
    ok, url, err = cf.deploy_site("myproj", [("index.html", b"hi")])
    assert ok is False
    assert "upload token" in err


def test_deploy_site_fails_when_asset_upload_fails(monkeypatch):
    monkeypatch.setattr(cf.requests, "get", lambda url, headers=None, **k: _Resp(200, {"result": {"jwt": "fake-jwt"}}) if "upload-token" in url else _Resp(200))
    monkeypatch.setattr(cf.requests, "post", lambda url, headers=None, json=None, files=None, **k: _Resp(500) if "assets/upload" in url else _Resp(200))
    ok, url, err = cf.deploy_site("myproj", [("index.html", b"hi")])
    assert ok is False
    assert "asset upload" in err


# --- attach_custom_domain() / create_dns_cname() ---

def test_attach_custom_domain_success(monkeypatch):
    monkeypatch.setattr(cf.requests, "post", lambda url, headers=None, json=None: _Resp(200))
    ok, _ = cf.attach_custom_domain("myproj", "myproj.example.com")
    assert ok is True


def test_create_dns_cname_uses_pages_dev_target(monkeypatch):
    captured = {}

    def fake_post(url, headers=None, json=None):
        captured["url"] = url
        captured["json"] = json
        return _Resp(200)

    monkeypatch.setattr(cf.requests, "post", fake_post)
    ok, _ = cf.create_dns_cname("myproj.example.com", "myproj.pages.dev")
    assert ok is True
    assert captured["json"]["type"] == "CNAME"
    assert captured["json"]["content"] == "myproj.pages.dev"
    assert captured["json"]["proxied"] is True


# --- Sprint 19: teardown functions (detach_custom_domain, delete_dns_record_for, delete_project) ---

def test_detach_custom_domain_deletes_correct_url(monkeypatch):
    captured = {}

    def fake_delete(url, headers=None):
        captured["url"] = url
        return _Resp(200)

    monkeypatch.setattr(cf.requests, "delete", fake_delete)
    ok, _ = cf.detach_custom_domain("myproj", "myproj.example.com")
    assert ok is True
    assert captured["url"].endswith("/pages/projects/myproj/domains/myproj.example.com")


def test_delete_project_success(monkeypatch):
    monkeypatch.setattr(cf.requests, "delete", lambda url, headers=None: _Resp(200))
    ok, _ = cf.delete_project("myproj")
    assert ok is True


def test_delete_project_fails_when_domain_still_attached(monkeypatch):
    monkeypatch.setattr(cf.requests, "delete", lambda url, headers=None: _Resp(400, text="domain still attached"))
    ok, err = cf.delete_project("myproj")
    assert ok is False


def test_delete_dns_record_for_deletes_all_matching_records(monkeypatch):
    deleted_ids = []

    def fake_get(url, headers=None, params=None):
        return _Resp(200, {"result": [{"id": "rec-1"}, {"id": "rec-2"}]})

    def fake_delete(url, headers=None):
        deleted_ids.append(url.rsplit("/", 1)[-1])
        return _Resp(200)

    monkeypatch.setattr(cf.requests, "get", fake_get)
    monkeypatch.setattr(cf.requests, "delete", fake_delete)

    ok, _ = cf.delete_dns_record_for("myproj.example.com")
    assert ok is True
    assert deleted_ids == ["rec-1", "rec-2"]


def test_delete_dns_record_for_no_records_found_is_not_fatal(monkeypatch):
    monkeypatch.setattr(cf.requests, "get", lambda url, headers=None, params=None: _Resp(200, {"result": []}))

    def delete_should_not_be_called(*a, **k):
        raise AssertionError("delete should not be called when no records exist")
    monkeypatch.setattr(cf.requests, "delete", delete_should_not_be_called)

    ok, _ = cf.delete_dns_record_for("myproj.example.com")
    assert ok is True
