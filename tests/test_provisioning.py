"""Tests for the device-provisioning token surface.

Covers the admin CRUD (create/list/delete), the public /script/{slug}
renderer (OS detection + expiry gate + template substitution), and the
/completed mark-used path. The APK endpoint's happy path is exercised
via a temp file; the 404 fallback confirms the MVP placeholder story.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from server.database import async_session
from server.models import DeviceProvisioningToken


async def _create_user(client: AsyncClient, auth_headers: dict, username: str = "provuser") -> int:
    resp = await client.post(
        "/api/users",
        json={"username": username, "password": "mumble-secret-123"},
        headers=auth_headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


@pytest.mark.asyncio
async def test_create_token_happy(client: AsyncClient, auth_headers: dict):
    uid = await _create_user(client, auth_headers, "provuser1")
    resp = await client.post(
        "/api/provisioning/tokens",
        json={"user_id": uid, "password": "mumble-secret-123"},
        headers=auth_headers,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert len(body["slug"]) == 8
    assert body["slug"].isalnum()
    assert body["url"].endswith("/p/" + body["slug"])
    assert body["username"] == "provuser1"
    assert body["used_at"] is None


@pytest.mark.asyncio
async def test_create_token_unknown_user(client: AsyncClient, auth_headers: dict):
    resp = await client.post(
        "/api/provisioning/tokens",
        json={"user_id": 99999, "password": "x" * 8},
        headers=auth_headers,
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_create_token_requires_auth(client: AsyncClient):
    resp = await client.post(
        "/api/provisioning/tokens",
        json={"user_id": 1, "password": "xxxx"},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_list_tokens(client: AsyncClient, auth_headers: dict):
    uid = await _create_user(client, auth_headers, "provuser2")
    await client.post(
        "/api/provisioning/tokens",
        json={"user_id": uid, "password": "mumble-secret-123"},
        headers=auth_headers,
    )
    resp = await client.get("/api/provisioning/tokens", headers=auth_headers)
    assert resp.status_code == 200
    rows = resp.json()
    assert any(r["username"] == "provuser2" for r in rows)


@pytest.mark.asyncio
async def test_revoke_token(client: AsyncClient, auth_headers: dict):
    uid = await _create_user(client, auth_headers, "provuser3")
    created = await client.post(
        "/api/provisioning/tokens",
        json={"user_id": uid, "password": "mumble-secret-123"},
        headers=auth_headers,
    )
    slug = created.json()["slug"]
    resp = await client.delete(
        f"/api/provisioning/tokens/{slug}",
        headers=auth_headers,
    )
    assert resp.status_code == 204
    # Second delete -> 404
    again = await client.delete(
        f"/api/provisioning/tokens/{slug}",
        headers=auth_headers,
    )
    assert again.status_code == 404


@pytest.mark.asyncio
async def test_script_macos(client: AsyncClient, auth_headers: dict):
    uid = await _create_user(client, auth_headers, "mac_user")
    created = await client.post(
        "/api/provisioning/tokens",
        json={"user_id": uid, "password": "macpass-123"},
        headers=auth_headers,
    )
    slug = created.json()["slug"]
    resp = await client.get(
        f"/api/provisioning/script/{slug}",
        headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4)"},
    )
    assert resp.status_code == 200
    assert "text/x-shellscript" in resp.headers["content-type"]
    assert "openptt-provision.sh" in resp.headers["content-disposition"]
    body = resp.text
    assert "USERNAME='mac_user'" in body
    assert "macpass-123" in body
    # os_fetched gets stamped
    async with async_session() as db:
        row = (await db.execute(
            select(DeviceProvisioningToken).where(DeviceProvisioningToken.slug == slug)
        )).scalar_one()
        assert row.os_fetched == "macos"


@pytest.mark.asyncio
async def test_script_windows(client: AsyncClient, auth_headers: dict):
    uid = await _create_user(client, auth_headers, "winuser")
    created = await client.post(
        "/api/provisioning/tokens",
        json={"user_id": uid, "password": "winpass-123"},
        headers=auth_headers,
    )
    slug = created.json()["slug"]
    resp = await client.get(
        f"/api/provisioning/script/{slug}",
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
    )
    assert resp.status_code == 200
    assert "text/x-powershell" in resp.headers["content-type"]
    assert "openptt-provision.ps1" in resp.headers["content-disposition"]
    body = resp.text
    assert "$Username       = 'winuser'" in body
    assert "winpass-123" in body


@pytest.mark.asyncio
async def test_script_linux_fallback(client: AsyncClient, auth_headers: dict):
    uid = await _create_user(client, auth_headers, "linuxuser")
    created = await client.post(
        "/api/provisioning/tokens",
        json={"user_id": uid, "password": "tuxpass-123"},
        headers=auth_headers,
    )
    slug = created.json()["slug"]
    resp = await client.get(
        f"/api/provisioning/script/{slug}",
        headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64)"},
    )
    assert resp.status_code == 200
    assert "text/x-shellscript" in resp.headers["content-type"]


@pytest.mark.asyncio
async def test_script_expired(client: AsyncClient, auth_headers: dict):
    uid = await _create_user(client, auth_headers, "expired_user")
    created = await client.post(
        "/api/provisioning/tokens",
        json={"user_id": uid, "password": "xxxx-1234"},
        headers=auth_headers,
    )
    slug = created.json()["slug"]
    # Force-expire by rewinding expires_at directly.
    async with async_session() as db:
        row = (await db.execute(
            select(DeviceProvisioningToken).where(DeviceProvisioningToken.slug == slug)
        )).scalar_one()
        row.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        await db.commit()
    resp = await client.get(f"/api/provisioning/script/{slug}")
    assert resp.status_code == 410


@pytest.mark.asyncio
async def test_script_unknown_slug(client: AsyncClient):
    resp = await client.get("/api/provisioning/script/not-a-real-slug")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_completed_marks_used(client: AsyncClient, auth_headers: dict):
    uid = await _create_user(client, auth_headers, "done_user")
    created = await client.post(
        "/api/provisioning/tokens",
        json={"user_id": uid, "password": "donepass-123"},
        headers=auth_headers,
    )
    slug = created.json()["slug"]
    # No auth required here.
    resp = await client.post(f"/api/provisioning/tokens/{slug}/completed")
    assert resp.status_code == 200
    assert resp.json()["used_at"] is not None
    # Idempotent second call keeps the original timestamp
    first_ts = resp.json()["used_at"]
    second = await client.post(f"/api/provisioning/tokens/{slug}/completed")
    assert second.status_code == 200
    assert second.json()["used_at"] == first_ts


@pytest.mark.asyncio
async def test_apk_missing_returns_404(client: AsyncClient, monkeypatch):
    """When no APK is published (MVP default), the endpoint 404s with a
    helpful message instead of serving garbage."""
    from server.config import settings
    monkeypatch.setattr(settings, "provisioning_apk_path", "/tmp/nonexistent-openptt.apk")
    resp = await client.get("/api/provisioning/apk/openptt-foss-debug.apk")
    assert resp.status_code == 404
    assert "not yet published" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_apk_served(client: AsyncClient, tmp_path, monkeypatch):
    """Happy path: a published APK file is streamed back as an
    application/vnd.android.package-archive."""
    fake_apk = tmp_path / "openptt-foss-debug.apk"
    fake_apk.write_bytes(b"PK\x03\x04FAKE-APK-BYTES")
    from server.config import settings
    monkeypatch.setattr(settings, "provisioning_apk_path", str(fake_apk))
    resp = await client.get("/api/provisioning/apk/openptt-foss-debug.apk")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/vnd.android.package-archive")
    assert resp.content == b"PK\x03\x04FAKE-APK-BYTES"
