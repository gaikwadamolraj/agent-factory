"""Knowledge episodes: one row can now produce several graph episodes (document sections), and rows carry the
subject they describe (e.g. a candidate's resume), so search can be limited to one subject.

Revision ID: 0003_knowledge_sections
Revises: 0002_knowledge_episodes
Create Date: 2026-11-04
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0003_knowledge_sections"
down_revision: Union[str, None] = "0002_knowledge_episodes"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("knowledge_episodes", sa.Column("subject_ref", sa.String(128)))
    op.add_column("knowledge_episodes", sa.Column("graph_episode_uuids", JSONB, nullable=False,
                                                  server_default=sa.text("'[]'::jsonb")))
    op.execute("UPDATE knowledge_episodes SET graph_episode_uuids = jsonb_build_array(graph_episode_uuid) "
               "WHERE graph_episode_uuid IS NOT NULL")
    op.drop_column("knowledge_episodes", "graph_episode_uuid")
    # search limited to subjects + list_episodes by subject
    op.create_index("ix_knowledge_episodes_graph_subject", "knowledge_episodes", ["graph_id", "subject_ref"])


def downgrade() -> None:
    op.drop_index("ix_knowledge_episodes_graph_subject", table_name="knowledge_episodes")
    op.add_column("knowledge_episodes", sa.Column("graph_episode_uuid", sa.String(64)))
    op.execute("UPDATE knowledge_episodes SET graph_episode_uuid = graph_episode_uuids->>0")
    op.drop_column("knowledge_episodes", "graph_episode_uuids")
    op.drop_column("knowledge_episodes", "subject_ref")
