"""Integration tests for ACL apply wiring in the call-groups endpoints.

The call-groups HTTP endpoints call admin_sqlite.batched_acl_apply after
the DB commit when the `call_groups_hiding` feature flag is on, so
Murmur's sqlite ACL mirrors the group↔channel mapping. When the flag
is off, the endpoints fall back to the bounce-only behaviour that
shipped previously — no sqlite writes, no murmur restart.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from server import features as _features
from server.models import CallGroup, Channel, User, UserCallGroup


def _stub_to_thread():
    """Patch asyncio.to_thread to call its target inline so tests can
    assert on the synchronous mock it wraps."""
    async def _run(fn, *args, **kwargs):
        return fn(*args, **kwargs)
    return patch("asyncio.to_thread", side_effect=_run)


@pytest.mark.asyncio
async def test_put_members_flag_off_skips_acl(
    admin_client: AsyncClient, db_session, monkeypatch,
):
    """Default state: flag off → no ACL calls, endpoint still succeeds."""
    monkeypatch.setitem(_features._cache, "call_groups_hiding", False)

    r = await admin_client.post("/api/call-groups", json={"name": "Sales"})
    gid = r.json()["id"]
    u = User(username="alice", mumble_password="x",
             mumble_registered_user_id=42)
    db_session.add(u)
    c = Channel(name="SalesChan", mumble_id=9, call_group_id=gid)
    db_session.add(c)
    await db_session.commit()
    await db_session.refresh(u)

    with patch("server.murmur.admin_sqlite.batched_acl_apply") as m_apply:
        r = await admin_client.put(
            f"/api/call-groups/{gid}/members",
            json={"user_ids": [u.id]},
        )
    assert r.status_code == 200
    m_apply.assert_not_called()


@pytest.mark.asyncio
async def test_put_members_flag_on_applies_acl(
    admin_client: AsyncClient, db_session, monkeypatch,
):
    """Flag on + tagged channel + registered member → ACL batch contains
    (channel_mumble_id, [member_uid]) with exactly one call."""
    monkeypatch.setitem(_features._cache, "call_groups_hiding", True)

    r = await admin_client.post("/api/call-groups", json={"name": "Sales"})
    gid = r.json()["id"]
    u = User(username="alice", mumble_password="x",
             mumble_registered_user_id=42)
    db_session.add(u)
    c = Channel(name="SalesChan", mumble_id=9, call_group_id=gid)
    db_session.add(c)
    await db_session.commit()
    await db_session.refresh(u)

    with _stub_to_thread(), patch(
        "server.murmur.admin_sqlite.batched_acl_apply"
    ) as m_apply:
        r = await admin_client.put(
            f"/api/call-groups/{gid}/members",
            json={"user_ids": [u.id]},
        )
    assert r.status_code == 200
    m_apply.assert_called_once()
    (changes,), _ = m_apply.call_args
    assert changes == [(9, [42])]


@pytest.mark.asyncio
async def test_put_members_skips_unregistered_members(
    admin_client: AsyncClient, db_session, monkeypatch,
):
    """Members without mumble_registered_user_id drop out of the allow
    list — the ACL can only reference users Murmur knows about."""
    monkeypatch.setitem(_features._cache, "call_groups_hiding", True)

    r = await admin_client.post("/api/call-groups", json={"name": "Sales"})
    gid = r.json()["id"]
    u_reg = User(
        username="alice", mumble_password="x",
        mumble_registered_user_id=42,
    )
    u_pending = User(username="bob", mumble_password="x")  # not registered
    db_session.add_all([u_reg, u_pending])
    c = Channel(name="SalesChan", mumble_id=9, call_group_id=gid)
    db_session.add(c)
    await db_session.commit()
    await db_session.refresh(u_reg)
    await db_session.refresh(u_pending)

    with _stub_to_thread(), patch(
        "server.murmur.admin_sqlite.batched_acl_apply"
    ) as m_apply:
        await admin_client.put(
            f"/api/call-groups/{gid}/members",
            json={"user_ids": [u_reg.id, u_pending.id]},
        )
    (changes,), _ = m_apply.call_args
    assert changes == [(9, [42])]


@pytest.mark.asyncio
async def test_put_members_skips_channels_without_mumble_id(
    admin_client: AsyncClient, db_session, monkeypatch,
):
    """Channels not yet mirrored to Murmur (mumble_id IS NULL) have
    nothing to apply — skip them."""
    monkeypatch.setitem(_features._cache, "call_groups_hiding", True)

    r = await admin_client.post("/api/call-groups", json={"name": "Sales"})
    gid = r.json()["id"]
    u = User(username="alice", mumble_password="x",
             mumble_registered_user_id=42)
    db_session.add(u)
    c_pending = Channel(name="PendingChan", call_group_id=gid)  # no mumble_id
    db_session.add(c_pending)
    await db_session.commit()
    await db_session.refresh(u)

    with _stub_to_thread(), patch(
        "server.murmur.admin_sqlite.batched_acl_apply"
    ) as m_apply:
        await admin_client.put(
            f"/api/call-groups/{gid}/members",
            json={"user_ids": [u.id]},
        )
    # No channels to touch → no batch call (saves a murmur restart).
    m_apply.assert_not_called()


@pytest.mark.asyncio
async def test_put_channels_adds_acl_on_new_channels(
    admin_client: AsyncClient, db_session, monkeypatch,
):
    """Channels newly assigned to the group get ACL applied with the
    group's current member uids. Channels removed get ACL cleared."""
    monkeypatch.setitem(_features._cache, "call_groups_hiding", True)

    r = await admin_client.post("/api/call-groups", json={"name": "Sales"})
    gid = r.json()["id"]
    u = User(username="alice", mumble_password="x",
             mumble_registered_user_id=42)
    db_session.add(u)
    c1 = Channel(name="Chan1", mumble_id=101, call_group_id=gid)  # in group
    c2 = Channel(name="Chan2", mumble_id=102)  # not in group yet
    db_session.add_all([c1, c2])
    await db_session.commit()
    for obj in (u, c1, c2):
        await db_session.refresh(obj)
    db_session.add(UserCallGroup(user_id=u.id, call_group_id=gid))
    await db_session.commit()

    with _stub_to_thread(), patch(
        "server.murmur.admin_sqlite.batched_acl_apply"
    ) as m_apply:
        # Replace: add Chan2, remove Chan1.
        await admin_client.put(
            f"/api/call-groups/{gid}/channels",
            json={"channel_ids": [c2.id]},
        )
    m_apply.assert_called_once()
    (changes,), _ = m_apply.call_args
    # Chan1 (101) removed → clear (None). Chan2 (102) added → members [42].
    change_map = dict(changes)
    assert change_map == {101: None, 102: [42]}


@pytest.mark.asyncio
async def test_delete_group_clears_acl(
    admin_client: AsyncClient, db_session, monkeypatch,
):
    """Deleting a group clears ACL on every channel currently tagged
    with it. The DB delete then cascades call_group_id→NULL."""
    monkeypatch.setitem(_features._cache, "call_groups_hiding", True)

    r = await admin_client.post("/api/call-groups", json={"name": "Sales"})
    gid = r.json()["id"]
    c1 = Channel(name="Chan1", mumble_id=101, call_group_id=gid)
    c2 = Channel(name="Chan2", mumble_id=102, call_group_id=gid)
    db_session.add_all([c1, c2])
    await db_session.commit()

    with _stub_to_thread(), patch(
        "server.murmur.admin_sqlite.batched_acl_apply"
    ) as m_apply:
        r = await admin_client.delete(f"/api/call-groups/{gid}")
    assert r.status_code == 204
    m_apply.assert_called_once()
    (changes,), _ = m_apply.call_args
    assert dict(changes) == {101: None, 102: None}


@pytest.mark.asyncio
async def test_delete_group_flag_off_skips_acl(
    admin_client: AsyncClient, db_session, monkeypatch,
):
    monkeypatch.setitem(_features._cache, "call_groups_hiding", False)

    r = await admin_client.post("/api/call-groups", json={"name": "Sales"})
    gid = r.json()["id"]
    c1 = Channel(name="Chan1", mumble_id=101, call_group_id=gid)
    db_session.add(c1)
    await db_session.commit()

    with patch("server.murmur.admin_sqlite.batched_acl_apply") as m_apply:
        r = await admin_client.delete(f"/api/call-groups/{gid}")
    assert r.status_code == 204
    m_apply.assert_not_called()


@pytest.mark.asyncio
async def test_put_channels_empty_with_flag_on(
    admin_client: AsyncClient, db_session, monkeypatch,
):
    """Empty channel list with flag on → every previously-assigned
    channel gets ACL cleared."""
    monkeypatch.setitem(_features._cache, "call_groups_hiding", True)

    r = await admin_client.post("/api/call-groups", json={"name": "Sales"})
    gid = r.json()["id"]
    c = Channel(name="Chan1", mumble_id=50, call_group_id=gid)
    db_session.add(c)
    await db_session.commit()

    with _stub_to_thread(), patch(
        "server.murmur.admin_sqlite.batched_acl_apply"
    ) as m_apply:
        r = await admin_client.put(
            f"/api/call-groups/{gid}/channels",
            json={"channel_ids": []},
        )
    assert r.status_code == 200
    (changes,), _ = m_apply.call_args
    assert dict(changes) == {50: None}
