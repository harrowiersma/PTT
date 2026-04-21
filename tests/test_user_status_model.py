import pytest
from sqlalchemy import select
from server.models import User


@pytest.mark.asyncio
async def test_user_has_status_columns(db_session):
    """All four new columns exist and default to NULL for existing rows."""
    u = User(username="alice", mumble_password="x")
    db_session.add(u)
    await db_session.commit()
    await db_session.refresh(u)
    assert u.status_label is None
    assert u.status_updated_at is None
    assert u.is_audible is None
    assert u.is_audible_updated_at is None


@pytest.mark.asyncio
async def test_user_status_label_accepts_values(db_session):
    u = User(
        username="bob", mumble_password="x",
        status_label="online", is_audible=True,
    )
    db_session.add(u)
    await db_session.commit()
    await db_session.refresh(u)
    assert u.status_label == "online"
    assert u.is_audible is True
