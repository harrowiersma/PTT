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

from server.database import Base, engine, get_db
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
async def auth_headers(client: AsyncClient):
    """Get JWT auth headers by logging in."""
    resp = await client.post("/api/auth/login", json={
        "username": "testadmin",
        "password": "testpass123",
    })
    token = resp.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}
