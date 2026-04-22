import pytest
from sqlalchemy import select
from server.models import User


@pytest.mark.asyncio
async def test_user_cert_hash_defaults_null(db_session):
    u = User(username="alice", mumble_password="x")
    db_session.add(u)
    await db_session.commit()
    await db_session.refresh(u)
    assert u.mumble_cert_hash is None
    assert u.mumble_registered_user_id is None


@pytest.mark.asyncio
async def test_user_cert_hash_roundtrip(db_session):
    u = User(
        username="bob",
        mumble_password="x",
        mumble_cert_hash="abc123" * 6,
        mumble_registered_user_id=42,
    )
    db_session.add(u)
    await db_session.commit()
    await db_session.refresh(u)
    assert u.mumble_cert_hash == "abc123" * 6
    assert u.mumble_registered_user_id == 42


@pytest.mark.asyncio
async def test_cert_hash_unique_when_set(db_session):
    """Two users can't share a non-null cert hash."""
    db_session.add(User(username="u1", mumble_password="x",
                        mumble_cert_hash="deadbeef"))
    await db_session.commit()
    db_session.add(User(username="u2", mumble_password="x",
                        mumble_cert_hash="deadbeef"))
    with pytest.raises(Exception):
        await db_session.commit()
    await db_session.rollback()


@pytest.mark.asyncio
async def test_cert_hash_nulls_coexist(db_session):
    """Multiple users with NULL cert_hash are allowed (partial unique)."""
    db_session.add(User(username="u1", mumble_password="x"))
    db_session.add(User(username="u2", mumble_password="x"))
    await db_session.commit()  # should not raise
    rows = (await db_session.execute(
        select(User).where(User.mumble_cert_hash.is_(None))
    )).scalars().all()
    assert len(rows) >= 2


@pytest.mark.asyncio
async def test_registered_user_id_unique_when_set(db_session):
    """Two users can't share a non-null mumble_registered_user_id."""
    db_session.add(User(username="u1", mumble_password="x",
                        mumble_registered_user_id=7))
    await db_session.commit()
    db_session.add(User(username="u2", mumble_password="x",
                        mumble_registered_user_id=7))
    with pytest.raises(Exception):
        await db_session.commit()
    await db_session.rollback()
