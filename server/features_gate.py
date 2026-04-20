"""FastAPI dependency that 503s requests when a feature is disabled.

Usage in a router:

    from server.features_gate import requires_feature

    router = APIRouter(prefix="/api/weather",
                       dependencies=[requires_feature("weather")])

Applied at the router level so every route in the module is covered by
one declaration.
"""
from __future__ import annotations

from fastapi import Depends, HTTPException

from server import features as _features


def requires_feature(key: str):
    def _check():
        if not _features.is_enabled(key):
            raise HTTPException(
                status_code=503,
                detail=f"Feature '{key}' is disabled by the administrator.",
            )
    return Depends(_check)
