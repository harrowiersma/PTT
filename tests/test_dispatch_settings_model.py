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
