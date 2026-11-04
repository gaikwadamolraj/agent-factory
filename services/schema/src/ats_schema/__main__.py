"""Schema job entry point: `python -m ats_schema [upgrade|seed|all]` (default: all).

1. create the target database if it does not exist
2. alembic upgrade head
3. apply seed/*.sql in name order when SEED_DEMO_DATA=true (scripts are idempotent)
"""
import sys
from pathlib import Path

import psycopg
from alembic import command
from alembic.config import Config
from psycopg import sql
from sqlalchemy.engine import make_url

from ats_common.logging import get_logger, log_event, setup_logging, shutdown_logging
from ats_schema.config import get_settings

ROOT = Path(__file__).resolve().parents[2]  # services/schema (or /app in the image)
log = get_logger("ats_schema")


def _psycopg_dsn(database: str | None = None) -> str:
    url = make_url(get_settings().sync_database_url)
    if database:
        url = url.set(database=database)
    return url.set(drivername="postgresql").render_as_string(hide_password=False)


def ensure_database() -> None:
    """CREATE DATABASE when missing (runs in autocommit on the maintenance database)."""
    settings = get_settings()
    target = make_url(settings.sync_database_url).database
    with psycopg.connect(_psycopg_dsn(settings.maintenance_database), autocommit=True) as conn:
        exists = conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (target,)).fetchone()
        if exists:
            log_event(log, "schema.database_exists", database=target)
            return
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(target)))
        log_event(log, "schema.database_created", database=target)


def upgrade() -> None:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    command.upgrade(config, "head")
    log_event(log, "schema.migrated", revision="head")


def seed() -> None:
    scripts = sorted((ROOT / "seed").glob("*.sql"))
    with psycopg.connect(_psycopg_dsn()) as conn:
        for script in scripts:
            conn.execute(script.read_text(encoding="utf-8"))
            log_event(log, "schema.seeded", script=script.name)
        conn.commit()


def main(argv: list[str]) -> int:
    settings = get_settings()
    setup_logging(settings.service_name, settings.app_env, settings.log_level, settings.log_json)
    action = argv[1] if len(argv) > 1 else "all"
    try:
        if action in ("all", "upgrade"):
            if settings.create_database:
                ensure_database()
            upgrade()
        if action == "seed" or (action == "all" and settings.seed_demo_data):
            seed()
        return 0
    except Exception:
        log.exception("schema.failed")
        return 1
    finally:
        shutdown_logging()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
