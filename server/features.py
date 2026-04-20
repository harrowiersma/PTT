"""Feature-flag service — single source of truth.

Backend reads this for: (a) lifespan task gating in main.py,
(b) requires_feature dependency on routers, (c) /api/status/capabilities.
The cache is in-process; refresh_cache() is called by the PUT endpoint
and once at startup. Readers (is_enabled) are lock-free — assignment of
a new dict is atomic under the GIL.
"""
from __future__ import annotations

import logging
from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.models import FeatureFlag

logger = logging.getLogger(__name__)

FEATURE_KEYS: Final[tuple[str, ...]] = (
    "lone_worker", "sip", "dispatch", "weather", "sos",
)

# Default to enabled so a fresh deploy without the migration still works.
_cache: dict[str, bool] = {k: True for k in FEATURE_KEYS}


def is_enabled(key: str) -> bool:
    """Return True if the named feature is enabled. Unknown keys = False."""
    return _cache.get(key, False)


async def refresh_cache(db: AsyncSession) -> dict[str, bool]:
    """Reload all flags from the DB into the in-process cache."""
    global _cache
    result = await db.execute(select(FeatureFlag.key, FeatureFlag.enabled))
    fresh = {row[0]: bool(row[1]) for row in result.all()}
    # Preserve defaults for any key missing from the DB (shouldn't happen
    # after the seed migration, but defensive).
    for k in FEATURE_KEYS:
        fresh.setdefault(k, True)
    _cache = fresh
    logger.info("feature-cache refreshed: %s", fresh)
    return fresh


def snapshot() -> dict[str, bool]:
    """Shallow copy of the current cache, for API responses."""
    return dict(_cache)
