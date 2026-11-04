"""Orchestrator-owned tables (schema: services/schema migration 0001). Every row carries client_id + project_id."""
import uuid
from datetime import datetime
from enum import StrEnum
from typing import Optional

from sqlalchemy import Column, LargeBinary, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

import ats_common.db.tables  # noqa: F401  (registers clients/projects/users for the foreign keys)
from ats_common.db.tables import created_at_field, updated_at_field


def new_chat_id() -> str:
    return uuid.uuid4().hex


class DocumentKind(StrEnum):
    RESUME = "resume"
    JD = "jd"


class Document(SQLModel, table=True):
    """An uploaded resume or job description: extracted text (+ original file in the blob store)."""
    __tablename__ = "documents"

    id: Optional[int] = Field(default=None, primary_key=True)
    client_id: int = Field(foreign_key="clients.id")
    project_id: int = Field(foreign_key="projects.id")
    user_id: int = Field(foreign_key="users.id")
    kind: str = Field(max_length=16)
    filename: str = Field(max_length=255)
    content_type: str = Field(max_length=100)
    size_bytes: int = 0
    pages: int = 0
    text: str = Field(sa_column=Column(Text, nullable=False))
    text_hash: str = Field(max_length=64)
    blob_key: Optional[str] = Field(default=None, max_length=128)
    created_at: datetime = created_at_field()


class DocumentBlob(SQLModel, table=True):
    """Original file bytes (Postgres backend of the BlobStore interface)."""
    __tablename__ = "document_blobs"

    key: str = Field(primary_key=True, max_length=128)
    client_id: int = Field(foreign_key="clients.id")
    project_id: int = Field(foreign_key="projects.id")
    data: bytes = Field(sa_column=Column(LargeBinary, nullable=False))
    created_at: datetime = created_at_field()


class Chat(SQLModel, table=True):
    """A conversation; holds the job description and resume the assistant works with."""
    __tablename__ = "chats"

    id: str = Field(default_factory=new_chat_id, primary_key=True, max_length=32)
    client_id: int = Field(foreign_key="clients.id")
    project_id: int = Field(foreign_key="projects.id")
    user_id: int = Field(foreign_key="users.id")
    title: str = Field(default="New chat", max_length=200)
    jd_text: Optional[str] = Field(default=None, sa_column=Column(Text))
    jd_document_id: Optional[int] = Field(default=None, foreign_key="documents.id")
    resume_document_id: Optional[int] = Field(default=None, foreign_key="documents.id")
    last_score: Optional[float] = None
    created_at: datetime = created_at_field()
    updated_at: datetime = updated_at_field()


class ChatMessage(SQLModel, table=True):
    __tablename__ = "chat_messages"

    id: Optional[int] = Field(default=None, primary_key=True)
    chat_id: str = Field(foreign_key="chats.id", max_length=32)
    role: str = Field(max_length=16)
    content: str = Field(sa_column=Column(Text, nullable=False))
    data: dict = Field(default_factory=dict, sa_column=Column(JSONB, nullable=False, server_default="{}"))
    created_at: datetime = created_at_field()


class Screening(SQLModel, table=True):
    __tablename__ = "screenings"

    id: Optional[int] = Field(default=None, primary_key=True)
    client_id: int = Field(foreign_key="clients.id")
    project_id: int = Field(foreign_key="projects.id")
    chat_id: str = Field(foreign_key="chats.id", max_length=32)
    user_id: int = Field(foreign_key="users.id")
    resume_document_id: Optional[int] = Field(default=None, foreign_key="documents.id")
    jd_hash: str = Field(max_length=64)
    score: float
    band: str = Field(max_length=32)
    recommendation: str = Field(max_length=64)
    skill: str = Field(max_length=128)
    result: dict = Field(sa_column=Column(JSONB, nullable=False))
    created_at: datetime = created_at_field()
