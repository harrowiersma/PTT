from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from server.config import settings

engine = create_async_engine(settings.database_url, echo=False)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def get_db():
    async with async_session() as session:
        yield session


async def init_db():
    # Schema creation and migrations are handled by Alembic in the container
    # entrypoint (server/entrypoint.sh) before uvicorn starts. This remains
    # a no-op hook so existing callers (main.lifespan) keep working.
    return None
