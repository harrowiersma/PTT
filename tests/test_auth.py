"""Tests for authentication endpoints."""

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_login_valid(client: AsyncClient):
    resp = await client.post("/api/auth/login", json={
        "username": "testadmin",
        "password": "testpass123",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert "access_token" in data
    assert data["token_type"] == "bearer"


@pytest.mark.asyncio
async def test_login_invalid_password(client: AsyncClient):
    resp = await client.post("/api/auth/login", json={
        "username": "testadmin",
        "password": "wrongpassword",
    })
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_login_invalid_username(client: AsyncClient):
    resp = await client.post("/api/auth/login", json={
        "username": "nonexistent",
        "password": "testpass123",
    })
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_protected_endpoint_no_token(client: AsyncClient):
    resp = await client.get("/api/users")
    assert resp.status_code in (401, 403)


@pytest.mark.asyncio
async def test_protected_endpoint_invalid_token(client: AsyncClient):
    resp = await client.get("/api/users", headers={
        "Authorization": "Bearer invalid-token"
    })
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_protected_endpoint_valid_token(client: AsyncClient, auth_headers: dict):
    resp = await client.get("/api/users", headers=auth_headers)
    assert resp.status_code == 200
