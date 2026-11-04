"""Initial schema: tenants, documents (+ blobs, hybrid-search chunks), chats, screenings, agent memory.

Revision ID: 0001_initial
Revises:
Create Date: 2026-11-04
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _ts(name: str, nullable: bool = False) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable, server_default=None if nullable else sa.func.now())


def _tenant_columns() -> list[sa.Column]:
    return [
        sa.Column("client_id", sa.Integer, sa.ForeignKey("clients.id", ondelete="CASCADE"), nullable=False),
        sa.Column("project_id", sa.Integer, sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
    ]


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")  # resume embeddings (pgvector)

    # ---------------------------------------------------------------- tenancy
    op.create_table(
        "clients",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("slug", sa.String(64), nullable=False, unique=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.true()),
        _ts("created_at"), _ts("updated_at"),
    )
    op.create_table(
        "projects",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("client_id", sa.Integer, sa.ForeignKey("clients.id", ondelete="CASCADE"), nullable=False),
        sa.Column("slug", sa.String(64), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.String(1000)),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.true()),
        _ts("created_at"), _ts("updated_at"),
        sa.UniqueConstraint("client_id", "slug", name="uq_projects_client_slug"),
    )
    op.create_index("ix_projects_client_id", "projects", ["client_id"])
    op.create_table(
        "users",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("client_id", sa.Integer, sa.ForeignKey("clients.id", ondelete="CASCADE"), nullable=False),
        sa.Column("username", sa.String(64), nullable=False, unique=True),
        sa.Column("email", sa.String(255)),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("role", sa.String(32), nullable=False, server_default="recruiter"),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.true()),
        _ts("last_login_at", nullable=True), _ts("created_at"), _ts("updated_at"),
    )
    op.create_index("ix_users_client_id", "users", ["client_id"])
    op.create_table(
        "project_members",
        sa.Column("project_id", sa.Integer, sa.ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        _ts("created_at"),
    )
    op.create_index("ix_project_members_user_id", "project_members", ["user_id"])

    # ---------------------------------------------------------------- documents (resume / JD) + hybrid search
    op.create_table(
        "documents",
        sa.Column("id", sa.Integer, primary_key=True),
        *_tenant_columns(),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),            # resume | jd
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("content_type", sa.String(100), nullable=False),
        sa.Column("size_bytes", sa.Integer, nullable=False, server_default="0"),
        sa.Column("pages", sa.Integer, nullable=False, server_default="0"),
        sa.Column("text", sa.Text, nullable=False),
        sa.Column("text_hash", sa.String(64), nullable=False),
        sa.Column("blob_key", sa.String(128)),                       # original file in the blob store (optional)
        _ts("created_at"),
        sa.UniqueConstraint("client_id", "project_id", "kind", "text_hash", name="uq_documents_tenant_kind_hash"),
    )
    op.create_index("ix_documents_tenant_kind", "documents", ["client_id", "project_id", "kind"])
    op.create_table(
        "document_blobs",
        sa.Column("key", sa.String(128), primary_key=True),
        *_tenant_columns(),
        sa.Column("data", sa.LargeBinary, nullable=False),
        _ts("created_at"),
    )
    op.create_index("ix_document_blobs_tenant", "document_blobs", ["client_id", "project_id"])
    op.create_table(
        "document_chunks",
        sa.Column("id", sa.BigInteger, primary_key=True),
        *_tenant_columns(),
        sa.Column("document_id", sa.Integer, sa.ForeignKey("documents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("chunk_index", sa.Integer, nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("embedding_model", sa.String(128)),
        _ts("created_at"),
    )
    # embedding: pgvector without a fixed dimension (one model per row); tsv: generated full-text vector
    op.execute("ALTER TABLE document_chunks ADD COLUMN embedding vector")
    op.execute("ALTER TABLE document_chunks ADD COLUMN tsv tsvector "
               "GENERATED ALWAYS AS (to_tsvector('english', content)) STORED")
    op.create_index("ix_document_chunks_tenant_doc", "document_chunks", ["client_id", "project_id", "document_id"])
    op.execute("CREATE INDEX ix_document_chunks_tsv ON document_chunks USING GIN (tsv)")

    # ---------------------------------------------------------------- chats
    op.create_table(
        "chats",
        sa.Column("id", sa.String(32), primary_key=True),
        *_tenant_columns(),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("title", sa.String(200), nullable=False, server_default="New chat"),
        sa.Column("jd_text", sa.Text),
        sa.Column("jd_document_id", sa.Integer, sa.ForeignKey("documents.id", ondelete="SET NULL")),
        sa.Column("resume_document_id", sa.Integer, sa.ForeignKey("documents.id", ondelete="SET NULL")),
        sa.Column("last_score", sa.Float),
        _ts("created_at"), _ts("updated_at"),
    )
    op.create_index("ix_chats_tenant_user_updated", "chats", ["client_id", "project_id", "user_id", "updated_at"])
    op.create_table(
        "chat_messages",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("chat_id", sa.String(32), sa.ForeignKey("chats.id", ondelete="CASCADE"), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("data", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        _ts("created_at"),
    )
    op.create_index("ix_chat_messages_chat_id_id", "chat_messages", ["chat_id", "id"])
    op.create_table(
        "screenings",
        sa.Column("id", sa.Integer, primary_key=True),
        *_tenant_columns(),
        sa.Column("chat_id", sa.String(32), sa.ForeignKey("chats.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("resume_document_id", sa.Integer, sa.ForeignKey("documents.id", ondelete="SET NULL")),
        sa.Column("jd_hash", sa.String(64), nullable=False),
        sa.Column("score", sa.Float, nullable=False),
        sa.Column("band", sa.String(32), nullable=False),
        sa.Column("recommendation", sa.String(64), nullable=False),
        sa.Column("skill", sa.String(128), nullable=False),
        sa.Column("result", JSONB, nullable=False),
        _ts("created_at"),
    )
    op.create_index("ix_screenings_tenant_created", "screenings", ["client_id", "project_id", "created_at"])
    op.create_index("ix_screenings_chat_id_id", "screenings", ["chat_id", "id"])

    # ---------------------------------------------------------------- agent memory (Strands session repository)
    op.create_table(
        "agent_sessions",
        sa.Column("session_id", sa.String(128), primary_key=True),
        sa.Column("data", JSONB, nullable=False),
        _ts("created_at"), _ts("updated_at"),
    )
    op.create_table(
        "agent_states",
        sa.Column("session_id", sa.String(128), primary_key=True),
        sa.Column("agent_id", sa.String(128), primary_key=True),
        sa.Column("data", JSONB, nullable=False),
        _ts("updated_at"),
    )
    op.create_table(
        "agent_messages",
        sa.Column("session_id", sa.String(128), primary_key=True),
        sa.Column("agent_id", sa.String(128), primary_key=True),
        sa.Column("message_id", sa.Integer, primary_key=True),
        sa.Column("data", JSONB, nullable=False),
        _ts("created_at"),
    )


def downgrade() -> None:
    for table in ("agent_messages", "agent_states", "agent_sessions", "screenings", "chat_messages", "chats",
                  "document_chunks", "document_blobs", "documents", "project_members", "users", "projects",
                  "clients"):
        op.drop_table(table)
