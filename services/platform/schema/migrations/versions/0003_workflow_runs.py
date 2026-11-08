"""workflow_runs: the application's projection of a durable run.

Temporal owns the event history and replay state. This table is what the product needs without asking the
engine: list a project's runs, show progress, correlate a chat message with the work it started, bill for it
and audit it (docs/impl_temporal.md section 3). Step detail stays in Temporal; step outputs become artifacts.

Revision ID: 0003_workflow_runs
Revises: 0002_subjects
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUuid

revision = "0003_workflow_runs"
down_revision = "0002_subjects"
branch_labels = None
depends_on = None

EMPTY_JSON_OBJECT = sa.text("'{}'::jsonb")


def timestamp(name: str, nullable: bool = False) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable,
                     server_default=None if nullable else sa.func.now())


def audit_columns() -> list[sa.Column]:
    return [timestamp("created_at"), sa.Column("created_by", sa.BigInteger),
            timestamp("updated_at"), sa.Column("updated_by", sa.BigInteger),
            sa.Column("is_deleted", sa.Boolean, nullable=False, server_default=sa.false()),
            timestamp("deleted_at", nullable=True), sa.Column("deleted_by", sa.BigInteger),
            sa.Column("deleted_reason", sa.String(500))]


def upgrade() -> None:
    op.create_table(
        "workflow_runs",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("external_id", PgUuid(as_uuid=True), nullable=False,
                  server_default=sa.text("gen_random_uuid()"), unique=True),
        sa.Column("client_id", sa.Integer, sa.ForeignKey("clients.id", ondelete="CASCADE"), nullable=False),
        sa.Column("project_id", sa.Integer, sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("domain", sa.String(64), nullable=False),
        sa.Column("journey", sa.String(64), nullable=False),
        # The run may be about a subject (onboarding, matching) or about none yet (a plan being built).
        sa.Column("subject_id", sa.BigInteger, sa.ForeignKey("subjects.id", ondelete="SET NULL")),
        sa.Column("workflow_type", sa.String(128), nullable=False),
        sa.Column("task_queue", sa.String(128), nullable=False),
        sa.Column("temporal_workflow_id", sa.String(255), nullable=False),
        sa.Column("temporal_run_id", sa.String(64), nullable=False, server_default=""),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("priority_tier", sa.String(16), nullable=False, server_default="standard"),
        sa.Column("status", sa.String(24), nullable=False, server_default="queued"),
        sa.Column("input_ref", JSONB, nullable=False, server_default=EMPTY_JSON_OBJECT),
        sa.Column("result_summary", JSONB, nullable=False, server_default=EMPTY_JSON_OBJECT),
        sa.Column("error_code", sa.String(32)),
        sa.Column("error_summary", sa.String(500)),
        timestamp("started_at", nullable=True),
        timestamp("completed_at", nullable=True),
        *audit_columns(),
        # One row per Temporal run; a retry that produces a new run id is a new row.
        sa.UniqueConstraint("temporal_workflow_id", "temporal_run_id", name="uq_workflow_runs_temporal"),
        # What makes a re-submitted request reattach instead of duplicating work.
        sa.UniqueConstraint("client_id", "idempotency_key", name="uq_workflow_runs_idempotency"),
    )
    op.create_index("ix_workflow_runs_client_id", "workflow_runs", ["client_id"])
    op.create_index("ix_workflow_runs_project_id", "workflow_runs", ["project_id"])
    op.create_index("ix_workflow_runs_subject_id", "workflow_runs", ["subject_id"])
    # The dashboard query: a project's runs of one journey, newest first.
    op.create_index("ix_workflow_runs_tenant_journey", "workflow_runs",
                    ["client_id", "project_id", "domain", "journey", "status"])

    op.execute("ALTER TABLE workflow_runs ENABLE ROW LEVEL SECURITY")
    op.execute("CREATE POLICY workflow_runs_tenant_isolation ON workflow_runs "
               "USING (client_id = current_setting('app.client_id', true)::integer) "
               "WITH CHECK (client_id = current_setting('app.client_id', true)::integer)")


def downgrade() -> None:
    op.drop_table("workflow_runs")
