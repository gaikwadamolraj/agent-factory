"""Agent memory tables (owned by the agent service; migrated with its own Alembic version table)."""
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import Column, DateTime, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _created() -> Any:
    return Field(default_factory=_now, sa_type=DateTime(timezone=True), sa_column_kwargs={"server_default": func.now()})


class AgentSessionRow(SQLModel, table=True):
    """One Strands session per user: session_id = 'user-<id>'."""
    __tablename__ = "agent_sessions"

    session_id: str = Field(sa_column=Column(String(128), primary_key=True))
    data: dict = Field(sa_column=Column(JSONB, nullable=False))
    created_at: datetime = _created()
    updated_at: datetime = _created()


class AgentStateRow(SQLModel, table=True):
    """Agent state (workspace, screening history) and conversation-manager state."""
    __tablename__ = "agent_states"

    session_id: str = Field(sa_column=Column(String(128), primary_key=True))
    agent_id: str = Field(sa_column=Column(String(128), primary_key=True))
    data: dict = Field(sa_column=Column(JSONB, nullable=False))
    updated_at: datetime = _created()


class AgentMessageRow(SQLModel, table=True):
    """Conversation messages of the agent, in order."""
    __tablename__ = "agent_messages"

    session_id: str = Field(sa_column=Column(String(128), primary_key=True))
    agent_id: str = Field(sa_column=Column(String(128), primary_key=True))
    message_id: int = Field(sa_column=Column(Integer, primary_key=True))
    data: dict = Field(sa_column=Column(JSONB, nullable=False))
    created_at: datetime = _created()
