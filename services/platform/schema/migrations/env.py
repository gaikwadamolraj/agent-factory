"""Alembic environment. Migrations are written by hand (reviewed DDL with explicit indexes), so no autogenerate
metadata is wired in; services' SQLModel models must match these migrations."""
from alembic import context
from sqlalchemy import create_engine, pool

from ats_schema.config import get_settings

VERSION_TABLE = "alembic_version"


def run_migrations_offline() -> None:
    context.configure(url=get_settings().sync_database_url, literal_binds=True, version_table=VERSION_TABLE)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(get_settings().sync_database_url, poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, version_table=VERSION_TABLE, transaction_per_migration=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
