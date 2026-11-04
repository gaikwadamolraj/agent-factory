"""Tenant tables shared by every service: clients -> projects, users (belong to one client), project members."""
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import DateTime, UniqueConstraint, func
from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def created_at_field() -> Any:
    return Field(default_factory=utcnow, sa_type=DateTime(timezone=True), sa_column_kwargs={"server_default": func.now()})


def updated_at_field() -> Any:
    return Field(default_factory=utcnow, sa_type=DateTime(timezone=True),
                 sa_column_kwargs={"server_default": func.now(), "onupdate": utcnow})


class Client(SQLModel, table=True):
    """A tenant (customer organisation)."""
    __tablename__ = "clients"

    id: Optional[int] = Field(default=None, primary_key=True)
    slug: str = Field(max_length=64, unique=True, index=True)
    name: str = Field(max_length=200)
    is_active: bool = Field(default=True)
    created_at: datetime = created_at_field()
    updated_at: datetime = updated_at_field()


class Project(SQLModel, table=True):
    """A hiring project inside a client; every chat, resume and screening belongs to one."""
    __tablename__ = "projects"
    __table_args__ = (UniqueConstraint("client_id", "slug", name="uq_projects_client_slug"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    client_id: int = Field(foreign_key="clients.id", ondelete="CASCADE", index=True)
    slug: str = Field(max_length=64)
    name: str = Field(max_length=200)
    description: Optional[str] = Field(default=None, max_length=1000)
    is_active: bool = Field(default=True)
    created_at: datetime = created_at_field()
    updated_at: datetime = updated_at_field()


class User(SQLModel, table=True):
    __tablename__ = "users"

    id: Optional[int] = Field(default=None, primary_key=True)
    client_id: int = Field(foreign_key="clients.id", ondelete="CASCADE", index=True)
    username: str = Field(max_length=64, unique=True, index=True)
    email: Optional[str] = Field(default=None, max_length=255)
    password_hash: str = Field(max_length=255)
    role: str = Field(default="recruiter", max_length=32)
    is_active: bool = Field(default=True)
    last_login_at: Optional[datetime] = Field(default=None, sa_type=DateTime(timezone=True))
    created_at: datetime = created_at_field()
    updated_at: datetime = updated_at_field()


class ProjectMember(SQLModel, table=True):
    """Which users may work in which project."""
    __tablename__ = "project_members"

    project_id: int = Field(foreign_key="projects.id", ondelete="CASCADE", primary_key=True)
    user_id: int = Field(foreign_key="users.id", ondelete="CASCADE", primary_key=True, index=True)
    created_at: datetime = created_at_field()
