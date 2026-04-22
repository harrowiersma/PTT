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

# Per-key defaults — applied when the DB row is missing (fresh deploy
# before seed migration) or when a new flag is added before its
# migration lands. Most flags default on; call_groups_hiding is the
# exception — it's opt-in so deploying the code doesn't flip the ACL.
FEATURE_DEFAULTS: Final[dict[str, bool]] = {
    "lone_worker": True,
    "sip": True,
    "dispatch": True,
    "weather": True,
    "sos": True,
    "call_groups_hiding": False,
}
FEATURE_KEYS: Final[tuple[str, ...]] = tuple(FEATURE_DEFAULTS.keys())

_cache: dict[str, bool] = dict(FEATURE_DEFAULTS)


def is_enabled(key: str) -> bool:
    """Return True if the named feature is enabled. Unknown keys = False."""
    return _cache.get(key, False)


async def refresh_cache(db: AsyncSession) -> dict[str, bool]:
    """Reload all flags from the DB into the in-process cache."""
    global _cache
    result = await db.execute(select(FeatureFlag.key, FeatureFlag.enabled))
    fresh = {row[0]: bool(row[1]) for row in result.all()}
    # Fall back to per-key defaults when a DB row is missing (defensive;
    # the seed migration covers every key in practice).
    for k, default in FEATURE_DEFAULTS.items():
        fresh.setdefault(k, default)
    _cache = fresh
    logger.info("feature-cache refreshed: %s", fresh)
    return fresh


def snapshot() -> dict[str, bool]:
    """Shallow copy of the current cache, for API responses."""
    return dict(_cache)
