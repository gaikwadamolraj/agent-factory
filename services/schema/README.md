# schema service

Owns the `ats_platform` database schema. Runs as a Kubernetes Job (`./deploy.sh schema`) or locally:

```bash
uv sync && PYTHONPATH=src DATABASE_URL=postgresql://... SEED_DEMO_DATA=true uv run python -m ats_schema
```

1. creates the database when missing (`CREATE_DATABASE=true`, needs CREATEDB on the maintenance DB)
2. `alembic upgrade head` (migrations in `migrations/versions`, hand-written with explicit indexes)
3. applies `seed/*.sql` when `SEED_DEMO_DATA=true` (idempotent; dev only)

New migration: add `migrations/versions/000N_<name>.py` with `down_revision` = previous id, then keep the services'
SQLModel models in sync. Tenant-owned tables always have `client_id` + `project_id` and a composite index starting
with them.
