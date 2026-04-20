from __future__ import annotations

import pytest
from sqlalchemy import update

from server.features import FEATURE_KEYS, is_enabled, refresh_cache
from server.models import FeatureFlag


@pytest.mark.asyncio
async def test_is_enabled_reads_from_cache_after_refresh(db_session):
    await refresh_cache(db_session)
    assert is_enabled("lone_worker") is True


@pytest.mark.asyncio
async def test_unknown_key_returns_false(db_session):
    await refresh_cache(db_session)
    assert is_enabled("nonexistent") is False


@pytest.mark.asyncio
async def test_disabling_propagates_after_refresh(db_session):
    await db_session.execute(
        update(FeatureFlag).where(FeatureFlag.key == "sip").values(enabled=False)
    )
    await db_session.commit()
    await refresh_cache(db_session)
    assert is_enabled("sip") is False
    assert is_enabled("lone_worker") is True  # unaffected


def test_feature_keys_canonical():
    assert FEATURE_KEYS == ("lone_worker", "sip", "dispatch", "weather", "sos")
