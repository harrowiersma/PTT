import pytest
from unittest.mock import MagicMock, patch
from sqlalchemy import select
from server.models import User


@pytest.mark.asyncio
async def test_user_created_callback_sets_online(db_session):
    """Simulate USERCREATED on a DB-registered user; verify status flips to online."""
    u = User(username="harry", mumble_password="x")
    db_session.add(u)
    await db_session.commit()

    from server.murmur.client import _on_user_created_sync

    event = {"name": "harry"}
    _on_user_created_sync(event)

    await db_session.refresh(u)
    assert u.status_label == "online"


@pytest.mark.asyncio
async def test_user_created_skips_bot_usernames(db_session):
    from server.murmur.client import _on_user_created_sync
    # Bot users must be ignored — no DB work, no errors.
    for name in ("PTTAdmin", "PTTWeather", "PTTPhone-1"):
        _on_user_created_sync({"name": name})


@pytest.mark.asyncio
async def test_user_created_ignores_unknown_username(db_session):
    from server.murmur.client import _on_user_created_sync
    # Username not in DB — must no-op, not raise.
    _on_user_created_sync({"name": "ghost"})
