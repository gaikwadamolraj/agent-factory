"""Row-level security on the ten tenant tables that had none, and a non-owner role to make it bite.

Fourteen tables carried a tenant-isolation policy; ten did not - and the ten were the ones holding the
actual customer data: documents and their blobs and chunks, chats and their messages and memory, screenings,
knowledge episodes, projects and users. Application code already scopes every query through `TenantScope`,
but RLS is the backstop for the query that forgets, and a backstop with a hole in it is worth very little.

**Policies do not apply to a table's owner.** This was measured, not assumed: a superuser or the owning role
reads every tenant's rows whatever the policy says. So the policies here are only real if the application
connects as a role that does not own the tables, which is what `app_role` exists for. Creating a *login*
role with a password belongs to the deployment, not to a migration in version control; this creates the
group role and its grants, and the deployment grants its login role membership of it.

Revision ID: 0005_rls_remaining_tables
Revises: 0004_match_results
"""
from alembic import op

revision = "0005_rls_remaining_tables"
down_revision = "0004_match_results"
branch_labels = None
depends_on = None

# Every remaining table with a `client_id`. Ordered as they appear in the product: documents, then the chat
# that uses them, then what the chat produced, then the tenancy tables themselves.
TENANT_TABLES = (
    "documents",
    "document_blobs",
    "document_chunks",
    "chats",
    "chat_memory",
    "chat_turns",
    "screenings",
    "knowledge_episodes",
    "projects",
    "users",
)

# `chat_messages` holds the actual conversation and carries no `client_id` of its own - it is scoped through
# the chat it belongs to. It therefore needs a policy that follows that link rather than being left out,
# which would make it the one unprotected table holding message text. The subquery is on the chats primary
# key, so it is an index lookup per row, and `chat_id` is already the leading column of its own index.
CHILD_TABLES = {
    "chat_messages": ("chat_id", "chats"),
}

# The group role the application connects as. No LOGIN and no password here on purpose: a credential in a
# migration is a credential in version control.
APP_ROLE = "ats_app"


def upgrade() -> None:
    for table in TENANT_TABLES:
        # `IF EXISTS` on the policy drop so a re-run is clean; enabling RLS twice is already idempotent.
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"DROP POLICY IF EXISTS {table}_tenant_isolation ON {table}")
        op.execute(
            f"CREATE POLICY {table}_tenant_isolation ON {table} "
            "USING (client_id = current_setting('app.client_id', true)::integer) "
            "WITH CHECK (client_id = current_setting('app.client_id', true)::integer)"
        )

    for table, (column, parent) in CHILD_TABLES.items():
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"DROP POLICY IF EXISTS {table}_tenant_isolation ON {table}")
        condition = (f"EXISTS (SELECT 1 FROM {parent} WHERE {parent}.id = {table}.{column} "
                     f"AND {parent}.client_id = current_setting('app.client_id', true)::integer)")
        op.execute(f"CREATE POLICY {table}_tenant_isolation ON {table} "
                   f"USING ({condition}) WITH CHECK ({condition})")

    # The role the application should connect as. Created only if absent, so re-running and a pre-provisioned
    # role both work.
    op.execute(
        f"DO $$ BEGIN "
        f"IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN "
        f"CREATE ROLE {APP_ROLE} NOLOGIN; "
        f"END IF; END $$"
    )
    # Read and write the data, never change its shape: no CREATE, no ALTER, no DROP. Schema changes are this
    # service's job alone, and an application that cannot alter a table cannot be made to drop one.
    op.execute(f"GRANT USAGE ON SCHEMA public TO {APP_ROLE}")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {APP_ROLE}")
    op.execute(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {APP_ROLE}")
    # Tables added by a later migration are covered without anyone remembering to come back here.
    op.execute(f"ALTER DEFAULT PRIVILEGES IN SCHEMA public "
               f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {APP_ROLE}")
    op.execute(f"ALTER DEFAULT PRIVILEGES IN SCHEMA public "
               f"GRANT USAGE, SELECT ON SEQUENCES TO {APP_ROLE}")


def downgrade() -> None:
    for table in (*TENANT_TABLES, *CHILD_TABLES):
        op.execute(f"DROP POLICY IF EXISTS {table}_tenant_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
    # The role and its grants are left in place: other databases or roles may have been granted membership
    # of it by the deployment, and dropping it here would break them for no benefit.
