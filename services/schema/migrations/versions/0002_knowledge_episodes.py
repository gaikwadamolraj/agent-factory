"""Knowledge episode ledger (owned by the knowledge service): ingestion queue, status and erasure trail.

Revision ID: 0002_knowledge_episodes
Revises: 0001_initial
Create Date: 2026-11-04
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0002_knowledge_episodes"
down_revision: Union[str, None] = "0001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "knowledge_episodes",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("client_id", sa.Integer, sa.ForeignKey("clients.id", ondelete="CASCADE"), nullable=False),
        sa.Column("project_id", sa.Integer, sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("namespace", sa.String(32), nullable=False),
        sa.Column("graph_id", sa.String(64), nullable=False),
        sa.Column("source_type", sa.String(32), nullable=False),
        sa.Column("source_ref", sa.String(128), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("content", sa.Text),                       # cleared once the episode is done or failed
        sa.Column("guidance", sa.Text, nullable=False, server_default=""),
        sa.Column("ontology", JSONB),
        sa.Column("reference_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="queued"),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("error_code", sa.String(32)),
        sa.Column("graph_episode_uuid", sa.String(64)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("graph_id", "content_hash", name="uq_knowledge_episodes_graph_hash"),
    )
    # tenant columns first (foreign keys + reporting per tenant)
    op.create_index("ix_knowledge_episodes_tenant_created", "knowledge_episodes",
                    ["client_id", "project_id", "created_at"])
    op.create_index("ix_knowledge_episodes_project_id", "knowledge_episodes", ["project_id"])
    # forget_source / get_episode lookups
    op.create_index("ix_knowledge_episodes_graph_source", "knowledge_episodes", ["graph_id", "source_ref"])
    # worker claim: only rows that still need work are indexed
    op.create_index("ix_knowledge_episodes_due", "knowledge_episodes", ["available_at"],
                    postgresql_where=sa.text("status IN ('queued', 'processing')"))


def downgrade() -> None:
    op.drop_table("knowledge_episodes")
