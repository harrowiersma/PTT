"""Tests for user management endpoints."""

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_create_user(client: AsyncClient, auth_headers: dict):
    resp = await client.post("/api/users", json={
        "username": "testuser1",
        "display_name": "Test User",
        "password": "password123",
    }, headers=auth_headers)
    assert resp.status_code == 201
    data = resp.json()
    assert data["username"] == "testuser1"
    assert data["display_name"] == "Test User"


@pytest.mark.asyncio
async def test_create_duplicate_user(client: AsyncClient, auth_headers: dict):
    await client.post("/api/users", json={
        "username": "dupuser",
        "password": "password123",
    }, headers=auth_headers)

    resp = await client.post("/api/users", json={
        "username": "dupuser",
        "password": "password456",
    }, headers=auth_headers)
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_list_users(client: AsyncClient, auth_headers: dict):
    await client.post("/api/users", json={
        "username": "listuser1",
        "password": "pass1234",
    }, headers=auth_headers)

    resp = await client.get("/api/users", headers=auth_headers)
    assert resp.status_code == 200
    users = resp.json()
    assert len(users) >= 1
    assert any(u["username"] == "listuser1" for u in users)


@pytest.mark.asyncio
async def test_get_user(client: AsyncClient, auth_headers: dict):
    create_resp = await client.post("/api/users", json={
        "username": "getuser1",
        "password": "pass1234",
    }, headers=auth_headers)
    user_id = create_resp.json()["id"]

    resp = await client.get(f"/api/users/{user_id}", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["username"] == "getuser1"


@pytest.mark.asyncio
async def test_get_nonexistent_user(client: AsyncClient, auth_headers: dict):
    resp = await client.get("/api/users/99999", headers=auth_headers)
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_user(client: AsyncClient, auth_headers: dict):
    create_resp = await client.post("/api/users", json={
        "username": "deleteuser",
        "password": "pass1234",
    }, headers=auth_headers)
    user_id = create_resp.json()["id"]

    resp = await client.delete(f"/api/users/{user_id}", headers=auth_headers)
    assert resp.status_code == 204


@pytest.mark.asyncio
async def test_get_user_qr(client: AsyncClient, auth_headers: dict):
    create_resp = await client.post("/api/users", json={
        "username": "qruser",
        "password": "pass1234",
    }, headers=auth_headers)
    user_id = create_resp.json()["id"]

    resp = await client.get(f"/api/users/{user_id}/qr", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert "mumble_url" in data
    assert "qruser" in data["mumble_url"]


@pytest.mark.asyncio
async def test_username_validation(client: AsyncClient, auth_headers: dict):
    # Username with spaces should fail
    resp = await client.post("/api/users", json={
        "username": "bad user name",
        "password": "pass1234",
    }, headers=auth_headers)
    assert resp.status_code == 422

    # Username too long should fail
    resp = await client.post("/api/users", json={
        "username": "a" * 100,
        "password": "pass1234",
    }, headers=auth_headers)
    assert resp.status_code == 422
