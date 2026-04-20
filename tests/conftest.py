"""Shared test fixtures for PTT server tests."""

import asyncio
import os

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# Use SQLite for tests (no PostgreSQL dependency)
os.environ["PTT_DATABASE_URL"] = "sqlite+aiosqlite:///test.db"
os.environ["PTT_DATABASE_URL_SYNC"] = "sqlite:///test.db"
os.environ["PTT_SECRET_KEY"] = "test-secret-key-not-for-production"
os.environ["PTT_ADMIN_USERNAME"] = "testadmin"
os.environ["PTT_ADMIN_PASSWORD"] = "testpass123"
os.environ["PTT_MURMUR_HOST"] = "localhost"

from server.database import Base, async_session, engine, get_db
from server.main import app


@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture(autouse=True)
async def setup_db():
    """Create tables before each test, drop after."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    # Seed feature_flags to mirror the Alembic migration's INSERT loop so
    # tests see the same starting state as a freshly-deployed DB.
    try:
        from server.models import FeatureFlag

        async with async_session() as _seed:
            for key in ("lone_worker", "sip", "dispatch", "weather", "sos"):
                _seed.add(FeatureFlag(key=key, enabled=True))
            await _seed.commit()
    except ImportError:
        # Model not added yet — early TDD iterations run without seed.
        pass
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest_asyncio.fixture
async def client():
    """Async HTTP client for testing FastAPI endpoints."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest_asyncio.fixture
async def db_session():
    """Direct async SQLAlchemy session for DB-layer assertions."""
    async with async_session() as session:
        yield session


@pytest_asyncio.fixture
async def auth_headers(client: AsyncClient):
    """Get JWT auth headers by logging in."""
    resp = await client.post("/api/auth/login", json={
        "username": "testadmin",
        "password": "testpass123",
    })
    token = resp.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest_asyncio.fixture
async def admin_client(client: AsyncClient, auth_headers):
    """HTTP client pre-authenticated as admin for protected endpoints."""
    client.headers.update(auth_headers)
    yield client
