"""Unit tests for channel-ACL helpers in server.murmur.admin_sqlite.

Tests mock _sqlite_exec and restart_murmur so no real docker/sqlite is
touched. Assertions cover:
  - DELETE-first replace semantics
  - deny-@all + per-member allow INSERT shape
  - exactly one restart per batched_acl_apply, regardless of size
  - clear_channel_acl only issues the DELETE
"""
from __future__ import annotations

from unittest.mock import patch

import pytest


@pytest.fixture
def fake_sqlite():
    calls: list[str] = []

    def _fake(sql: str) -> str:
        calls.append(sql)
        return ""

    with patch("server.murmur.admin_sqlite._sqlite_exec", side_effect=_fake):
        yield calls


@pytest.fixture
def fake_restart():
    with patch("server.murmur.admin_sqlite.restart_murmur") as m:
        yield m


def test_set_channel_acl_denies_all_and_grants_members(fake_sqlite, fake_restart):
    from server.murmur.admin_sqlite import set_channel_acl

    set_channel_acl(9, [42, 43])

    calls = fake_sqlite
    # 1 DELETE + 1 deny-@all INSERT + 2 member INSERTs = 4 total.
    assert len(calls) == 4
    # First call must be the DELETE.
    assert calls[0].startswith("DELETE FROM acl")
    assert "channel_id=9" in calls[0]
    # Second call: deny-@all INSERT.
    assert "INSERT INTO acl" in calls[1]
    assert "'all'" in calls[1]
    # revokepriv should be Traverse|Enter = 0x06 = 6.
    assert ", 6)" in calls[1] or ", 6);" in calls[1]
    # Third/fourth: per-member allow INSERTs with user_id 42 and 43.
    member_sqls = [c for c in calls[2:] if "INSERT INTO acl" in c]
    assert len(member_sqls) == 2
    # grantpriv = Traverse|Enter|Speak = 0x0e = 14 on each.
    assert any(", 14, 0)" in s or ", 14, 0);" in s for s in member_sqls)
    # Both uids appear.
    assert any(", 42," in s for s in member_sqls)
    assert any(", 43," in s for s in member_sqls)
    # One restart.
    fake_restart.assert_called_once()


def test_set_channel_acl_empty_members_still_denies_all(fake_sqlite, fake_restart):
    """A channel with zero members still needs the deny-@all row so
    non-members lose Traverse+Enter."""
    from server.murmur.admin_sqlite import set_channel_acl

    set_channel_acl(7, [])

    calls = fake_sqlite
    # 1 DELETE + 1 deny-@all INSERT only.
    assert len(calls) == 2
    assert calls[0].startswith("DELETE FROM acl")
    assert "INSERT INTO acl" in calls[1]
    assert "'all'" in calls[1]
    fake_restart.assert_called_once()


def test_set_channel_acl_no_restart_when_requested(fake_sqlite, fake_restart):
    """restart=False must not touch murmur — used by batched_acl_apply."""
    from server.murmur.admin_sqlite import set_channel_acl

    set_channel_acl(11, [1], restart=False)
    fake_restart.assert_not_called()


def test_set_channel_acl_priorities_monotonic(fake_sqlite, fake_restart):
    """ACL rows must carry increasing priorities: deny-@all at 1, then
    members at 2, 3, 4, ... — the acl unique index forbids duplicates."""
    from server.murmur.admin_sqlite import set_channel_acl

    set_channel_acl(20, [100, 200, 300])

    calls = fake_sqlite
    inserts = [c for c in calls if "INSERT INTO acl" in c]
    # Deny-@all at priority=1, then members at 2, 3, 4.
    # The priority sits in the 3rd VALUES slot — look for it positionally.
    assert ", 20, 1," in inserts[0]  # deny-@all
    assert ", 20, 2," in inserts[1]
    assert ", 20, 3," in inserts[2]
    assert ", 20, 4," in inserts[3]


def test_clear_channel_acl_issues_only_delete(fake_sqlite, fake_restart):
    from server.murmur.admin_sqlite import clear_channel_acl

    clear_channel_acl(5)

    calls = fake_sqlite
    assert len(calls) == 1
    assert calls[0].startswith("DELETE FROM acl")
    assert "channel_id=5" in calls[0]
    fake_restart.assert_called_once()


def test_clear_channel_acl_no_restart(fake_sqlite, fake_restart):
    from server.murmur.admin_sqlite import clear_channel_acl

    clear_channel_acl(5, restart=False)
    fake_restart.assert_not_called()


def test_batched_acl_apply_one_restart(fake_sqlite, fake_restart):
    """Two channels changed → one murmur restart total, not two."""
    from server.murmur.admin_sqlite import batched_acl_apply

    batched_acl_apply([(9, [42, 43]), (10, [])])

    calls = fake_sqlite
    # Channel 9: 1 DELETE + 1 deny + 2 allows = 4.
    # Channel 10: 1 DELETE + 1 deny = 2.
    assert len(calls) == 6
    assert any("channel_id=9" in c for c in calls)
    assert any("channel_id=10" in c for c in calls)
    # Exactly ONE restart, regardless of change count.
    fake_restart.assert_called_once()


def test_batched_acl_apply_clear_via_none(fake_sqlite, fake_restart):
    """members=None means clear the ACL entirely (channel reverts to
    default visible-to-all)."""
    from server.murmur.admin_sqlite import batched_acl_apply

    batched_acl_apply([(9, None)])

    calls = fake_sqlite
    # One DELETE, no INSERTs.
    assert len(calls) == 1
    assert calls[0].startswith("DELETE FROM acl")
    assert "channel_id=9" in calls[0]
    fake_restart.assert_called_once()


def test_batched_acl_apply_empty_no_restart(fake_sqlite, fake_restart):
    """No changes → no sqlite, no restart."""
    from server.murmur.admin_sqlite import batched_acl_apply

    batched_acl_apply([])

    assert fake_sqlite == []
    fake_restart.assert_not_called()


def test_permission_bit_constants_canonical():
    """Regression guard — these are canonical Mumble ACL bits."""
    from server.murmur import admin_sqlite
    assert admin_sqlite._PERM_TRAVERSE == 0x02
    assert admin_sqlite._PERM_ENTER == 0x04
    assert admin_sqlite._PERM_SPEAK == 0x08
    assert admin_sqlite._DENY_TRAVERSE_ENTER == 0x06
    assert admin_sqlite._GRANT_MEMBER == 0x0E
