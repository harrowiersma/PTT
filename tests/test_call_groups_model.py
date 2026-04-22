import pytest
from sqlalchemy import select
from server.models import CallGroup, UserCallGroup, User, Channel


@pytest.mark.asyncio
async def test_call_group_create(db_session):
    cg = CallGroup(name="Sales", description="Sales team")
    db_session.add(cg)
    await db_session.commit()
    await db_session.refresh(cg)
    assert cg.id is not None
    assert cg.name == "Sales"


@pytest.mark.asyncio
async def test_user_call_groups_join(db_session):
    """User in a group → join-table row visible via direct query."""
    cg = CallGroup(name="Sales")
    u = User(username="alice", mumble_password="x")
    db_session.add_all([cg, u])
    await db_session.commit()
    await db_session.refresh(cg)
    await db_session.refresh(u)

    db_session.add(UserCallGroup(user_id=u.id, call_group_id=cg.id))
    await db_session.commit()

    rows = (await db_session.execute(
        select(UserCallGroup).where(UserCallGroup.user_id == u.id)
    )).scalars().all()
    assert len(rows) == 1
    assert rows[0].call_group_id == cg.id


@pytest.mark.asyncio
async def test_channel_call_group_id_nullable(db_session):
    """A channel without a group_id is unrestricted (NULL)."""
    c = Channel(name="Root", description="default")
    db_session.add(c)
    await db_session.commit()
    await db_session.refresh(c)
    assert c.call_group_id is None


@pytest.mark.asyncio
async def test_channel_with_call_group(db_session):
    cg = CallGroup(name="Sales")
    db_session.add(cg)
    await db_session.commit()
    await db_session.refresh(cg)
    c = Channel(name="SalesChan", description="", call_group_id=cg.id)
    db_session.add(c)
    await db_session.commit()
    await db_session.refresh(c)
    assert c.call_group_id == cg.id


@pytest.mark.asyncio
async def test_call_group_unique_name(db_session):
    """name is unique — second insert raises."""
    db_session.add(CallGroup(name="Sales"))
    await db_session.commit()

    db_session.add(CallGroup(name="Sales"))
    with pytest.raises(Exception):  # IntegrityError or InvalidRequestError
        await db_session.commit()
    await db_session.rollback()
