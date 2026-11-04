"""Schema service settings."""
from functools import lru_cache

from ats_common.config import CommonSettings


class SchemaSettings(CommonSettings):
    service_name: str = "ats-schema"
    create_database: bool = True       # CREATE DATABASE <name> if it does not exist (needs CREATEDB privilege)
    maintenance_database: str = "postgres"
    seed_demo_data: bool = False       # apply seed/*.sql after migrating (idempotent)


@lru_cache
def get_settings() -> SchemaSettings:
    return SchemaSettings()
