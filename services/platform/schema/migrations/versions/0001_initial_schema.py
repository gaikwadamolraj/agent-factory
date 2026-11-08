"""Initial schema (fresh start, 2026-11-05): replaces the earlier 0001-0004 migrations.

Sections, by owner:
* tenancy          clients, projects, users, project_members            (agent_common.db)
* documents        documents, document_blobs, document_chunks            (ats-orchestrator)
* chats            chats, chat_messages, chat_memory, chat_turns, screenings (ats-orchestrator)
* agent memory     agent_sessions, agent_states, agent_messages          (agent-skill, Strands sessions)
* knowledge graph  knowledge_episodes                                    (agent-knowledge-graph)
* platform admin   skills, skill_versions, skill_version_files, skill_approvals, notifications, groups,
                   group_members, access_policies, user_invites          (agent_common.db, agent-factory)

Rules: tenant-owned tables carry client_id + project_id with a composite index starting with them; every foreign
key has an index; the services' SQLModel models must match these tables.

Platform-admin tables (the last section) are NEW, so they adopt the platform-wide conventions
(docs/impl_agent_factory.md section 5): a BIGSERIAL `id` for joins (never exposed), an `external_id` UUID for the API,
an audit mixin (created/updated/deleted actor + time, soft delete), and Postgres Row-Level Security.

Tenancy decision for RLS (reconciling the design's `tenant_id` with the existing schema): we have clients -> projects
and no separate tenant entity, so these tables isolate on **client_id** - the simplest key that fits the current
hierarchy - and the RLS predicate reads it from the `app.client_id` session GUC, which a tenant-scoped session sets
from verified claims (agent_common.db.set_tenant_context / Database.tenant_session). RLS is ENABLED with a tenant
policy, but NOT forced (no `FORCE ROW LEVEL SECURITY`): the table owner the services connect as bypasses RLS today,
so enabling it is additive and breaks no existing query. TODO before relying on RLS for isolation: run the apps under
a dedicated non-owner role and add FORCE ROW LEVEL SECURITY; until then isolation is enforced in code by the
client-scoped repositories, and the GUC + policies are in place for the switch.

Revision ID: 0001_initial
Revises:
Create Date: 2026-11-05
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUuid

revision: str = "0001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

EMPTY_JSON_OBJECT = sa.text("'{}'::jsonb")
EMPTY_JSON_ARRAY = sa.text("'[]'::jsonb")


def timestamp(name: str, nullable: bool = False) -> sa.Column:
    """A timezone-aware timestamp column; NOT NULL ones default to now()."""
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable,
                     server_default=None if nullable else sa.func.now())


def tenant_columns() -> list[sa.Column]:
    """client_id + project_id of a tenant-owned table (rows are deleted with their client/project)."""
    return [
        sa.Column("client_id", sa.Integer, sa.ForeignKey("clients.id", ondelete="CASCADE"), nullable=False),
        sa.Column("project_id", sa.Integer, sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
    ]


def foreign_key(column: str, target: str, ondelete: str = "CASCADE", nullable: bool = False) -> sa.Column:
    """An integer foreign key column to `target` (e.g. "users.id")."""
    return sa.Column(column, sa.Integer, sa.ForeignKey(target, ondelete=ondelete), nullable=nullable)


def big_foreign_key(column: str, target: str, ondelete: str = "CASCADE", nullable: bool = False) -> sa.Column:
    """A BIGINT foreign key to a new platform table whose `id` is BIGSERIAL (e.g. "skills.id")."""
    return sa.Column(column, sa.BigInteger, sa.ForeignKey(target, ondelete=ondelete), nullable=nullable)


def external_id_column() -> sa.Column:
    """The UUID exposed to the API; the database fills it with gen_random_uuid() when a row is inserted without one."""
    return sa.Column("external_id", PgUuid(as_uuid=True), nullable=False,
                     server_default=sa.text("gen_random_uuid()"), unique=True)


def audit_columns() -> list[sa.Column]:
    """The audit + soft-delete columns shared by every new platform table. `*_by` are the actor's user id as a plain
    BIGINT (not a foreign key), so the audit trail outlives the actor's user row."""
    return [
        timestamp("created_at"),
        sa.Column("created_by", sa.BigInteger),
        timestamp("updated_at"),
        sa.Column("updated_by", sa.BigInteger),
        sa.Column("is_deleted", sa.Boolean, nullable=False, server_default=sa.false()),
        timestamp("deleted_at", nullable=True),
        sa.Column("deleted_by", sa.BigInteger),
        sa.Column("deleted_reason", sa.String(500)),
    ]


def enable_tenant_rls(table: str) -> None:
    """Turn on Row-Level Security for a new platform table and add a tenant-isolation policy.

    The policy keys off the `app.client_id` session GUC set from verified claims (agent_common.db.set_tenant_context);
    `current_setting(..., true)` uses missing_ok=true, so when the GUC is unset the predicate is NULL (no rows) rather
    than an error. RLS is not FORCED, so the owning role the services connect as still sees every row today - enabling
    it is additive. Flip to FORCE under a dedicated app role to actually enforce isolation (see the module docstring)."""
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"CREATE POLICY {table}_tenant_isolation ON {table} "
               f"USING (client_id = current_setting('app.client_id', true)::integer) "
               f"WITH CHECK (client_id = current_setting('app.client_id', true)::integer)")


def create_tenancy() -> None:
    op.create_table(
        "clients",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("slug", sa.String(64), nullable=False, unique=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.true()),
        timestamp("created_at"), timestamp("updated_at"),
    )
    op.create_table(
        "projects",
        sa.Column("id", sa.Integer, primary_key=True),
        foreign_key("client_id", "clients.id"),
        sa.Column("slug", sa.String(64), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.String(1000)),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.true()),
        timestamp("created_at"), timestamp("updated_at"),
        sa.UniqueConstraint("client_id", "slug", name="uq_projects_client_slug"),   # also indexes client_id
    )
    op.create_table(
        "users",
        sa.Column("id", sa.Integer, primary_key=True),
        foreign_key("client_id", "clients.id"),
        sa.Column("username", sa.String(64), nullable=False, unique=True),
        sa.Column("email", sa.String(255)),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("role", sa.String(32), nullable=False, server_default="recruiter"),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.true()),
        timestamp("last_login_at", nullable=True), timestamp("created_at"), timestamp("updated_at"),
    )
    op.create_index("ix_users_client_id", "users", ["client_id"])
    op.create_table(
        "project_members",
        sa.Column("project_id", sa.Integer, sa.ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        timestamp("created_at"),
    )
    op.create_index("ix_project_members_user_id", "project_members", ["user_id"])


def create_documents() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")   # pgvector, for chunk embeddings
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")  # trigram similarity; full text uses built-in tsvector
    op.create_table(
        "documents",
        sa.Column("id", sa.Integer, primary_key=True),
        *tenant_columns(),
        foreign_key("user_id", "users.id"),
        sa.Column("kind", sa.String(16), nullable=False),               # resume | jd
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("content_type", sa.String(100), nullable=False),
        sa.Column("size_bytes", sa.Integer, nullable=False, server_default="0"),
        sa.Column("pages", sa.Integer, nullable=False, server_default="0"),
        sa.Column("text", sa.Text, nullable=False),
        sa.Column("text_hash", sa.String(64), nullable=False),
        sa.Column("blob_key", sa.String(128)),                          # original file in the blob store
        timestamp("created_at"),
        sa.UniqueConstraint("client_id", "project_id", "kind", "text_hash", name="uq_documents_tenant_kind_hash"),
    )
    op.create_index("ix_documents_tenant_kind", "documents", ["client_id", "project_id", "kind"])
    op.create_index("ix_documents_project_id", "documents", ["project_id"])
    op.create_index("ix_documents_user_id", "documents", ["user_id"])

    op.create_table(
        "document_blobs",
        sa.Column("key", sa.String(128), primary_key=True),
        *tenant_columns(),
        sa.Column("data", sa.LargeBinary, nullable=False),
        timestamp("created_at"),
    )
    op.create_index("ix_document_blobs_tenant", "document_blobs", ["client_id", "project_id"])
    op.create_index("ix_document_blobs_project_id", "document_blobs", ["project_id"])

    op.create_table(
        "document_chunks",
        sa.Column("id", sa.BigInteger, primary_key=True),
        *tenant_columns(),
        foreign_key("document_id", "documents.id"),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("chunk_index", sa.Integer, nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("embedding_model", sa.String(128)),
        timestamp("created_at"),
    )
    # embedding: pgvector without a fixed dimension (the model is stored per row);
    # tsv: full-text vector generated from the content, for keyword search
    op.execute("ALTER TABLE document_chunks ADD COLUMN embedding vector")
    op.execute("ALTER TABLE document_chunks ADD COLUMN tsv tsvector "
               "GENERATED ALWAYS AS (to_tsvector('english', content)) STORED")
    op.create_index("ix_document_chunks_tenant_doc", "document_chunks", ["client_id", "project_id", "document_id"])
    op.create_index("ix_document_chunks_project_id", "document_chunks", ["project_id"])
    op.create_index("ix_document_chunks_document_id", "document_chunks", ["document_id", "chunk_index"])
    op.execute("CREATE INDEX ix_document_chunks_tsv ON document_chunks USING GIN (tsv)")


def create_chats() -> None:
    op.create_table(
        "chats",
        sa.Column("id", sa.String(32), primary_key=True),
        *tenant_columns(),
        foreign_key("user_id", "users.id"),
        sa.Column("title", sa.String(200), nullable=False, server_default="New chat"),
        sa.Column("jd_text", sa.Text),
        foreign_key("jd_document_id", "documents.id", ondelete="SET NULL", nullable=True),
        foreign_key("resume_document_id", "documents.id", ondelete="SET NULL", nullable=True),
        sa.Column("last_score", sa.Float),
        timestamp("created_at"), timestamp("updated_at"),
    )
    op.create_index("ix_chats_tenant_user_updated", "chats", ["client_id", "project_id", "user_id", "updated_at"])
    op.create_index("ix_chats_project_id", "chats", ["project_id"])
    op.create_index("ix_chats_user_id", "chats", ["user_id"])
    op.create_index("ix_chats_jd_document_id", "chats", ["jd_document_id"])
    op.create_index("ix_chats_resume_document_id", "chats", ["resume_document_id"])

    op.create_table(
        "chat_messages",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("chat_id", sa.String(32), sa.ForeignKey("chats.id", ondelete="CASCADE"), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("data", JSONB, nullable=False, server_default=EMPTY_JSON_OBJECT),
        timestamp("created_at"),
    )
    op.create_index("ix_chat_messages_chat_id_id", "chat_messages", ["chat_id", "id"])

    # running summary of what the recruiter asked and decided in a chat
    op.create_table(
        "chat_memory",
        sa.Column("chat_id", sa.String(32), sa.ForeignKey("chats.id", ondelete="CASCADE"), primary_key=True),
        *tenant_columns(),
        sa.Column("summary", sa.Text, nullable=False, server_default=""),
        sa.Column("notes", JSONB, nullable=False, server_default=EMPTY_JSON_OBJECT),
        sa.Column("last_message_id", sa.Integer, nullable=False, server_default="0"),
        sa.Column("version", sa.String(32), nullable=False, server_default=""),
        timestamp("updated_at"),
    )
    op.create_index("ix_chat_memory_tenant", "chat_memory", ["client_id", "project_id"])
    op.create_index("ix_chat_memory_project_id", "chat_memory", ["project_id"])

    # one JSON record per chat turn (journey, outcome, timings)
    op.create_table(
        "chat_turns",
        sa.Column("id", sa.String(32), primary_key=True),                  # the turn id streamed to the browser
        *tenant_columns(),
        sa.Column("chat_id", sa.String(32), sa.ForeignKey("chats.id", ondelete="CASCADE"), nullable=False),
        foreign_key("user_id", "users.id"),
        sa.Column("journey", sa.String(32), nullable=False),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("error_code", sa.String(32)),
        sa.Column("duration_ms", sa.Integer, nullable=False),
        sa.Column("record", JSONB, nullable=False, server_default=EMPTY_JSON_OBJECT),
        timestamp("created_at"),
    )
    op.create_index("ix_chat_turns_tenant_created", "chat_turns", ["client_id", "project_id", "created_at"])
    op.create_index("ix_chat_turns_project_id", "chat_turns", ["project_id"])
    op.create_index("ix_chat_turns_chat", "chat_turns", ["chat_id", "created_at"])
    op.create_index("ix_chat_turns_user_id", "chat_turns", ["user_id"])

    op.create_table(
        "screenings",
        sa.Column("id", sa.Integer, primary_key=True),
        *tenant_columns(),
        sa.Column("chat_id", sa.String(32), sa.ForeignKey("chats.id", ondelete="CASCADE"), nullable=False),
        foreign_key("user_id", "users.id"),
        foreign_key("resume_document_id", "documents.id", ondelete="SET NULL", nullable=True),
        sa.Column("jd_hash", sa.String(64), nullable=False),
        sa.Column("score", sa.Float, nullable=False),
        sa.Column("band", sa.String(32), nullable=False),
        sa.Column("recommendation", sa.String(64), nullable=False),
        sa.Column("skill", sa.String(128), nullable=False),
        sa.Column("result", JSONB, nullable=False),
        timestamp("created_at"),
    )
    op.create_index("ix_screenings_tenant_created", "screenings", ["client_id", "project_id", "created_at"])
    op.create_index("ix_screenings_project_id", "screenings", ["project_id"])
    op.create_index("ix_screenings_chat_id_id", "screenings", ["chat_id", "id"])
    op.create_index("ix_screenings_user_id", "screenings", ["user_id"])
    op.create_index("ix_screenings_resume_document_id", "screenings", ["resume_document_id"])


def create_agent_memory() -> None:
    """Strands session repository of agent-skill: one session per (client, project, user), keyed by session_id."""
    op.create_table(
        "agent_sessions",
        sa.Column("session_id", sa.String(128), primary_key=True),
        sa.Column("data", JSONB, nullable=False),
        timestamp("created_at"), timestamp("updated_at"),
    )
    op.create_table(
        "agent_states",
        sa.Column("session_id", sa.String(128), primary_key=True),
        sa.Column("agent_id", sa.String(128), primary_key=True),
        sa.Column("data", JSONB, nullable=False),
        timestamp("updated_at"),
    )
    op.create_table(
        "agent_messages",
        sa.Column("session_id", sa.String(128), primary_key=True),
        sa.Column("agent_id", sa.String(128), primary_key=True),
        sa.Column("message_id", sa.Integer, primary_key=True),
        sa.Column("data", JSONB, nullable=False),
        timestamp("created_at"),
    )


def create_knowledge_episodes() -> None:
    """Ingestion queue + status + erasure trail of agent-knowledge-graph. One row may produce several graph
    episodes (document sections, `graph_episode_uuids`); `subject_ref` names what the text is about."""
    op.create_table(
        "knowledge_episodes",
        sa.Column("id", sa.String(32), primary_key=True),
        *tenant_columns(),
        sa.Column("namespace", sa.String(32), nullable=False),
        sa.Column("graph_id", sa.String(64), nullable=False),
        sa.Column("source_type", sa.String(32), nullable=False),
        sa.Column("source_ref", sa.String(128), nullable=False),
        sa.Column("subject_ref", sa.String(128)),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("content", sa.Text),                                   # cleared once done or failed
        sa.Column("guidance", sa.Text, nullable=False, server_default=""),
        sa.Column("ontology", JSONB),
        sa.Column("reference_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="queued"),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        timestamp("available_at"),
        sa.Column("error_code", sa.String(32)),
        sa.Column("graph_episode_uuids", JSONB, nullable=False, server_default=EMPTY_JSON_ARRAY),
        timestamp("created_at"), timestamp("updated_at"),
        sa.UniqueConstraint("graph_id", "content_hash", name="uq_knowledge_episodes_graph_hash"),
    )
    op.create_index("ix_knowledge_episodes_tenant_created", "knowledge_episodes",
                    ["client_id", "project_id", "created_at"])
    op.create_index("ix_knowledge_episodes_project_id", "knowledge_episodes", ["project_id"])
    op.create_index("ix_knowledge_episodes_graph_source", "knowledge_episodes", ["graph_id", "source_ref"])
    op.create_index("ix_knowledge_episodes_graph_subject", "knowledge_episodes", ["graph_id", "subject_ref"])
    # worker claim: only rows that still need work are indexed
    op.create_index("ix_knowledge_episodes_due", "knowledge_episodes", ["available_at"],
                    postgresql_where=sa.text("status IN ('queued', 'processing')"))


def create_platform_admin() -> None:
    """Shared admin-plane tables (skills + lifecycle, notifications, groups, access policies, invites).

    New-table conventions: BIGSERIAL id + external_id UUID + audit mixin + client_id RLS (see the module docstring)."""
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")   # gen_random_uuid() for external_id defaults

    # skills: active_version_id has no FK here (it points at skill_versions, created next); the FK is added afterwards.
    op.create_table(
        "skills",
        sa.Column("id", sa.BigInteger, primary_key=True),
        external_id_column(),
        foreign_key("client_id", "clients.id"),
        sa.Column("domain", sa.String(64), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("description", sa.String(1000)),
        sa.Column("active_version_id", sa.BigInteger),
        *audit_columns(),
        sa.UniqueConstraint("client_id", "domain", "name", name="uq_skills_client_domain_name"),
    )
    op.create_index("ix_skills_client_id", "skills", ["client_id"])
    op.create_index("ix_skills_active_version_id", "skills", ["active_version_id"])

    op.create_table(
        "skill_versions",
        sa.Column("id", sa.BigInteger, primary_key=True),
        external_id_column(),
        foreign_key("client_id", "clients.id"),
        big_foreign_key("skill_id", "skills.id"),
        sa.Column("version_number", sa.Integer, nullable=False, server_default="1"),
        sa.Column("state", sa.String(16), nullable=False, server_default="draft"),   # draft|in_review|published|archived|rejected
        foreign_key("author_id", "users.id"),
        sa.Column("workflow", JSONB, nullable=False, server_default=EMPTY_JSON_OBJECT),
        sa.Column("rejection_reason", sa.String(1000)),
        *audit_columns(),
        sa.UniqueConstraint("skill_id", "version_number", name="uq_skill_versions_skill_number"),
    )
    op.create_index("ix_skill_versions_client_id", "skill_versions", ["client_id"])
    op.create_index("ix_skill_versions_skill_id", "skill_versions", ["skill_id"])
    op.create_index("ix_skill_versions_author_id", "skill_versions", ["author_id"])
    op.create_index("ix_skill_versions_client_skill_state", "skill_versions", ["client_id", "skill_id", "state"])

    # Deferred FK: skills.active_version_id -> skill_versions.id (SET NULL so archiving a version does not delete skills)
    op.create_foreign_key("fk_skills_active_version", "skills", "skill_versions",
                          ["active_version_id"], ["id"], ondelete="SET NULL")

    op.create_table(
        "skill_version_files",
        sa.Column("id", sa.BigInteger, primary_key=True),
        external_id_column(),
        foreign_key("client_id", "clients.id"),
        big_foreign_key("skill_version_id", "skill_versions.id"),
        sa.Column("path", sa.String(255), nullable=False),
        sa.Column("content", sa.Text, nullable=False, server_default=""),
        sa.Column("content_hash", sa.String(64)),
        *audit_columns(),
        sa.UniqueConstraint("skill_version_id", "path", name="uq_skill_version_files_version_path"),
    )
    op.create_index("ix_skill_version_files_client_id", "skill_version_files", ["client_id"])
    op.create_index("ix_skill_version_files_version", "skill_version_files", ["skill_version_id"])

    op.create_table(
        "skill_approvals",
        sa.Column("id", sa.BigInteger, primary_key=True),
        external_id_column(),
        foreign_key("client_id", "clients.id"),
        big_foreign_key("skill_version_id", "skill_versions.id"),
        foreign_key("actor_id", "users.id"),
        sa.Column("from_state", sa.String(16), nullable=False),
        sa.Column("to_state", sa.String(16), nullable=False),
        sa.Column("comment", sa.String(2000)),
        *audit_columns(),
    )
    op.create_index("ix_skill_approvals_client_id", "skill_approvals", ["client_id"])
    op.create_index("ix_skill_approvals_actor_id", "skill_approvals", ["actor_id"])
    op.create_index("ix_skill_approvals_version_created", "skill_approvals", ["skill_version_id", "created_at"])

    op.create_table(
        "notifications",
        sa.Column("id", sa.BigInteger, primary_key=True),
        external_id_column(),
        foreign_key("client_id", "clients.id"),
        foreign_key("project_id", "projects.id", ondelete="SET NULL", nullable=True),
        foreign_key("recipient_user_id", "users.id"),
        sa.Column("type", sa.String(64), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("body", sa.Text, nullable=False, server_default=""),
        sa.Column("link", sa.String(500)),
        foreign_key("actor_user_id", "users.id", ondelete="SET NULL", nullable=True),
        sa.Column("is_read", sa.Boolean, nullable=False, server_default=sa.false()),
        *audit_columns(),
    )
    op.create_index("ix_notifications_client_id", "notifications", ["client_id"])
    op.create_index("ix_notifications_project_id", "notifications", ["project_id"])
    op.create_index("ix_notifications_actor_user_id", "notifications", ["actor_user_id"])
    # The notifications tab: a user's unread-first, newest-first list
    op.create_index("ix_notifications_recipient", "notifications",
                    ["client_id", "recipient_user_id", "is_read", "created_at"])

    op.create_table(
        "groups",
        sa.Column("id", sa.BigInteger, primary_key=True),
        external_id_column(),
        foreign_key("client_id", "clients.id"),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("description", sa.String(1000)),
        *audit_columns(),
        sa.UniqueConstraint("client_id", "name", name="uq_groups_client_name"),
    )
    op.create_index("ix_groups_client_id", "groups", ["client_id"])

    op.create_table(
        "group_members",
        sa.Column("id", sa.BigInteger, primary_key=True),
        external_id_column(),
        foreign_key("client_id", "clients.id"),
        big_foreign_key("group_id", "groups.id"),
        foreign_key("user_id", "users.id"),
        *audit_columns(),
        sa.UniqueConstraint("group_id", "user_id", name="uq_group_members_group_user"),
    )
    op.create_index("ix_group_members_client_id", "group_members", ["client_id"])
    op.create_index("ix_group_members_group_id", "group_members", ["group_id"])
    op.create_index("ix_group_members_user_id", "group_members", ["user_id"])

    op.create_table(
        "access_policies",
        sa.Column("id", sa.BigInteger, primary_key=True),
        external_id_column(),
        foreign_key("client_id", "clients.id"),
        sa.Column("principal_type", sa.String(16), nullable=False),    # user|group|project|client
        sa.Column("principal_id", sa.BigInteger, nullable=False),
        sa.Column("name", sa.String(128)),
        sa.Column("document", JSONB, nullable=False, server_default=EMPTY_JSON_OBJECT),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.true()),
        *audit_columns(),
    )
    op.create_index("ix_access_policies_client_id", "access_policies", ["client_id"])
    # Load a subject's effective policies: filter by client + principal, only enabled ones
    op.create_index("ix_access_policies_principal", "access_policies",
                    ["client_id", "principal_type", "principal_id", "enabled"])

    op.create_table(
        "user_invites",
        sa.Column("id", sa.BigInteger, primary_key=True),
        external_id_column(),
        foreign_key("client_id", "clients.id"),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column("groups", JSONB, nullable=False, server_default=EMPTY_JSON_ARRAY),
        sa.Column("projects", JSONB, nullable=False, server_default=EMPTY_JSON_ARRAY),
        sa.Column("token_hash", sa.String(128), nullable=False, unique=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),   # pending|accepted|expired|revoked
        timestamp("expires_at"),
        foreign_key("invited_by", "users.id"),
        timestamp("accepted_at", nullable=True),
        *audit_columns(),
    )
    op.create_index("ix_user_invites_client_id", "user_invites", ["client_id"])
    op.create_index("ix_user_invites_invited_by", "user_invites", ["invited_by"])
    op.create_index("ix_user_invites_client_email_status", "user_invites", ["client_id", "email", "status"])

    # Enable RLS (client_id isolation) on every new platform table.
    for table in ("skills", "skill_versions", "skill_version_files", "skill_approvals", "notifications",
                  "groups", "group_members", "access_policies", "user_invites"):
        enable_tenant_rls(table)


def upgrade() -> None:
    create_tenancy()
    create_documents()
    create_chats()
    create_agent_memory()
    create_knowledge_episodes()
    create_platform_admin()


def downgrade() -> None:
    # Drop the deferred circular FK before the two tables it ties together.
    op.drop_constraint("fk_skills_active_version", "skills", type_="foreignkey")
    for table in ("user_invites", "access_policies", "group_members", "groups", "notifications", "skill_approvals",
                  "skill_version_files", "skill_versions", "skills",
                  "knowledge_episodes", "agent_messages", "agent_states", "agent_sessions", "screenings",
                  "chat_turns", "chat_memory", "chat_messages", "chats", "document_chunks", "document_blobs",
                  "documents", "project_members", "users", "projects", "clients"):
        op.drop_table(table)
