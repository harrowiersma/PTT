# Dispatch / Map Merge Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Merge the dashboard's `Live → Map` and `Live → Dispatch` sub-tabs into a single operational page, and split the admin-y configuration onto a new `System → Dispatch Setup` tab with five cards (Map Home, Search Behaviour, Saved Locations, Canned Messages, plus the existing locations CRUD).

**Architecture:** Two new typed tables (`dispatch_settings` singleton, `dispatch_canned_messages`); modify the existing `/api/dispatch/nearest` to honour configurable max-results + radius; restructure the dashboard HTML/JS so the merged Dispatch page reads the map default from settings and the dispatch modal pulls canned messages from the new endpoint. Existing `dispatch_locations` table stays unchanged — only its UI moves.

**Tech Stack:** FastAPI + SQLAlchemy + Alembic (backend), vanilla HTML/CSS/JS + Leaflet (dashboard).

**Companion design doc:** `docs/plans/2026-04-21-dispatch-map-merge-design.md`

---

## Phases

1. **Backend: settings table, model, service** — single-row config with seed.
2. **Backend: settings API + cache refresh** — GET (public) / PUT (admin).
3. **Backend: nearest honours settings** — radius filter + max-workers limit.
4. **Backend: canned messages model + CRUD API.**
5. **Dashboard: tab restructure** — remove Map sub-tab, rename Dispatch, add System → Dispatch Setup.
6. **Dashboard: merged Dispatch page** — map + nearest list + recent dispatches, target pin, settings-driven map home.
7. **Dashboard: Setup page cards** — Map Home, Search Behaviour, Saved Locations (moved), Canned Messages.
8. **Dashboard: dispatch modal with canned-message dropdown.**
9. **Deploy + manual verification on `ptt.harro.ch`.**

Each task is independently committable. Hard-stop after Task 9 to confirm prod is healthy before considering Phase 6 complete.

---

## Task 1: `dispatch_settings` table + model + migration

**Files:**
- Modify: `server/models.py` (append `DispatchSettings` class)
- Create: `server/alembic/versions/e1c4f8a3b5d6_dispatch_settings_and_messages.py`
- Modify: `tests/conftest.py` (seed singleton row in `setup_db`)
- Test: `tests/test_dispatch_settings_model.py`

**Step 1: Write the failing test**

```python
# tests/test_dispatch_settings_model.py
import pytest
from sqlalchemy import select
from server.models import DispatchSettings


@pytest.mark.asyncio
async def test_singleton_seeded_with_lisbon_defaults(db_session):
    result = await db_session.execute(select(DispatchSettings))
    rows = result.scalars().all()
    assert len(rows) == 1
    s = rows[0]
    assert s.id == 1
    assert s.map_home_lat == pytest.approx(38.72, abs=0.01)
    assert s.map_home_lng == pytest.approx(-9.14, abs=0.01)
    assert s.map_home_zoom == 11
    assert s.max_workers == 10
    assert s.search_radius_m is None
```

**Step 2: Run test, expect fail**

```bash
cd /Users/harrowiersma/Documents/CLAUDE/PTT
python3 -m pytest tests/test_dispatch_settings_model.py -v
```
Expected: `AttributeError: module 'server.models' has no attribute 'DispatchSettings'`.

**Step 3: Add the model**

Append to `server/models.py`:

```python
class DispatchSettings(Base):
    """Singleton config row for the dispatch feature.

    Always exactly one row with id=1 (seeded by migration). Holds the map
    default, the per-request worker cap, and the optional radius filter
    applied by /api/dispatch/nearest.
    """
    __tablename__ = "dispatch_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    map_home_lat: Mapped[float] = mapped_column(Float, nullable=False)
    map_home_lng: Mapped[float] = mapped_column(Float, nullable=False)
    map_home_zoom: Mapped[int] = mapped_column(Integer, nullable=False, default=11)
    max_workers: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    search_radius_m: Mapped[int] = mapped_column(Integer, nullable=True)
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    updated_by: Mapped[str] = mapped_column(String(64), nullable=True)
```

**Step 4: Create the Alembic migration**

```bash
cd /Users/harrowiersma/Documents/CLAUDE/PTT/server
docker exec ptt-admin-1 alembic current  # confirm head is d7e9a1f2b0c3
```

Create `server/alembic/versions/e1c4f8a3b5d6_dispatch_settings_and_messages.py`:

```python
"""dispatch_settings + dispatch_canned_messages

Revision ID: e1c4f8a3b5d6
Revises: d7e9a1f2b0c3
Create Date: 2026-04-21
"""
from alembic import op
import sqlalchemy as sa

revision = "e1c4f8a3b5d6"
down_revision = "d7e9a1f2b0c3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "dispatch_settings",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=False),
        sa.Column("map_home_lat", sa.Float, nullable=False),
        sa.Column("map_home_lng", sa.Float, nullable=False),
        sa.Column("map_home_zoom", sa.Integer, nullable=False, server_default="11"),
        sa.Column("max_workers", sa.Integer, nullable=False, server_default="10"),
        sa.Column("search_radius_m", sa.Integer, nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
        sa.Column("updated_by", sa.String(64), nullable=True),
    )
    # Singleton seed: Lisbon centre, default search behaviour.
    op.execute(
        "INSERT INTO dispatch_settings (id, map_home_lat, map_home_lng, "
        "map_home_zoom, max_workers, search_radius_m) "
        "VALUES (1, 38.72, -9.14, 11, 10, NULL)"
    )

    op.create_table(
        "dispatch_canned_messages",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("label", sa.String(64), nullable=False),
        sa.Column("message", sa.String(500), nullable=False),
        sa.Column("sort_order", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("dispatch_canned_messages")
    op.drop_table("dispatch_settings")
```

**Step 5: Add seed to `tests/conftest.py`**

The test fixture uses `Base.metadata.create_all` (not Alembic), so the singleton seed must be replicated. After the existing `FeatureFlag` seed loop in `setup_db`, append:

```python
        # Match the e1c4f8a3b5d6 migration: seed the singleton dispatch_settings row.
        try:
            from server.models import DispatchSettings
            async with async_session() as _seed:
                _seed.add(DispatchSettings(
                    id=1, map_home_lat=38.72, map_home_lng=-9.14,
                    map_home_zoom=11, max_workers=10, search_radius_m=None,
                ))
                await _seed.commit()
        except ImportError:
            pass
```

**Step 6: Apply migration + run tests**

```bash
cd /Users/harrowiersma/Documents/CLAUDE/PTT
docker compose up -d --build admin
docker exec ptt-admin-1 alembic upgrade head
python3 -m pytest tests/test_dispatch_settings_model.py -v
```
Expected: PASS.

**Step 7: Commit**

```bash
git add server/models.py server/alembic/versions/e1c4f8a3b5d6_dispatch_settings_and_messages.py tests/conftest.py tests/test_dispatch_settings_model.py
git commit -m "dispatch: settings singleton + canned_messages tables (Phase 6)"
```

---

## Task 2: Settings service + GET/PUT API

**Files:**
- Create: `server/api/dispatch_settings.py`
- Modify: `server/main.py` (register router)
- Test: `tests/test_dispatch_settings_api.py`

**Step 1: Write the failing test**

```python
# tests/test_dispatch_settings_api.py
import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_get_settings_no_auth(client: AsyncClient):
    r = await client.get("/api/dispatch/settings")
    assert r.status_code == 200
    body = r.json()
    assert body["map_home_lat"] == pytest.approx(38.72, abs=0.01)
    assert body["max_workers"] == 10
    assert body["search_radius_m"] is None


@pytest.mark.asyncio
async def test_put_settings_requires_admin(client: AsyncClient):
    r = await client.put("/api/dispatch/settings", json={"max_workers": 5})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_put_settings_updates_fields(admin_client: AsyncClient):
    r = await admin_client.put("/api/dispatch/settings", json={
        "map_home_lat": 41.15, "map_home_lng": -8.61, "map_home_zoom": 12,
        "max_workers": 5, "search_radius_m": 3000,
    })
    assert r.status_code == 200
    body = r.json()
    assert body["map_home_lat"] == pytest.approx(41.15, abs=0.01)
    assert body["max_workers"] == 5
    assert body["search_radius_m"] == 3000


@pytest.mark.asyncio
async def test_put_settings_clamps_max_workers(admin_client: AsyncClient):
    r = await admin_client.put("/api/dispatch/settings", json={"max_workers": 999})
    assert r.status_code == 422  # pydantic validation


@pytest.mark.asyncio
async def test_put_settings_rejects_negative_radius(admin_client: AsyncClient):
    r = await admin_client.put("/api/dispatch/settings", json={"search_radius_m": -5})
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_put_settings_rejects_invalid_lat(admin_client: AsyncClient):
    r = await admin_client.put("/api/dispatch/settings", json={"map_home_lat": 999})
    assert r.status_code == 422
```

**Step 2: Run test, expect fail**

```bash
python3 -m pytest tests/test_dispatch_settings_api.py -v
```
Expected: 404 on all endpoints.

**Step 3: Implement the router**

Create `server/api/dispatch_settings.py`:

```python
"""Singleton config for the dispatch feature.

GET is intentionally unauthenticated — the dashboard reads it before login
to centre the map correctly. The only thing leaked is the operator's chosen
map default; same risk profile as /api/status/capabilities.

PUT is admin-only and refreshes the in-process cache so /api/dispatch/nearest
sees the new values without a service restart.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.auth import get_current_admin
from server.database import get_db
from server.features_gate import requires_feature
from server.models import DispatchSettings

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/dispatch/settings",
    tags=["dispatch"],
    dependencies=[requires_feature("dispatch")],
)


# In-process cache; refreshed on PUT and on first read.
_cache: dict | None = None


class SettingsResponse(BaseModel):
    map_home_lat: float
    map_home_lng: float
    map_home_zoom: int
    max_workers: int
    search_radius_m: int | None

    model_config = {"from_attributes": True}


class SettingsUpdate(BaseModel):
    map_home_lat: float | None = Field(default=None, ge=-90, le=90)
    map_home_lng: float | None = Field(default=None, ge=-180, le=180)
    map_home_zoom: int | None = Field(default=None, ge=1, le=19)
    max_workers: int | None = Field(default=None, ge=1, le=50)
    search_radius_m: int | None = Field(default=None, ge=0)


async def _load(db: AsyncSession) -> DispatchSettings:
    result = await db.execute(select(DispatchSettings).where(DispatchSettings.id == 1))
    row = result.scalar_one_or_none()
    if row is None:
        # Defensive: migration should have seeded this. Materialise defaults.
        row = DispatchSettings(
            id=1, map_home_lat=38.72, map_home_lng=-9.14,
            map_home_zoom=11, max_workers=10, search_radius_m=None,
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
    return row


async def get_cached(db: AsyncSession) -> dict:
    """Used by /api/dispatch/nearest to avoid a DB round-trip per request."""
    global _cache
    if _cache is None:
        row = await _load(db)
        _cache = {
            "map_home_lat": row.map_home_lat,
            "map_home_lng": row.map_home_lng,
            "map_home_zoom": row.map_home_zoom,
            "max_workers": row.max_workers,
            "search_radius_m": row.search_radius_m,
        }
    return _cache


def invalidate_cache() -> None:
    global _cache
    _cache = None


@router.get("", response_model=SettingsResponse)
async def get_settings(db: AsyncSession = Depends(get_db)):
    return await _load(db)


@router.put("", response_model=SettingsResponse)
async def update_settings(
    body: SettingsUpdate,
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(get_current_admin),
):
    row = await _load(db)
    if body.map_home_lat is not None:
        row.map_home_lat = body.map_home_lat
    if body.map_home_lng is not None:
        row.map_home_lng = body.map_home_lng
    if body.map_home_zoom is not None:
        row.map_home_zoom = body.map_home_zoom
    if body.max_workers is not None:
        row.max_workers = body.max_workers
    if "search_radius_m" in body.model_fields_set:
        # Allow explicit null to clear the radius filter.
        row.search_radius_m = body.search_radius_m
    row.updated_by = admin.get("username")
    await db.commit()
    await db.refresh(row)
    invalidate_cache()
    return row
```

**Note on the GET dependency:** `requires_feature("dispatch")` is at the router level. If `dispatch` is disabled, even GET 503s — which is fine: the dashboard won't try to read map home for a disabled feature.

**Step 4: Register the router**

In `server/main.py`, add to the imports near the other dispatch imports:

```python
from server.api.dispatch_settings import router as dispatch_settings_router
```

And register it (alongside `dispatch_router`):

```python
app.include_router(dispatch_settings_router)
```

**Step 5: Run tests**

```bash
docker compose up -d --build admin
python3 -m pytest tests/test_dispatch_settings_api.py -v
```
Expected: PASS (6/6).

**Step 6: Commit**

```bash
git add server/api/dispatch_settings.py server/main.py tests/test_dispatch_settings_api.py
git commit -m "dispatch: settings GET/PUT API with in-process cache"
```

---

## Task 3: `/api/dispatch/nearest` honours settings

**Files:**
- Modify: `server/api/dispatch.py` (`find_nearest`)
- Test: `tests/test_dispatch_nearest_settings.py`

**Step 1: Write the failing test**

```python
# tests/test_dispatch_nearest_settings.py
import pytest
from unittest.mock import patch
from httpx import AsyncClient


class _FakePosition:
    def __init__(self, device_id, name, lat, lng):
        self.device_id = device_id
        self.device_name = name
        self.latitude = lat
        self.longitude = lng
        self.timestamp = "2026-04-21T12:00:00Z"
        self.accuracy = 5
        self.battery = 90


def _fake_positions():
    # Centre = (38.72, -9.14). Build 12 positions at increasing distance.
    base_lat, base_lng = 38.72, -9.14
    return [
        _FakePosition(i, f"w{i}", base_lat + 0.001 * i, base_lng)
        for i in range(1, 13)
    ]


@pytest.mark.asyncio
async def test_nearest_respects_max_workers(admin_client: AsyncClient):
    # Default seed = 10. Verify the cap.
    with patch("server.api.dispatch.TraccarClient") as MockTC:
        MockTC.return_value.get_positions.return_value = _fake_positions()
        # Re-bind the static method used by the route:
        from server.traccar_client import TraccarClient as RealTC
        MockTC.haversine_distance = staticmethod(RealTC.haversine_distance)
        r = await admin_client.get("/api/dispatch/nearest?lat=38.72&lng=-9.14")
    assert r.status_code == 200
    assert len(r.json()) == 10


@pytest.mark.asyncio
async def test_nearest_respects_lower_max_workers(admin_client: AsyncClient):
    # Reduce cap, then check.
    await admin_client.put("/api/dispatch/settings", json={"max_workers": 3})
    with patch("server.api.dispatch.TraccarClient") as MockTC:
        MockTC.return_value.get_positions.return_value = _fake_positions()
        from server.traccar_client import TraccarClient as RealTC
        MockTC.haversine_distance = staticmethod(RealTC.haversine_distance)
        r = await admin_client.get("/api/dispatch/nearest?lat=38.72&lng=-9.14")
    assert len(r.json()) == 3


@pytest.mark.asyncio
async def test_nearest_respects_radius(admin_client: AsyncClient):
    # 500 m radius. At lat 38.72, 0.001 deg lat ≈ 111 m, so positions
    # 1..4 are within 500 m, 5..12 are not.
    await admin_client.put("/api/dispatch/settings", json={"search_radius_m": 500})
    with patch("server.api.dispatch.TraccarClient") as MockTC:
        MockTC.return_value.get_positions.return_value = _fake_positions()
        from server.traccar_client import TraccarClient as RealTC
        MockTC.haversine_distance = staticmethod(RealTC.haversine_distance)
        r = await admin_client.get("/api/dispatch/nearest?lat=38.72&lng=-9.14")
    body = r.json()
    assert all(w["distance_m"] <= 500 for w in body)
    assert len(body) <= 4
```

**Step 2: Run test, expect fail**

```bash
python3 -m pytest tests/test_dispatch_nearest_settings.py -v
```
Expected: tests fail because cap is hardcoded to `[:10]` and there's no radius filter.

**Step 3: Update `find_nearest`**

Modify `server/api/dispatch.py` `find_nearest()`:

```python
@router.get("/nearest")
async def find_nearest(
    lat: float,
    lng: float,
    db: AsyncSession = Depends(get_db),
    _admin: dict = Depends(get_current_admin),
):
    """Find nearest workers to a given GPS location.
    Uses explicit user-device links where available, falls back to name matching.
    Honours dispatch_settings.max_workers and search_radius_m.
    """
    from server.api.dispatch_settings import get_cached as _settings

    settings = await _settings(db)
    client = TraccarClient()
    positions = await client.get_positions()

    result = await db.execute(select(User).where(User.traccar_device_id.isnot(None)))
    device_to_user = {u.traccar_device_id: u.username for u in result.scalars().all()}

    radius = settings["search_radius_m"]
    results = []
    for p in positions:
        if p.latitude == 0 and p.longitude == 0:
            continue
        username = device_to_user.get(p.device_id, p.device_name)
        distance = TraccarClient.haversine_distance(lat, lng, p.latitude, p.longitude)
        if radius is not None and distance > radius:
            continue
        results.append({
            "username": username,
            "distance_m": round(distance),
            "latitude": p.latitude,
            "longitude": p.longitude,
            "timestamp": p.timestamp,
        })
    results.sort(key=lambda x: x["distance_m"])
    return results[: settings["max_workers"]]
```

**Step 4: Run tests**

```bash
docker compose up -d --build admin
python3 -m pytest tests/test_dispatch_nearest_settings.py tests/test_dispatch_settings_api.py -v
```
Expected: all PASS.

**Step 5: Commit**

```bash
git add server/api/dispatch.py tests/test_dispatch_nearest_settings.py
git commit -m "dispatch: /nearest honours max_workers + search_radius_m"
```

---

## Task 4: Canned messages CRUD API

**Files:**
- Modify: `server/models.py` (append `DispatchCannedMessage`)
- Create: `server/api/dispatch_messages.py`
- Modify: `server/main.py` (register router)
- Test: `tests/test_dispatch_messages_api.py`

**Step 1: Write the failing test**

```python
# tests/test_dispatch_messages_api.py
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
```

**Step 2: Run test, expect fail**

Expected: 404s.

**Step 3: Add the model**

Append to `server/models.py`:

```python
class DispatchCannedMessage(Base):
    """Admin-managed canned messages for the dispatch modal dropdown."""
    __tablename__ = "dispatch_canned_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    label: Mapped[str] = mapped_column(String(64), nullable=False)
    message: Mapped[str] = mapped_column(String(500), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
```

**Step 4: Implement the router**

Create `server/api/dispatch_messages.py`:

```python
"""CRUD for admin-managed canned dispatch messages.

The merged Dispatch page's send modal renders these as a dropdown so the
operator can pick "Pickup ready" instead of typing it every time. Free-text
fallback is always available.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.auth import get_current_admin
from server.database import get_db
from server.features_gate import requires_feature
from server.models import DispatchCannedMessage

router = APIRouter(
    prefix="/api/dispatch/messages",
    tags=["dispatch"],
    dependencies=[requires_feature("dispatch")],
)


class MessageCreate(BaseModel):
    label: str = Field(min_length=1, max_length=64)
    message: str = Field(min_length=1, max_length=500)
    sort_order: int = 0


class MessageUpdate(BaseModel):
    label: str | None = Field(default=None, min_length=1, max_length=64)
    message: str | None = Field(default=None, min_length=1, max_length=500)
    sort_order: int | None = None


class MessageResponse(BaseModel):
    id: int
    label: str
    message: str
    sort_order: int

    model_config = {"from_attributes": True}


@router.get("", response_model=list[MessageResponse])
async def list_messages(
    db: AsyncSession = Depends(get_db),
    _admin: dict = Depends(get_current_admin),
):
    result = await db.execute(
        select(DispatchCannedMessage)
        .order_by(DispatchCannedMessage.sort_order, DispatchCannedMessage.id)
    )
    return result.scalars().all()


@router.post("", response_model=MessageResponse, status_code=status.HTTP_201_CREATED)
async def create_message(
    body: MessageCreate,
    db: AsyncSession = Depends(get_db),
    _admin: dict = Depends(get_current_admin),
):
    msg = DispatchCannedMessage(
        label=body.label, message=body.message, sort_order=body.sort_order
    )
    db.add(msg)
    await db.commit()
    await db.refresh(msg)
    return msg


@router.patch("/{message_id}", response_model=MessageResponse)
async def update_message(
    message_id: int,
    body: MessageUpdate,
    db: AsyncSession = Depends(get_db),
    _admin: dict = Depends(get_current_admin),
):
    result = await db.execute(
        select(DispatchCannedMessage).where(DispatchCannedMessage.id == message_id)
    )
    msg = result.scalar_one_or_none()
    if msg is None:
        raise HTTPException(status_code=404, detail="Message not found")
    if body.label is not None:
        msg.label = body.label
    if body.message is not None:
        msg.message = body.message
    if body.sort_order is not None:
        msg.sort_order = body.sort_order
    await db.commit()
    await db.refresh(msg)
    return msg


@router.delete("/{message_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_message(
    message_id: int,
    db: AsyncSession = Depends(get_db),
    _admin: dict = Depends(get_current_admin),
):
    result = await db.execute(
        select(DispatchCannedMessage).where(DispatchCannedMessage.id == message_id)
    )
    msg = result.scalar_one_or_none()
    if msg is None:
        raise HTTPException(status_code=404, detail="Message not found")
    await db.delete(msg)
    await db.commit()
```

**Step 5: Register the router**

In `server/main.py`:

```python
from server.api.dispatch_messages import router as dispatch_messages_router
# ...
app.include_router(dispatch_messages_router)
```

**Step 6: Run tests**

```bash
docker compose up -d --build admin
python3 -m pytest tests/test_dispatch_messages_api.py -v
```
Expected: PASS (5/5).

**Step 7: Commit**

```bash
git add server/models.py server/api/dispatch_messages.py server/main.py tests/test_dispatch_messages_api.py
git commit -m "dispatch: canned-messages CRUD API"
```

---

## Task 5: Dashboard tab restructure

**Files:**
- Modify: `server/dashboard/index.html`

This task is HTML-only (no JS rewrites yet). Goal: remove the Map sub-tab, add the Setup sub-tab, leave the merged Dispatch page as a placeholder we'll fill in Task 6.

**Step 1: Remove the Map sub-tab from the Live group nav**

Find (around line 817):
```html
<div class="sub-tab" data-tab="map" onclick="switchTab('map')">Map</div>
```
Delete this line.

**Step 2: Add the Setup sub-tab to the System group nav**

Find (around line 831, right after the Features sub-tab):
```html
<div class="sub-tab" data-tab="features" onclick="switchTab('features')">Features</div>
```
Add immediately below it:
```html
<div class="sub-tab" data-tab="dispatch-setup" onclick="switchTab('dispatch-setup')" data-feature="dispatch">Dispatch Setup</div>
```

**Step 3: Delete the old Map content block**

Find (lines 928-938, the entire `<div class="content tab-content" id="tab-map">` ... `</div>` block) and delete it. The merged page will absorb its functionality in Task 6.

**Step 4: Add the placeholder Setup content block**

After the existing `tab-features` content block (line ~1002), add:

```html
<!-- Dispatch Setup Tab -->
<div class="content tab-content" id="tab-dispatch-setup" style="display:none" role="tabpanel" aria-label="Dispatch Setup" data-feature="dispatch">
    <div class="card">
        <h2>Map Home</h2>
        <p style="font-size:0.875rem;color:var(--fg-muted);margin-bottom:var(--space-md)">Default map centre + zoom for the Dispatch page.</p>
        <div id="mapHomeForm" class="form-row">
            <input type="number" step="0.0001" id="mapHomeLat" placeholder="Latitude" style="flex:1">
            <input type="number" step="0.0001" id="mapHomeLng" placeholder="Longitude" style="flex:1">
            <input type="number" id="mapHomeZoom" placeholder="Zoom (1-19)" min="1" max="19" style="flex:1">
            <button class="btn primary" onclick="saveMapHome()">Save</button>
        </div>
    </div>

    <div class="card">
        <h2>Search Behaviour</h2>
        <p style="font-size:0.875rem;color:var(--fg-muted);margin-bottom:var(--space-md)">How many workers to return per dispatch search, and the optional radius filter (0 = no limit).</p>
        <div class="form-row">
            <label style="flex:1">Max workers
                <input type="number" id="maxWorkers" min="1" max="50">
            </label>
            <label style="flex:1">Search radius (m)
                <input type="number" id="searchRadius" min="0" placeholder="0 = unlimited">
            </label>
            <button class="btn primary" onclick="saveSearchBehaviour()">Save</button>
        </div>
    </div>

    <div class="card">
        <h2>Saved Locations</h2>
        <p style="font-size:0.875rem;color:var(--fg-muted);margin-bottom:var(--space-md)">Pre-configure common dispatch locations for one-click access from the Dispatch page.</p>
        <div class="form-row" style="margin-bottom:var(--space-md)">
            <input type="text" id="newLocName" placeholder="Location name" style="flex:2">
            <input type="text" id="newLocCoords" placeholder="Lat, Lng (e.g., 38.72, -9.14)" style="flex:2">
            <button class="btn primary" onclick="addSavedLocation()">Add Location</button>
        </div>
        <div class="table-wrap">
            <table>
                <thead><tr><th>Name</th><th>Coordinates</th><th>Description</th><th>Actions</th></tr></thead>
                <tbody id="savedLocationsTable"></tbody>
            </table>
        </div>
    </div>

    <div class="card">
        <h2>Canned Messages</h2>
        <p style="font-size:0.875rem;color:var(--fg-muted);margin-bottom:var(--space-md)">Pre-written dispatch messages, picked from a dropdown when sending.</p>
        <div class="form-row" style="margin-bottom:var(--space-md)">
            <input type="text" id="newCannedLabel" placeholder="Label (e.g. Pickup ready)" style="flex:1">
            <input type="text" id="newCannedMessage" placeholder="Message text" style="flex:2">
            <input type="number" id="newCannedOrder" placeholder="Order" value="0" style="width:80px">
            <button class="btn primary" onclick="addCannedMessage()">Add</button>
        </div>
        <div class="table-wrap">
            <table>
                <thead><tr><th>Label</th><th>Message</th><th>Order</th><th>Actions</th></tr></thead>
                <tbody id="cannedMessagesTable"></tbody>
            </table>
        </div>
    </div>
</div>
```

**Step 5: Remove the now-duplicate Saved Locations card from `tab-dispatch`**

Find the `<div class="card">` containing the `Saved Locations` `<h2>` (around line 1040 in the existing dispatch tab) and delete that entire card. Quick-pick chips and the dispatch UX stay; only the management table is removed (it now lives on Setup).

**Step 6: Manual smoke**

```bash
docker compose up -d --build admin
```
Open `http://localhost:8000/dashboard/` (or whichever URL you use), log in, verify:
- Live group: Overview · Dispatch (no Map).
- System group: Admins · Audit Log · Features · Dispatch Setup.
- Clicking Dispatch Setup shows the four placeholder cards (the Saved Locations table will be empty since the JS isn't rewired yet — that's fine for this task).

**Step 7: Commit**

```bash
git add server/dashboard/index.html
git commit -m "dashboard: tab restructure — remove Map sub-tab, add Dispatch Setup"
```

---

## Task 6: Merged Dispatch page (map + nearest list + recent + settings-driven home)

**Files:**
- Modify: `server/dashboard/index.html`

**Step 1: Embed the map inside the existing `tab-dispatch` content block**

Find the `tab-dispatch` content block (around line 1006). Restructure it so the cards are:

1. Quick-dispatch chips (existing `#savedLocationsBar`).
2. Search bar + Find Nearest button (existing).
3. **New:** Map card (full-width, 500 px) — copied from the deleted `tab-map` block.
4. Nearest Workers card (existing).
5. Recent Dispatches card (existing).

Insert this map card directly between the search bar card and the `#dispatchResults` card:

```html
<div class="card" style="padding:0;overflow:hidden">
    <div id="mapContainer" style="height:500px;width:100%"></div>
</div>
```

**Step 2: Wire `initMap()` to the dispatch tab**

Find `switchTab` (around line 1579) — currently:
```js
if (name === 'map') { setTimeout(function() { initMap(); if (map) map.invalidateSize(); }, 100); }
```
Change to:
```js
if (name === 'dispatch') { setTimeout(function() { initMap(); if (map) map.invalidateSize(); }, 100); }
```

**Step 3: Add `fetchDispatchSettings()` and use it in `initMap()`**

Add near the top of the `<script>` block (with the other `let _capabilities = ...` lines):

```js
let _dispatchSettings = null;

async function fetchDispatchSettings() {
    try {
        const r = await fetch('/api/dispatch/settings');
        if (r.ok) _dispatchSettings = await r.json();
    } catch (e) { console.warn('dispatch settings fetch failed:', e); }
}
```

Call `fetchDispatchSettings()` immediately after `fetchCapabilities()` on login success.

In `initMap()` (around line 2404), replace:
```js
map = L.map('mapContainer').setView([52.37, 4.89], 10); // Default: Netherlands
```
with:
```js
const home = _dispatchSettings || { map_home_lat: 38.72, map_home_lng: -9.14, map_home_zoom: 11 };
map = L.map('mapContainer').setView([home.map_home_lat, home.map_home_lng], home.map_home_zoom);
```

**Step 4: Drop a target pin on dispatch-search**

Add a module-scope `let targetMarker = null;`. After the search resolves to (lat, lng) in `searchDispatchLocation`, but before populating the results table, add:

```js
if (map) {
    if (targetMarker) map.removeLayer(targetMarker);
    targetMarker = L.marker([lat, lng], {
        icon: L.divIcon({ className: 'target-marker', iconSize: [20, 20] })
    }).addTo(map).bindTooltip(locationName, { permanent: true, direction: 'top', offset: [0, -10] });
    map.setView([lat, lng], Math.max(map.getZoom(), 13));
}
```

Add CSS (in the `<style>` block, near the `.leaflet-...` rules):
```css
.target-marker {
    width: 20px; height: 20px;
    background: var(--amber); border: 2px solid var(--amber-ink);
    border-radius: 50%;
    box-shadow: 0 0 0 4px rgba(255, 191, 0, 0.3);
}
```

**Step 5: Replace `showDispatchPanel` clicks with the search flow**

Old `map.on('click', ...)` calls `showDispatchPanel(lat, lng)` which renders to the now-removed `#nearestList`. Change the handler to drive the same path as the search bar — set `dispatchLocation`'s value and call `searchDispatchLocation`:

In `initMap()`:
```js
map.on('click', function(e) {
    document.getElementById('dispatchLocation').value =
        e.latlng.lat.toFixed(5) + ', ' + e.latlng.lng.toFixed(5);
    searchDispatchLocation('Map click');
});
```

Delete the now-unused `showDispatchPanel` and `dispatchWorker` functions if `dispatchWorker` is also unreferenced (check first — `dispatchWorker` may be used elsewhere; if so, leave it).

**Step 6: Manual smoke**

```bash
docker compose up -d --build admin
```
- Reload dashboard.
- Map appears centred on Lisbon.
- Type "Lisbon airport" or click the map → target pin drops, nearest workers list populates.
- Recent Dispatches table still renders.

**Step 7: Commit**

```bash
git add server/dashboard/index.html
git commit -m "dashboard: merge Map into Dispatch — embedded map + target pin + settings home"
```

---

## Task 7: Setup page wiring (Map Home, Search Behaviour, Saved Locations move, Canned Messages)

**Files:**
- Modify: `server/dashboard/index.html`

**Step 1: Map Home — load + save**

Add near the other render functions:

```js
async function renderDispatchSetup() {
    // Load settings into the form
    const s = await api('/api/dispatch/settings');
    if (s) {
        document.getElementById('mapHomeLat').value = s.map_home_lat;
        document.getElementById('mapHomeLng').value = s.map_home_lng;
        document.getElementById('mapHomeZoom').value = s.map_home_zoom;
        document.getElementById('maxWorkers').value = s.max_workers;
        document.getElementById('searchRadius').value = s.search_radius_m || 0;
    }
    refreshSavedLocations();      // re-uses existing function
    refreshCannedMessages();      // new, defined below
}

async function saveMapHome() {
    const body = {
        map_home_lat: parseFloat(document.getElementById('mapHomeLat').value),
        map_home_lng: parseFloat(document.getElementById('mapHomeLng').value),
        map_home_zoom: parseInt(document.getElementById('mapHomeZoom').value, 10),
    };
    const r = await api('/api/dispatch/settings', { method: 'PUT', body: JSON.stringify(body) });
    if (r) {
        _dispatchSettings = r;
        showToast('Map home saved', 'success');
    }
}

async function saveSearchBehaviour() {
    const radiusVal = parseInt(document.getElementById('searchRadius').value, 10) || 0;
    const body = {
        max_workers: parseInt(document.getElementById('maxWorkers').value, 10),
        search_radius_m: radiusVal === 0 ? null : radiusVal,
    };
    const r = await api('/api/dispatch/settings', { method: 'PUT', body: JSON.stringify(body) });
    if (r) {
        _dispatchSettings = r;
        showToast('Search behaviour saved', 'success');
    }
}
```

**Step 2: Wire `switchTab` to call `renderDispatchSetup` when switching to it**

In `switchTab`, add a case for `'dispatch-setup'`:
```js
if (name === 'dispatch-setup') renderDispatchSetup();
```

**Step 3: Canned messages render + CRUD**

```js
async function refreshCannedMessages() {
    const data = await api('/api/dispatch/messages');
    if (!data) return;
    window._cannedMessages = data;
    const tbody = document.getElementById('cannedMessagesTable');
    if (data.length === 0) {
        tbody.innerHTML = '<tr><td colspan="4"><div class="empty-state"><p>No canned messages yet.</p></div></td></tr>';
        return;
    }
    tbody.innerHTML = data.map(m =>
        '<tr>' +
        '<td><strong>' + esc(m.label) + '</strong></td>' +
        '<td>' + esc(m.message) + '</td>' +
        '<td style="font-family:monospace">' + m.sort_order + '</td>' +
        '<td><button class="btn" onclick="deleteCannedMessage(' + m.id + ')" style="font-size:0.75rem;color:var(--color-danger)">Delete</button></td>' +
        '</tr>'
    ).join('');
}

async function addCannedMessage() {
    const label = document.getElementById('newCannedLabel').value.trim();
    const message = document.getElementById('newCannedMessage').value.trim();
    const sort_order = parseInt(document.getElementById('newCannedOrder').value, 10) || 0;
    if (!label || !message) { showToast('Label and message are required', 'error'); return; }
    const r = await api('/api/dispatch/messages', {
        method: 'POST',
        body: JSON.stringify({ label, message, sort_order }),
    });
    if (r) {
        showToast('Canned message added', 'success');
        document.getElementById('newCannedLabel').value = '';
        document.getElementById('newCannedMessage').value = '';
        document.getElementById('newCannedOrder').value = '0';
        refreshCannedMessages();
    }
}

async function deleteCannedMessage(id) {
    if (!confirm('Delete this canned message?')) return;
    await api('/api/dispatch/messages/' + id, { method: 'DELETE' });
    refreshCannedMessages();
}
```

**Step 4: Manual smoke**

```bash
docker compose up -d --build admin
```
- Open System → Dispatch Setup.
- Map Home: change zoom to 13, save → reload → still 13. Reload Dispatch page → map opens at zoom 13.
- Search Behaviour: set max_workers=3, save → run a dispatch search → list shows ≤ 3.
- Saved Locations: add a location → quick-pick chip appears on Dispatch page after a `refreshSavedLocations()` (will trigger on next Dispatch tab open).
- Canned Messages: add "Pickup ready" / "Pickup ready at the gate" → row appears.

**Step 5: Commit**

```bash
git add server/dashboard/index.html
git commit -m "dashboard: Dispatch Setup page wires all 4 cards to the new APIs"
```

---

## Task 8: Dispatch modal with canned-message dropdown

**Files:**
- Modify: `server/dashboard/index.html`

**Step 1: Replace the `prompt(...)` flow in `sendDispatch`**

Find the existing `sendDispatch(username, lat, lng)` (around line 2367):
```js
async function sendDispatch(username, lat, lng) {
    const message = prompt('Dispatch message for ' + username + ':');
    if (!message) return;
    // ...
}
```

Replace with:
```js
function sendDispatch(username, lat, lng) {
    openDispatchModal(username, lat, lng);
}
```

**Step 2: Add the modal HTML**

Append this near the end of `<body>` (alongside any other modals):

```html
<div id="dispatchModal" class="modal" style="display:none">
    <div class="modal-content" style="max-width:500px">
        <h2 id="dispatchModalTitle">Dispatch</h2>
        <label style="display:block;margin-top:var(--space-md)">Pick a canned message
            <select id="dispatchModalCanned" onchange="onCannedPicked()" style="width:100%">
                <option value="">— Custom message —</option>
            </select>
        </label>
        <label style="display:block;margin-top:var(--space-md)">Message
            <textarea id="dispatchModalMessage" rows="3" style="width:100%"></textarea>
        </label>
        <div class="form-row" style="margin-top:var(--space-md);justify-content:flex-end">
            <button class="btn" onclick="closeDispatchModal()">Cancel</button>
            <button class="btn primary" onclick="confirmDispatch()">Send</button>
        </div>
    </div>
</div>
```

**Step 3: Modal logic**

```js
let _dispatchTarget = null;  // { username, lat, lng }

async function openDispatchModal(username, lat, lng) {
    _dispatchTarget = { username, lat, lng };
    document.getElementById('dispatchModalTitle').textContent = 'Dispatch ' + username;
    document.getElementById('dispatchModalMessage').value = '';
    document.getElementById('dispatchModalCanned').value = '';
    // Populate the dropdown
    const messages = await api('/api/dispatch/messages') || [];
    window._cannedMessages = messages;
    const sel = document.getElementById('dispatchModalCanned');
    sel.innerHTML = '<option value="">— Custom message —</option>' +
        messages.map(m => '<option value="' + m.id + '">' + esc(m.label) + '</option>').join('');
    document.getElementById('dispatchModal').style.display = 'flex';
}

function closeDispatchModal() {
    document.getElementById('dispatchModal').style.display = 'none';
    _dispatchTarget = null;
}

function onCannedPicked() {
    const id = parseInt(document.getElementById('dispatchModalCanned').value, 10);
    const msg = (window._cannedMessages || []).find(m => m.id === id);
    if (msg) document.getElementById('dispatchModalMessage').value = msg.message;
}

async function confirmDispatch() {
    const message = document.getElementById('dispatchModalMessage').value.trim();
    if (!message) { showToast('Message is required', 'error'); return; }
    const t = _dispatchTarget;
    closeDispatchModal();
    await api('/api/dispatch', {
        method: 'POST',
        body: JSON.stringify({
            target_username: t.username, message,
            latitude: t.lat, longitude: t.lng,
        }),
    });
    showToast('Dispatched ' + t.username, 'success');
    if (typeof loadRecentDispatches === 'function') loadRecentDispatches();
}
```

**Step 4: Add basic modal CSS** (if not already in the stylesheet)

```css
.modal {
    position: fixed; inset: 0;
    background: rgba(0, 0, 0, 0.5);
    display: flex; align-items: center; justify-content: center;
    z-index: 1000;
}
.modal-content {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius-md);
    padding: var(--space-lg);
    width: 90%;
}
```
(Skip if a `.modal` style already exists.)

**Step 5: Manual smoke**

- Add a canned message via Setup.
- Run a dispatch search on Dispatch page.
- Click "Dispatch" on a worker row → modal opens → dropdown shows the canned message → picking it fills the textarea → Send works.
- Test free-text path too: leave dropdown on "Custom message", type a message, send.

**Step 6: Commit**

```bash
git add server/dashboard/index.html
git commit -m "dashboard: dispatch modal with canned-message dropdown + free-text"
```

---

## Task 9: Deploy + verify on `ptt.harro.ch`

**Step 1: Push to origin**

```bash
git push origin main
```

**Step 2: Deploy on the VPS**

```bash
ssh -i ~/.ssh/id_ed25519_ptt root@ptt.harro.ch
cd /root/ptt  # or wherever the deploy lives — confirm with `ls`
git pull
docker compose up -d --build admin
docker exec ptt-admin-1 alembic upgrade head
docker exec ptt-admin-1 alembic current  # expect e1c4f8a3b5d6
exit
```

**Step 3: Verify on prod**

Open `https://ptt.harro.ch`, log in (`admin` / `xYf3huRzivc6Zl6`):

1. **Tab structure** — Live group has Overview · Dispatch (no Map). System group has Admins · Audit Log · Features · Dispatch Setup.
2. **Map default** — Dispatch page opens; map centred on Lisbon.
3. **Setup → Map Home** — change zoom to 12, save, reload Dispatch → opens at zoom 12. Restore to 11.
4. **Setup → Search Behaviour** — set max_workers=3, save → run a search → list shows ≤ 3 results.
5. **Setup → Saved Locations** — add "Office" with valid coords → quick-pick chip appears on Dispatch page.
6. **Setup → Canned Messages** — add "Pickup ready" → "Pickup ready at the gate".
7. **Dispatch flow end-to-end** — search an address → target pin drops on map → click Dispatch on a worker row → modal opens with the canned message in the dropdown → pick it → Send → toast confirms → Recent Dispatches table updates.
8. **Capabilities gate** — flip dispatch off via System → Features → confirm both Live's Dispatch sub-tab AND System's Dispatch Setup sub-tab disappear (both have `data-feature="dispatch"`). Re-enable.

**Step 4: Smoke against a real worker (if one is online)**

If a P50 is online with a Traccar device link, run an actual dispatch and confirm the worker receives it (TTS whisper or text fallback in their channel).

**Step 5: Tag and announce**

No git tag this phase; just confirm `git log --oneline -10` on prod matches origin/main.

---

## Verification checklist (all phases)

After Task 9:

- [ ] `docker exec ptt-admin-1 alembic current` → `e1c4f8a3b5d6`.
- [ ] `curl https://ptt.harro.ch/api/dispatch/settings` returns Lisbon defaults (or whatever the operator set).
- [ ] `Live → Map` sub-tab is gone.
- [ ] `System → Dispatch Setup` exists with four cards.
- [ ] Map default reads from settings; persists across reloads.
- [ ] `max_workers` cap works end-to-end.
- [ ] `search_radius_m` filter works end-to-end.
- [ ] Saved Locations CRUD works from Setup; quick-pick chips render on Dispatch.
- [ ] Canned Messages CRUD works; dropdown populates the dispatch modal.
- [ ] Real dispatch end-to-end on prod (search → modal → send → toast → Recent log).
- [ ] All pytest tests pass: `python3 -m pytest tests/ -v` (no regressions).

All ten = Phase 6 ship.

---

## Open questions / deferred

1. **Halo markers on top-N closest workers** — design called for the visual highlight; explicitly deferred to a later task. Ship the list-only view first.
2. **Hover-row ↔ marker pulse** — nice-to-have, deferred.
3. **Saved-location reorder UI** — `sort_order` field exists; UI for it deferred until anyone asks.
4. **OSM driving routes / ETA** — explicitly out of scope for this phase. The earlier sketch can be picked up later if demand emerges.

---

## Dependencies + parallelization

- Tasks 1 → 2 → 3 must run in order (model → API → consumer).
- Task 4 is independent of 1-3; can ship in any order after 1.
- Tasks 5 → 6 → 7 → 8 are dashboard tasks and stack in order.
- Task 9 is the final deploy gate.

If working solo: do them in order. The whole plan is ~6-10 hours of focused work.
