"""Common database access: one async engine per process, unit-of-work sessions, tenant-scoped repositories.

The schema itself is owned by the `schema` service (Alembic). Models here must match its migrations.
"""
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlmodel.ext.asyncio.session import AsyncSession

from ats_common.config import CommonSettings


class Database:
    """Owns the engine and session factory. Create once at startup, `close()` on shutdown."""

    def __init__(self, settings: CommonSettings):
        self.engine: AsyncEngine = create_async_engine(
            settings.async_database_url,
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
            pool_pre_ping=True,
            connect_args={"server_settings": {"statement_timeout": str(settings.db_statement_timeout_ms),
                                              "application_name": settings.service_name}},
        )
        self._sessions = async_sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """Unit of work: commit on success, roll back on any error (including cancellation)."""
        async with self._sessions() as session:
            try:
                yield session
                await session.commit()
            except BaseException:
                await session.rollback()
                raise

    async def close(self) -> None:
        await self.engine.dispose()


_db: Optional[Database] = None


def init_database(settings: CommonSettings) -> Database:
    global _db
    _db = Database(settings)
    return _db


def get_database() -> Database:
    if _db is None:
        raise RuntimeError("Database is not initialised; call init_database() at startup.")
    return _db


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: one unit of work per request."""
    async with get_database().session() as session:
        yield session
