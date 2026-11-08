"""Strands SessionRepository backed by Postgres (source of truth) with a Redis write-through cache.

Strands calls these methods synchronously while the agent runs. Every write goes to Postgres first, then Redis;
reads try Redis and fall back to Postgres (refilling the cache).
"""
import json
from datetime import datetime, timezone
from typing import Any, Optional

import redis
from sqlalchemy import Engine, create_engine
from sqlalchemy.dialects.postgresql import insert
from sqlmodel import Session as DbSession
from sqlmodel import select
from strands.session.session_repository import SessionRepository
from strands.types.exceptions import SessionException
from strands.types.session import Session, SessionAgent, SessionMessage

from agent_skill.memory.models import AgentMessageRow, AgentSessionRow, AgentStateRow
from agent_common.logging import get_logger, log_event
from agent_common.redis_keys import Keys, build_key

log = get_logger(__name__)


class PostgresRedisSessionRepository(SessionRepository):
    def __init__(self, database_url: str, redis_client: Optional[redis.Redis], ttl_seconds: int, env: str):
        self.engine: Engine = create_engine(database_url, pool_size=5, max_overflow=5, pool_pre_ping=True)
        self.cache = redis_client
        self.ttl = ttl_seconds
        self.env = env

    # ------------------------------------------------------------- redis helpers (never fatal)
    def _k(self, session_id: str, agent_id: str | None, kind: str) -> str:
        """Registered Redis key for a memory record (see agent_common.redis_keys)."""
        key = {"session": Keys.AGENT_MEMORY_SESSION, "state": Keys.AGENT_MEMORY_STATE,
               "messages": Keys.AGENT_MEMORY_MESSAGES}[kind]
        parts = {"session_id": session_id} if agent_id is None else {"session_id": session_id, "agent_id": agent_id}
        return build_key(key, self.env, **parts)

    def _cache_set(self, key: str, value: dict) -> None:
        if self.cache:
            try:
                self.cache.setex(key, self.ttl, json.dumps(value))
            except Exception as e:
                log_event(log, "agent.memory_cache_write_failed", error_type=type(e).__name__)

    def _cache_get(self, key: str) -> Optional[dict]:
        if not self.cache:
            return None
        try:
            raw = self.cache.get(key)
            return json.loads(raw) if raw else None
        except Exception as e:
            log_event(log, "agent.memory_cache_read_failed", error_type=type(e).__name__)
            return None

    def _cache_message(self, session_id: str, agent_id: str, message: dict) -> None:
        if self.cache:
            try:
                key = self._k(session_id, agent_id, "messages")
                if self.cache.exists(key):  # only extend a fully-loaded hash
                    self.cache.hset(key, str(message["message_id"]), json.dumps(message))
                    self.cache.expire(key, self.ttl)
            except Exception as e:
                log_event(log, "agent.memory_cache_write_failed", error_type=type(e).__name__)

    # ------------------------------------------------------------- sessions
    def create_session(self, session: Session, **kwargs: Any) -> Session:
        data = session.to_dict()
        with DbSession(self.engine) as db:
            if db.get(AgentSessionRow, session.session_id):
                raise SessionException(f"Session {session.session_id} already exists")
            db.add(AgentSessionRow(session_id=session.session_id, data=data))
            db.commit()
        self._cache_set(self._k(session.session_id, None, "session"), data)
        return session

    def read_session(self, session_id: str, **kwargs: Any) -> Optional[Session]:
        data = self._cache_get(self._k(session_id, None, "session"))
        if data is None:
            with DbSession(self.engine) as db:
                row = db.get(AgentSessionRow, session_id)
            if row is None:
                return None
            data = row.data
            self._cache_set(self._k(session_id, None, "session"), data)
        return Session.from_dict(data)

    # ------------------------------------------------------------- agents (state)
    def _upsert_agent(self, session_id: str, agent: SessionAgent) -> None:
        data = agent.to_dict()
        stmt = insert(AgentStateRow).values(session_id=session_id, agent_id=agent.agent_id, data=data)
        stmt = stmt.on_conflict_do_update(index_elements=["session_id", "agent_id"],
                                          set_={"data": data, "updated_at": datetime.now(timezone.utc)})
        with DbSession(self.engine) as db:
            db.exec(stmt)
            db.commit()
        self._cache_set(self._k(session_id, agent.agent_id, "state"), data)

    def create_agent(self, session_id: str, session_agent: SessionAgent, **kwargs: Any) -> None:
        self._upsert_agent(session_id, session_agent)

    def update_agent(self, session_id: str, session_agent: SessionAgent, **kwargs: Any) -> None:
        self._upsert_agent(session_id, session_agent)

    def read_agent(self, session_id: str, agent_id: str, **kwargs: Any) -> Optional[SessionAgent]:
        data = self._cache_get(self._k(session_id, agent_id, "state"))
        if data is None:
            with DbSession(self.engine) as db:
                row = db.get(AgentStateRow, (session_id, agent_id))
            if row is None:
                return None
            data = row.data
            self._cache_set(self._k(session_id, agent_id, "state"), data)
        return SessionAgent.from_dict(data)

    # ------------------------------------------------------------- messages
    def _upsert_message(self, session_id: str, agent_id: str, message: SessionMessage) -> None:
        data = message.to_dict()
        stmt = insert(AgentMessageRow).values(session_id=session_id, agent_id=agent_id,
                                              message_id=message.message_id, data=data)
        stmt = stmt.on_conflict_do_update(index_elements=["session_id", "agent_id", "message_id"], set_={"data": data})
        with DbSession(self.engine) as db:
            db.exec(stmt)
            db.commit()
        self._cache_message(session_id, agent_id, data)

    def create_message(self, session_id: str, agent_id: str, session_message: SessionMessage, **kwargs: Any) -> None:
        self._upsert_message(session_id, agent_id, session_message)

    def update_message(self, session_id: str, agent_id: str, session_message: SessionMessage, **kwargs: Any) -> None:
        self._upsert_message(session_id, agent_id, session_message)

    def read_message(self, session_id: str, agent_id: str, message_id: int, **kwargs: Any) -> Optional[SessionMessage]:
        with DbSession(self.engine) as db:
            row = db.get(AgentMessageRow, (session_id, agent_id, message_id))
        return SessionMessage.from_dict(row.data) if row else None

    def _all_messages(self, session_id: str, agent_id: str) -> list[dict]:
        key = self._k(session_id, agent_id, "messages")
        if self.cache:
            try:
                cached = self.cache.hgetall(key)
                if cached:
                    return [json.loads(v) for _, v in sorted(cached.items(), key=lambda kv: int(kv[0]))]
            except Exception as e:
                log_event(log, "agent.memory_cache_read_failed", error_type=type(e).__name__)
        with DbSession(self.engine) as db:
            rows = db.exec(select(AgentMessageRow)
                           .where(AgentMessageRow.session_id == session_id, AgentMessageRow.agent_id == agent_id)
                           .order_by(AgentMessageRow.message_id)).all()
        messages = [r.data for r in rows]
        if self.cache and messages:
            try:
                self.cache.hset(key, mapping={str(m["message_id"]): json.dumps(m) for m in messages})
                self.cache.expire(key, self.ttl)
            except Exception as e:
                log_event(log, "agent.memory_cache_write_failed", error_type=type(e).__name__)
        return messages

    def list_messages(
        self, session_id: str, agent_id: str, limit: int | None = None, offset: int = 0, **kwargs: Any
    ) -> list[SessionMessage]:
        messages = self._all_messages(session_id, agent_id)
        window = messages[offset: offset + limit] if limit is not None else messages[offset:]
        return [SessionMessage.from_dict(m) for m in window]
