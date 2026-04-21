import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_list_empty_initially(admin_client: AsyncClient):
    r = await admin_client.get("/api/dispatch/messages")
    assert r.status_code == 200
    assert r.json() == []


@pytest.mark.asyncio
async def test_create_then_list(admin_client: AsyncClient):
    r = await admin_client.post("/api/dispatch/messages", json={
        "label": "Pickup ready",
        "message": "Pickup ready at the gate",
        "sort_order": 1,
    })
    assert r.status_code == 201
    created = r.json()
    assert created["label"] == "Pickup ready"

    r = await admin_client.get("/api/dispatch/messages")
    assert len(r.json()) == 1


@pytest.mark.asyncio
async def test_update_message(admin_client: AsyncClient):
    r = await admin_client.post("/api/dispatch/messages", json={
        "label": "x", "message": "y",
    })
    mid = r.json()["id"]
    r = await admin_client.patch(f"/api/dispatch/messages/{mid}", json={"label": "renamed"})
    assert r.status_code == 200
    assert r.json()["label"] == "renamed"


@pytest.mark.asyncio
async def test_delete_message(admin_client: AsyncClient):
    r = await admin_client.post("/api/dispatch/messages", json={
        "label": "x", "message": "y",
    })
    mid = r.json()["id"]
    r = await admin_client.delete(f"/api/dispatch/messages/{mid}")
    assert r.status_code == 204
    r = await admin_client.get("/api/dispatch/messages")
    assert r.json() == []


@pytest.mark.asyncio
async def test_list_ordered_by_sort_order(admin_client: AsyncClient):
    await admin_client.post("/api/dispatch/messages", json={"label": "B", "message": "b", "sort_order": 2})
    await admin_client.post("/api/dispatch/messages", json={"label": "A", "message": "a", "sort_order": 1})
    r = await admin_client.get("/api/dispatch/messages")
    labels = [m["label"] for m in r.json()]
    assert labels == ["A", "B"]
