"""Settings base class. Every service subclasses `CommonSettings` and exposes an `lru_cache`d `get_settings()`.

Values come only from the environment (secrets are mounted as env vars at runtime), never from code.
"""
from typing import Optional

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url

from ats_common.constants import AppEnv


class CommonSettings(BaseSettings):
    """Settings every service needs: identity, environment, logging, telemetry, database, redis, JWT."""

    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False, env_file=None)

    service_name: str = Field(description="Logical service name, e.g. 'ats-orchestrator'.")
    service_version: str = "0.1.0"
    app_env: AppEnv = AppEnv.DEV

    # Logging / telemetry
    log_level: str = "INFO"
    log_json: bool = True
    otel_enabled: bool = False
    otel_exporter_otlp_endpoint: str = "http://localhost:4318"
    otel_export_interval_ms: int = 15_000

    # Data stores
    database_url: Optional[SecretStr] = None
    database_name: Optional[str] = None     # overrides the database in DATABASE_URL (secret = credentials + host)
    db_pool_size: int = 10
    db_max_overflow: int = 5
    db_statement_timeout_ms: int = 15_000
    redis_url: Optional[SecretStr] = None

    # Auth
    jwt_secret: Optional[SecretStr] = None
    jwt_algorithm: str = "HS256"

    @field_validator("app_env", mode="before")
    @classmethod
    def _normalise_env(cls, value: str) -> str:
        return str(value).upper().replace("-", "_")

    @property
    def is_prod(self) -> bool:
        return self.app_env == AppEnv.PROD

    def _db_url(self, driver: str) -> str:
        if self.database_url is None:
            raise RuntimeError("DATABASE_URL is not configured.")
        url = make_url(self.database_url.get_secret_value()).set(drivername=f"postgresql+{driver}")
        if self.database_name:
            url = url.set(database=self.database_name)
        return url.render_as_string(hide_password=False)

    @property
    def async_database_url(self) -> str:
        """SQLAlchemy async URL (asyncpg)."""
        return self._db_url("asyncpg")

    @property
    def sync_database_url(self) -> str:
        """SQLAlchemy sync URL (psycopg 3)."""
        return self._db_url("psycopg")

    def require_jwt_secret(self) -> str:
        if self.jwt_secret is None or len(self.jwt_secret.get_secret_value()) < 32:
            raise RuntimeError("JWT_SECRET must be set and at least 32 characters long.")
        return self.jwt_secret.get_secret_value()


class McpServiceSettings(CommonSettings):
    """Settings shared by the MCP agent services (screening agent, compute agent): zero-trust caller list,
    per-caller tool allow-list and the listen address."""

    allowed_callers: str = "ats-orchestrator"
    tool_policy: str = ""                   # "caller=tool1|tool2,caller2=*"
    host: str = "0.0.0.0"
    port: int = 8001

    def caller_set(self) -> set[str]:
        return {c.strip() for c in self.allowed_callers.split(",") if c.strip()}

    def tool_policy_map(self) -> dict[str, set[str]]:
        """"caller=tool1|tool2,caller2=*" -> {"caller": {"tool1", "tool2"}, "caller2": {"*"}}"""
        policy: dict[str, set[str]] = {}
        for entry in self.tool_policy.split(","):
            caller, _, tools = entry.partition("=")
            if caller.strip():
                policy[caller.strip()] = {t.strip() for t in tools.split("|") if t.strip()}
        return policy
