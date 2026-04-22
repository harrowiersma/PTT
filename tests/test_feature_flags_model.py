from __future__ import annotations

import pytest
from sqlalchemy import select

from server.models import FeatureFlag


@pytest.mark.asyncio
async def test_feature_flags_seeded(db_session):
    result = await db_session.execute(
        select(FeatureFlag.key).order_by(FeatureFlag.key)
    )
    keys = [row[0] for row in result.all()]
    assert keys == [
        "call_groups_hiding", "dispatch", "lone_worker",
        "sip", "sos", "weather",
    ]


@pytest.mark.asyncio
async def test_feature_flag_defaults_enabled(db_session):
    result = await db_session.execute(
        select(FeatureFlag).where(FeatureFlag.key == "lone_worker")
    )
    flag = result.scalar_one()
    assert flag.enabled is True
