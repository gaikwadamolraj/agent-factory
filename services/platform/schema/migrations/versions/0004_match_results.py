"""match_results: one row per subject graded in a match run (J2 single, J3 bulk).

A bulk run grades thousands of subjects against one criteria set. Those results cannot live in the run's
`result_summary` - it is a small JSON column, and workflow history is not a database - and they must outlive
the engine, because "why was this candidate ranked 400th" is a question asked months later, often by someone
with a legal interest in the answer (docs/impl_multi_domain.md sections 2.2 and 4).

So each graded subject gets a row: the score the rules engine computed, the band and recommendation, the
per-criterion grades, and the rubric that produced them. The run's `result_summary` then holds only the
shortlist and the counts.

`(client_id, run_id, subject_id)` is unique: a Temporal activity that retries after a crash updates the same
row rather than grading the subject twice, which is what makes a bulk run safe to resume.

Revision ID: 0004_match_results
Revises: 0003_workflow_runs
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUuid

revision = "0004_match_results"
down_revision = "0003_workflow_runs"
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
        "match_results",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("external_id", PgUuid(as_uuid=True), nullable=False,
                  server_default=sa.text("gen_random_uuid()"), unique=True),
        sa.Column("client_id", sa.Integer, sa.ForeignKey("clients.id", ondelete="CASCADE"), nullable=False),
        sa.Column("project_id", sa.Integer, sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("run_id", sa.BigInteger, sa.ForeignKey("workflow_runs.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("subject_id", sa.BigInteger, sa.ForeignKey("subjects.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("domain", sa.String(64), nullable=False),
        # Which criteria set the subject was graded against, and under which rubric. Both are pinned per run
        # so a re-score months later can be compared with the original rather than quietly differing.
        sa.Column("criteria_key", sa.String(64), nullable=False, server_default=""),
        sa.Column("criteria_ref", sa.String(128), nullable=False, server_default=""),
        sa.Column("rubric_key", sa.String(64), nullable=False, server_default=""),
        sa.Column("rubric_version", sa.String(32), nullable=False, server_default=""),
        sa.Column("score", sa.Numeric(6, 2)),
        sa.Column("band", sa.String(64), nullable=False, server_default=""),
        sa.Column("recommendation", sa.String(128), nullable=False, server_default=""),
        sa.Column("rank", sa.Integer),                 # position in this run, written once the run ranks
        # Per-criterion grades and the component points: enough to explain the score without re-running it.
        sa.Column("grades", JSONB, nullable=False, server_default=EMPTY_JSON_OBJECT),
        sa.Column("breakdown", JSONB, nullable=False, server_default=EMPTY_JSON_OBJECT),
        # A subject that could not be graded is recorded, not dropped: a bulk run that silently skipped 200
        # candidates would look exactly like one that found them unsuitable.
        sa.Column("error_code", sa.String(32)),
        sa.Column("error_summary", sa.String(500)),
        *audit_columns(),
        sa.UniqueConstraint("client_id", "run_id", "subject_id", name="uq_match_results_run_subject"),
    )
    op.create_index("ix_match_results_client_id", "match_results", ["client_id"])
    op.create_index("ix_match_results_subject_id", "match_results", ["subject_id"])
    # The shortlist query: this run's results, best first. Tenant columns lead, as every composite index here
    # does, so the index is usable under the tenant filter rather than only after it.
    op.create_index("ix_match_results_run_rank", "match_results",
                    ["client_id", "run_id", sa.text("score DESC NULLS LAST")])
    # "Every run that graded this subject" - the provenance question J4 answers.
    op.create_index("ix_match_results_tenant_subject", "match_results",
                    ["client_id", "project_id", "subject_id"])

    op.execute("ALTER TABLE match_results ENABLE ROW LEVEL SECURITY")
    op.execute("CREATE POLICY match_results_tenant_isolation ON match_results "
               "USING (client_id = current_setting('app.client_id', true)::integer) "
               "WITH CHECK (client_id = current_setting('app.client_id', true)::integer)")


def downgrade() -> None:
    op.drop_table("match_results")
