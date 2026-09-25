#!/usr/bin/env python3
"""
Cloudflare Pages client — SiteScout (Sprint 17, Track B)

Thin wrapper around the real Cloudflare API calls needed to stand up one
Pages project per ghost site and point a example.com subdomain at it.
Every call here was verified by hand against the real account (project
hp-ghost-site-test) before being written into this module — two things
in particular are NOT what the API reference docs alone tell you:

1. Direct-upload asset hashing is NOT sha256(raw bytes). It's
   blake3(base64(content) + extension).hexdigest()[:32] — confirmed by a
   working end-to-end deploy; the sha256 version silently "succeeds"
   (the deployment API accepts any hash string with no validation) but
   serves a 500 with an empty body forever, because the asset was never
   actually stored under the key the edge looks up at request time.

2. Attaching a custom domain to a Pages project does NOT auto-create the
   DNS record, even though the zone is on Cloudflare and owned by the
   same account/token. The domain sits at status "pending" with
   verification_data.error_message == "CNAME record not set" until a
   CNAME pointing at "{project_name}.pages.dev" is created by hand in
   the zone — create_dns_cname() below does that.

Deploy is a 4-call sequence (get_upload_token, upload_assets,
upsert_hashes, create_deployment) — this is what Wrangler does
internally; there is no single-call "just send me the files" endpoint
despite what the top-level deployments POST reference implies.
"""

import base64
import json
import os

import requests
from blake3 import blake3

import hp_env

API_BASE = "https://api.cloudflare.com/client/v4"

CLOUDFLARE_API_TOKEN = os.environ.get("CLOUDFLARE_API_TOKEN")
CLOUDFLARE_ACCOUNT_ID = os.environ.get("CLOUDFLARE_ACCOUNT_ID")
CLOUDFLARE_ZONE_ID = os.environ.get("CLOUDFLARE_ZONE_ID")

_CONTENT_TYPES = {
    ".html": "text/html",
    ".css": "text/css",
    ".js": "application/javascript",
    ".json": "application/json",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}


def _auth_headers(token=None):
    return {"Authorization": f"Bearer {token or CLOUDFLARE_API_TOKEN}"}


def content_type_for(filename):
    _, ext = os.path.splitext(filename)
    return _CONTENT_TYPES.get(ext.lower(), "application/octet-stream")


def asset_hash(content_bytes, filename):
    _, ext = os.path.splitext(filename)
    ext = ext.lstrip(".")
    b64 = base64.b64encode(content_bytes).decode("ascii")
    return blake3((b64 + ext).encode()).hexdigest()[:32]


def project_exists(project_name):
    resp = requests.get(
        f"{API_BASE}/accounts/{CLOUDFLARE_ACCOUNT_ID}/pages/projects/{project_name}",
        headers=_auth_headers(),
    )
    return resp.status_code == 200


def create_project(project_name):
    resp = requests.post(
        f"{API_BASE}/accounts/{CLOUDFLARE_ACCOUNT_ID}/pages/projects",
        headers=_auth_headers(),
        json={"name": project_name, "production_branch": "main"},
    )
    return resp.status_code == 200, resp.text


def get_upload_token(project_name):
    resp = requests.get(
        f"{API_BASE}/accounts/{CLOUDFLARE_ACCOUNT_ID}/pages/projects/{project_name}/upload-token",
        headers=_auth_headers(),
    )
    if resp.status_code != 200:
        return None
    return resp.json().get("result", {}).get("jwt")


def upload_assets(jwt, files):
    """files: list of (path_without_leading_slash, content_bytes). Returns
    (success, {path: hash})."""
    payload = []
    hashes = {}
    for path, content_bytes in files:
        h = asset_hash(content_bytes, path)
        hashes[path] = h
        payload.append({
            "key": h,
            "value": base64.b64encode(content_bytes).decode("ascii"),
            "metadata": {"contentType": content_type_for(path)},
            "base64": True,
        })
    resp = requests.post(
        f"{API_BASE}/pages/assets/upload",
        headers=_auth_headers(jwt),
        json=payload,
    )
    return resp.status_code == 200, hashes


def upsert_hashes(jwt, hashes):
    resp = requests.post(
        f"{API_BASE}/pages/assets/upsert-hashes",
        headers=_auth_headers(jwt),
        json={"hashes": list(hashes)},
    )
    return resp.status_code == 200


def create_deployment(project_name, manifest):
    """manifest: {"/index.html": hash, ...} (leading slash required)."""
    resp = requests.post(
        f"{API_BASE}/accounts/{CLOUDFLARE_ACCOUNT_ID}/pages/projects/{project_name}/deployments",
        headers=_auth_headers(),
        files={"manifest": (None, json.dumps(manifest))},
    )
    if resp.status_code != 200:
        return False, None, resp.text
    result = resp.json()["result"]
    return True, result["url"], None


def deploy_site(project_name, files):
    """Full deploy: create project if needed, upload all files, deploy.
    files: list of (path_without_leading_slash, content_bytes).
    Returns (success, pages_dev_url_or_None, error_or_None)."""
    if not project_exists(project_name):
        ok, err = create_project(project_name)
        if not ok:
            return False, None, f"create_project failed: {err[:300]}"

    jwt = get_upload_token(project_name)
    if not jwt:
        return False, None, "could not get upload token"

    ok, hashes = upload_assets(jwt, files)
    if not ok:
        return False, None, "asset upload failed"

    if not upsert_hashes(jwt, hashes.values()):
        return False, None, "upsert-hashes failed"

    manifest = {f"/{path}": h for path, h in hashes.items()}
    ok, url, err = create_deployment(project_name, manifest)
    if not ok:
        return False, None, f"create_deployment failed: {err[:300]}"
    return True, url, None


def attach_custom_domain(project_name, domain):
    resp = requests.post(
        f"{API_BASE}/accounts/{CLOUDFLARE_ACCOUNT_ID}/pages/projects/{project_name}/domains",
        headers=_auth_headers(),
        json={"name": domain},
    )
    return resp.status_code == 200, resp.text


def create_dns_cname(subdomain, target):
    """subdomain: full hostname (e.g. "westgate-barber-a1b2c3.example.com").
    target: "{project_name}.pages.dev"."""
    resp = requests.post(
        f"{API_BASE}/zones/{CLOUDFLARE_ZONE_ID}/dns_records",
        headers=_auth_headers(),
        json={"type": "CNAME", "name": subdomain, "content": target, "proxied": True},
    )
    return resp.status_code == 200, resp.text


def detach_custom_domain(project_name, domain):
    resp = requests.delete(
        f"{API_BASE}/accounts/{CLOUDFLARE_ACCOUNT_ID}/pages/projects/{project_name}/domains/{domain}",
        headers=_auth_headers(),
    )
    return resp.status_code == 200, resp.text


def delete_dns_record_for(subdomain):
    """Deletes the CNAME record create_dns_cname() made for this subdomain,
    if any. Not fatal if none is found — a project's domain can be detached
    and the project deleted even with a stray DNS record left behind, but
    Sprint 19 (expiry) wants to clean this up rather than leave one CNAME
    per expired lead accumulating in the zone forever."""
    resp = requests.get(
        f"{API_BASE}/zones/{CLOUDFLARE_ZONE_ID}/dns_records",
        headers=_auth_headers(),
        params={"name": subdomain},
    )
    if resp.status_code != 200:
        return False, resp.text
    records = resp.json().get("result", [])
    ok = True
    for record in records:
        d = requests.delete(
            f"{API_BASE}/zones/{CLOUDFLARE_ZONE_ID}/dns_records/{record['id']}",
            headers=_auth_headers(),
        )
        ok = ok and d.status_code == 200
    return ok, ""


def delete_project(project_name):
    """Deletes a Pages project outright — NOT the same as emptying it.
    Requires the project's custom domain to already be detached
    (detach_custom_domain() first) or Cloudflare returns a 400 (found
    live while cleaning up Sprint 17's test project)."""
    resp = requests.delete(
        f"{API_BASE}/accounts/{CLOUDFLARE_ACCOUNT_ID}/pages/projects/{project_name}",
        headers=_auth_headers(),
    )
    return resp.status_code == 200, resp.text
