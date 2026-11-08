"""Subjects: the one record a domain journey is about (candidate, claim, customer, brief).

One table for every domain rather than a table per vertical (docs/impl_multi_domain.md section 3): the variable
part of a record lives in `payload` JSONB and only the columns we filter, join or sort on are relational. The
natural key (client, project, domain, subject_type, external_ref) is what the knowledge graph stores as its
`subject_ref`, so a record and its extracted facts stay joined without the graph knowing this table exists.

Conventions follow 0001: BIGSERIAL `id` for joins, `external_id` UUID on the wire, audit + soft delete, RLS on
`client_id` from the `app.client_id` GUC. Repeated here rather than imported because a migration must keep
working even when the helpers in 0001 change.

Revision ID: 0002_subjects
Revises: 0001_initial
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUuid

revision = "0002_subjects"
down_revision = "0001_initial"
branch_labels = None
depends_on = None

EMPTY_JSON_OBJECT = sa.text("'{}'::jsonb")


def timestamp(name: str, nullable: bool = False) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable,
                     server_default=None if nullable else sa.func.now())


def external_id_column() -> sa.Column:
    return sa.Column("external_id", PgUuid(as_uuid=True), nullable=False,
                     server_default=sa.text("gen_random_uuid()"), unique=True)


def audit_columns() -> list[sa.Column]:
    return [timestamp("created_at"), sa.Column("created_by", sa.BigInteger),
            timestamp("updated_at"), sa.Column("updated_by", sa.BigInteger),
            sa.Column("is_deleted", sa.Boolean, nullable=False, server_default=sa.false()),
            timestamp("deleted_at", nullable=True), sa.Column("deleted_by", sa.BigInteger),
            sa.Column("deleted_reason", sa.String(500))]


def enable_tenant_rls(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"CREATE POLICY {table}_tenant_isolation ON {table} "
               f"USING (client_id = current_setting('app.client_id', true)::integer) "
               f"WITH CHECK (client_id = current_setting('app.client_id', true)::integer)")


def upgrade() -> None:
    op.create_table(
        "subjects",
        sa.Column("id", sa.BigInteger, primary_key=True),
        external_id_column(),
        sa.Column("client_id", sa.Integer, sa.ForeignKey("clients.id", ondelete="CASCADE"), nullable=False),
        sa.Column("project_id", sa.Integer, sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("domain", sa.String(64), nullable=False),          # pack id: ats | insurance | banking | media
        sa.Column("subject_type", sa.String(64), nullable=False),    # candidate | claim | customer | brief
        sa.Column("external_ref", sa.String(128), nullable=False),   # the caller's id for the record
        sa.Column("display_name", sa.String(255), nullable=False, server_default=""),
        sa.Column("status", sa.String(32), nullable=False, server_default="active"),
        sa.Column("schema_version", sa.String(16), nullable=False, server_default="v1"),
        sa.Column("classification", sa.String(32), nullable=False, server_default="confidential"),
        sa.Column("payload", JSONB, nullable=False, server_default=EMPTY_JSON_OBJECT),
        sa.Column("search_metadata", JSONB, nullable=False, server_default=EMPTY_JSON_OBJECT),
        *audit_columns(),
        sa.UniqueConstraint("client_id", "project_id", "domain", "subject_type", "external_ref",
                            name="uq_subjects_tenant_ref"),
    )
    op.create_index("ix_subjects_client_id", "subjects", ["client_id"])
    op.create_index("ix_subjects_project_id", "subjects", ["project_id"])
    # List/count one subject type in a project, newest first (the bulk-match and dashboard query).
    op.create_index("ix_subjects_tenant_type", "subjects",
                    ["client_id", "project_id", "domain", "subject_type", "status"])
    # Filter inside the pack-shaped record without a column per domain.
    op.create_index("ix_subjects_payload", "subjects", ["payload"], postgresql_using="gin",
                    postgresql_ops={"payload": "jsonb_path_ops"})

    op.create_table(
        "subject_versions",
        sa.Column("id", sa.BigInteger, primary_key=True),
        external_id_column(),
        sa.Column("client_id", sa.Integer, sa.ForeignKey("clients.id", ondelete="CASCADE"), nullable=False),
        sa.Column("subject_id", sa.BigInteger, sa.ForeignKey("subjects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("version_number", sa.Integer, nullable=False),
        sa.Column("schema_version", sa.String(16), nullable=False, server_default="v1"),
        sa.Column("payload_snapshot", JSONB, nullable=False, server_default=EMPTY_JSON_OBJECT),
        sa.Column("change_reason", sa.String(500)),
        *audit_columns(),
        sa.UniqueConstraint("subject_id", "version_number", name="uq_subject_versions_number"),
    )
    op.create_index("ix_subject_versions_client_id", "subject_versions", ["client_id"])
    # Newest snapshots of one subject (reproducing a decision against what was known then).
    op.create_index("ix_subject_versions_subject", "subject_versions",
                    ["client_id", "subject_id", "version_number"])

    op.create_table(
        "subject_relations",
        sa.Column("id", sa.BigInteger, primary_key=True),
        external_id_column(),
        sa.Column("client_id", sa.Integer, sa.ForeignKey("clients.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source_subject_id", sa.BigInteger, sa.ForeignKey("subjects.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("target_subject_id", sa.BigInteger, sa.ForeignKey("subjects.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("relation_type", sa.String(64), nullable=False),
        sa.Column("attributes", JSONB, nullable=False, server_default=EMPTY_JSON_OBJECT),
        *audit_columns(),
        sa.UniqueConstraint("client_id", "source_subject_id", "target_subject_id", "relation_type",
                            name="uq_subject_relations_edge"),
        # A subject can never relate to itself; enforced by the database, not only by the application.
        sa.CheckConstraint("source_subject_id <> target_subject_id", name="ck_subject_relations_not_self"),
    )
    op.create_index("ix_subject_relations_client_id", "subject_relations", ["client_id"])
    op.create_index("ix_subject_relations_source", "subject_relations",
                    ["client_id", "source_subject_id", "relation_type"])
    op.create_index("ix_subject_relations_target", "subject_relations",
                    ["client_id", "target_subject_id", "relation_type"])

    for table in ("subjects", "subject_versions", "subject_relations"):
        enable_tenant_rls(table)


def downgrade() -> None:
    op.drop_table("subject_relations")
    op.drop_table("subject_versions")
    op.drop_table("subjects")
