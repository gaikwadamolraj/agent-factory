"""Test environment of the auth-service: no Postgres, no Redis, no real keys.

* database: in-memory SQLite with the four tenancy tables, seeded with one client, two projects and three users
* Redis:    FakeRedis (the handful of commands the service uses)
* keys:     a fresh EC P-256 key per test; one service client (ats-orchestrator) with a known secret
"""
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
import httpx
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from agent_common.authz import database_snapshot_loader
from agent_common.db import get_session
from agent_common.db.tables import Client, Project, ProjectMember, User
from agent_common.testing import FakeRedis
from auth_service import passwords
from auth_service.app import create_app
from auth_service.config import AuthSettings
from auth_service.container import build_container
from auth_service.signing import KeyRing
from auth_service.tokens import TokenIssuer

PASSWORD = "Demo@12345"
ORCHESTRATOR_SECRET = "o" * 48
TEST_ISSUER = "http://auth-service.test"



def pem_of_new_key() -> str:
    """PEM (PKCS8) of a fresh EC P-256 private key, like `openssl genpkey -algorithm EC ...` writes."""
    key = ec.generate_private_key(ec.SECP256R1())
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption()).decode("ascii")


@pytest.fixture(autouse=True)
def fast_bcrypt(monkeypatch):
    """Cheap hashes in tests (production uses 12 rounds)."""
    monkeypatch.setattr(passwords, "BCRYPT_ROUNDS", 4)


@pytest.fixture
def settings(monkeypatch) -> AuthSettings:
    monkeypatch.setenv("CLIENT_SECRET_ATS_ORCHESTRATOR", ORCHESTRATOR_SECRET)
    return AuthSettings(auth_signing_key=pem_of_new_key(), auth_issuer=TEST_ISSUER,
                        oauth_client_audiences="ats-orchestrator=agent-skill|agent-compute",
                        cookie_secure=False, login_max_failures=3, client_auth_max_failures=3,
                        otel_enabled=False, log_json=False)


async def _seed(sessions) -> None:
    async with sessions() as session:
        session.add(Client(id=1, slug="acme", name="Acme"))
        session.add(Client(id=2, slug="globex", name="Globex"))
        session.add_all([Project(id=1, client_id=1, slug="eng", name="Engineering"),
                         Project(id=2, client_id=1, slug="ops", name="Operations")])
        session.add_all([
            User(id=1, client_id=1, username="acme.admin", password_hash=passwords.hash_password(PASSWORD),
                 role="admin"),
            User(id=2, client_id=1, username="acme.recruiter", password_hash=passwords.hash_password(PASSWORD),
                 role="recruiter"),
            User(id=3, client_id=1, username="acme.disabled", password_hash=passwords.hash_password(PASSWORD),
                 role="recruiter", is_active=False),
            User(id=4, client_id=2, username="globex.admin", password_hash=passwords.hash_password(PASSWORD),
                 role="admin"),
        ])
        session.add_all([ProjectMember(project_id=1, user_id=1), ProjectMember(project_id=2, user_id=1),
                         ProjectMember(project_id=1, user_id=2)])
        await session.commit()


class SqliteDatabase:
    """The `session()` unit of work of agent_common.db.Database, on the test's SQLite session factory."""

    def __init__(self, sessions):
        self._sessions = sessions

    @asynccontextmanager
    async def session(self):
        async with self._sessions() as session:
            yield session
            await session.commit()


@pytest.fixture
async def database():
    """In-memory SQLite shared by all sessions of one test (StaticPool = one connection)."""
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool,
                                 connect_args={"check_same_thread": False})
    async with engine.begin() as connection:
        await connection.run_sync(SQLModel.metadata.create_all, tables=[
            Client.__table__, Project.__table__, User.__table__, ProjectMember.__table__])
    sessions = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await _seed(sessions)
    yield sessions
    await engine.dispose()


@pytest.fixture
async def service(settings, database):
    """The app on fake Redis + SQLite: client (httpx.AsyncClient, same event loop as SQLite), container, redis,
    settings, tokens."""
    tokens = TokenIssuer.from_settings(settings, KeyRing.from_settings(settings))
    app = create_app(settings, tokens)
    redis, db = FakeRedis(), SqliteDatabase(database)
    container = build_container(settings, redis, tokens, database_snapshot_loader(db))
    app.state.container = container          # no lifespan in tests: no real Redis or Postgres

    async def session_override():
        async with db.session() as session:
            yield session

    app.dependency_overrides[get_session] = session_override
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        yield SimpleNamespace(client=client, container=container, redis=redis, settings=settings, tokens=tokens)
