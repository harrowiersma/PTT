"""Tests for channel management endpoints."""

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_create_channel(client: AsyncClient, auth_headers: dict):
    resp = await client.post("/api/channels", json={
        "name": "TestChannel",
        "description": "A test channel",
    }, headers=auth_headers)
    assert resp.status_code == 201
    assert resp.json()["name"] == "TestChannel"


@pytest.mark.asyncio
async def test_create_duplicate_channel(client: AsyncClient, auth_headers: dict):
    await client.post("/api/channels", json={"name": "DupChan"}, headers=auth_headers)
    resp = await client.post("/api/channels", json={"name": "DupChan"}, headers=auth_headers)
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_list_channels(client: AsyncClient, auth_headers: dict):
    await client.post("/api/channels", json={"name": "ListChan"}, headers=auth_headers)
    resp = await client.get("/api/channels", headers=auth_headers)
    assert resp.status_code == 200
    assert len(resp.json()) >= 1


@pytest.mark.asyncio
async def test_delete_channel(client: AsyncClient, auth_headers: dict):
    create_resp = await client.post("/api/channels", json={"name": "DelChan"}, headers=auth_headers)
    chan_id = create_resp.json()["id"]
    resp = await client.delete(f"/api/channels/{chan_id}", headers=auth_headers)
    assert resp.status_code == 204


@pytest.mark.asyncio
async def test_channel_name_validation(client: AsyncClient, auth_headers: dict):
    resp = await client.post("/api/channels", json={
        "name": "<script>alert(1)</script>",
    }, headers=auth_headers)
    assert resp.status_code == 422
