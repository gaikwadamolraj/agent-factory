"""Common database access: one async engine per process, unit-of-work sessions, tenant-scoped repositories.

The schema itself is owned by the `schema` service (Alembic). Models here must match its migrations.
"""
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlmodel.ext.asyncio.session import AsyncSession

from agent_common.config import CommonSettings

# Session GUC that the new RLS-protected platform tables isolate on (see services/platform/schema 0001 migration).
# It must be set from a *verified* client id (JWT/tenant claim), never from a request body.
TENANT_GUC = "app.client_id"


class Database:
    """Owns the engine and session factory. Create once at startup, `close()` on shutdown."""

    def __init__(self, settings: CommonSettings):
        connect_args: dict = {"server_settings": {"statement_timeout": str(settings.db_statement_timeout_ms),
                                                  "application_name": settings.service_name}}
        # Behind PgBouncer in transaction-pooling mode, asyncpg's prepared statements break (a statement prepared on
        # one server connection is reused on another). Disabling the statement cache makes queries pgbouncer-safe.
        if settings.db_pgbouncer:
            connect_args["statement_cache_size"] = 0
        self.engine: AsyncEngine = create_async_engine(
            settings.async_database_url,
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
            pool_pre_ping=True,
            connect_args=connect_args,
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

    @asynccontextmanager
    async def tenant_session(self, client_id: int) -> AsyncIterator[AsyncSession]:
        """A unit of work with the tenant GUC set, so Postgres RLS on the new platform tables isolates to this client.

        Use this (instead of `session()`) for work on RLS-protected tables; `client_id` must come from verified claims.
        Setting the GUC is additive: existing non-RLS tables and queries are unaffected whether or not it is set."""
        async with self.session() as session:
            await set_tenant_context(session, client_id)
            yield session

    async def close(self) -> None:
        await self.engine.dispose()


async def set_tenant_context(session: AsyncSession, client_id: int) -> None:
    """Set the `app.client_id` GUC for the current transaction (SET LOCAL semantics via set_config(..., true)).

    The RLS policies on the new platform tables use this value, so a tenant-scoped session only ever sees its own
    client's rows. The value is transaction-local: it is reset automatically on commit or rollback, so one pooled
    connection never leaks a tenant context into the next unit of work."""
    await session.exec(text("SELECT set_config(:guc, :value, true)")
                       .bindparams(guc=TENANT_GUC, value=str(int(client_id))))


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


# Re-export the shared tables and repositories so callers import them from one place (agent_common.db).
from agent_common.db.platform_repositories import (AccessPolicyRepository, GroupMemberRepository,  # noqa: E402
                                                 GroupRepository, NotificationRepository,
                                                 SkillApprovalRepository, SkillRepository,
                                                 SkillVersionFileRepository, SkillVersionRepository,
                                                 MatchResultRepository, SubjectRelationRepository, SubjectRepository,
                                                 SubjectVersionRepository, UserInviteRepository,
                                                 WorkflowRunRepository)
from agent_common.db.platform_tables import (AccessPolicy, AuditMixin, Group, GroupMember,  # noqa: E402
                                           Notification, Skill, SkillApproval, SkillVersion,
                                           MatchResult, SkillVersionFile, Subject, SubjectRelation, SubjectVersion,
                                           UserInvite, WorkflowRun)
from agent_common.db.repositories import (BaseRepository, MembershipRepository, TenantRepository,  # noqa: E402
                                        TenantScope)
from agent_common.db.tables import Client, Project, ProjectMember, User  # noqa: E402
