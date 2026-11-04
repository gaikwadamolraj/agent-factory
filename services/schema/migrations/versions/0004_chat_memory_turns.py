"""Chat memory (running summary of what the recruiter asked and decided) and one JSON record per chat turn.

Revision ID: 0004_chat_memory_turns
Revises: 0003_knowledge_sections
Create Date: 2026-11-04
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0004_chat_memory_turns"
down_revision: Union[str, None] = "0003_knowledge_sections"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "chat_memory",
        sa.Column("chat_id", sa.String(32), sa.ForeignKey("chats.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("client_id", sa.Integer, sa.ForeignKey("clients.id", ondelete="CASCADE"), nullable=False),
        sa.Column("project_id", sa.Integer, sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("summary", sa.Text, nullable=False, server_default=""),
        sa.Column("notes", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("last_message_id", sa.Integer, nullable=False, server_default="0"),
        sa.Column("version", sa.String(32), nullable=False, server_default=""),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_chat_memory_tenant", "chat_memory", ["client_id", "project_id"])
    op.create_index("ix_chat_memory_project_id", "chat_memory", ["project_id"])

    op.create_table(
        "chat_turns",
        sa.Column("id", sa.String(32), primary_key=True),                 # the turn id streamed to the browser
        sa.Column("client_id", sa.Integer, sa.ForeignKey("clients.id", ondelete="CASCADE"), nullable=False),
        sa.Column("project_id", sa.Integer, sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("chat_id", sa.String(32), sa.ForeignKey("chats.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("journey", sa.String(32), nullable=False),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("error_code", sa.String(32)),
        sa.Column("duration_ms", sa.Integer, nullable=False),
        sa.Column("record", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_chat_turns_tenant_created", "chat_turns", ["client_id", "project_id", "created_at"])
    op.create_index("ix_chat_turns_project_id", "chat_turns", ["project_id"])
    op.create_index("ix_chat_turns_chat", "chat_turns", ["chat_id", "created_at"])
    op.create_index("ix_chat_turns_user_id", "chat_turns", ["user_id"])


def downgrade() -> None:
    op.drop_table("chat_turns")
    op.drop_table("chat_memory")
