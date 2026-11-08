"""Settings base class. Every service subclasses `CommonSettings` and exposes an `lru_cache`d `get_settings()`.

Values come only from the environment (secrets are mounted as env vars at runtime), never from code.
"""
from typing import Optional
from urllib.parse import urlsplit, urlunsplit

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url

from agent_common.constants import AppEnv


class CommonSettings(BaseSettings):
    """Settings every service needs: identity, environment, logging, telemetry, database, redis, token verification."""

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
    db_pgbouncer: bool = False   # true when DATABASE_URL points at PgBouncer (transaction pooling): disables the
    #                              asyncpg prepared-statement cache so queries work through the pooler.
    redis_url: Optional[SecretStr] = None
    redis_db: Optional[int] = None          # overrides the database number in REDIS_URL (vector search needs 0)

    # Durable workflows (agent_common.workflow, docs/impl_temporal.md). Empty address = no workflow engine:
    # services fall back to their non-durable path, so this can be rolled out one service at a time.
    temporal_address: str = ""              # e.g. temporal.agent-infra.svc.cluster.local:7233
    temporal_namespace: str = "default"
    # Per-worker throttles. Activities call LLMs and MCP services, so max_concurrent_activities is what stops
    # one replica stampeding the model gateway; KEDA adds replicas, this bounds each one.
    # Starting a journey now happens inside user-facing requests (an upload), so the engine gets the same
    # treatment as every other outbound dependency: explicit timeouts and a shared circuit breaker.
    temporal_connect_timeout_s: float = 10.0
    temporal_call_timeout_s: float = 15.0
    temporal_max_concurrent_activities: int = 4
    temporal_max_concurrent_workflow_tasks: int = 32
    temporal_graceful_shutdown_s: int = 30

    # Secret backend (agent_common.secrets): where get_secret() reads from. "env" = k8s Secret mounted as env vars /
    # files (today). openbao / gcp / aws / azure are future drivers, selected here without any caller change.
    secret_backend: str = "env"
    secret_env_prefix: str = ""             # optional prefix stripped/added by EnvSecretProvider (e.g. "ATS_")

    # Auth: every token is signed by the auth-service (ES256) and verified here with its public keys (JWKS).
    auth_issuer: str = "http://auth-service"                       # `iss` of every token; same value everywhere
    auth_jwks_url: str = "http://auth-service/.well-known/jwks.json"
    auth_token_url: str = "http://auth-service/oauth/token"        # client_credentials (service tokens)
    auth_jwks_cache_ttl_s: Optional[float] = None   # periodic key refresh; None = only on an unknown kid
    auth_jwks_min_refetch_s: float = 10.0     # an unknown kid refetches the keys at most this often
    auth_leeway_s: int = 30                   # clock skew allowed on exp/iat
    auth_http_timeout_s: float = 5.0          # per call to the auth-service
    auth_http_retry_attempts: int = 2         # transport errors only
    service_client_secret: Optional[SecretStr] = None   # this service's client_credentials secret (k8s Secret)
    service_token_refresh_ratio: float = 0.8  # a cached service token is renewed after 80% of its lifetime
    authz_version_cache_ttl_s: float = 5.0   # in-process cache of a user's authz version (agent_common.authz)

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
    def redis_connection_url(self) -> str:
        """REDIS_URL with REDIS_DB applied (when set). Raises RuntimeError when REDIS_URL is not configured."""
        if self.redis_url is None:
            raise RuntimeError("REDIS_URL is not configured.")
        url = self.redis_url.get_secret_value()
        if self.redis_db is None:
            return url
        parts = urlsplit(url)
        return urlunsplit(parts._replace(path=f"/{self.redis_db}"))

    @property
    def async_database_url(self) -> str:
        """SQLAlchemy async URL (asyncpg)."""
        return self._db_url("asyncpg")

    @property
    def sync_database_url(self) -> str:
        """SQLAlchemy sync URL (psycopg 3)."""
        return self._db_url("psycopg")

    def require_service_client_secret(self) -> str:
        """The client_credentials secret of this service. Raises RuntimeError when SERVICE_CLIENT_SECRET is unset
        or too short (only services that call other services need it)."""
        if self.service_client_secret is None or len(self.service_client_secret.get_secret_value()) < 32:
            raise RuntimeError("SERVICE_CLIENT_SECRET must be set and at least 32 characters long.")
        return self.service_client_secret.get_secret_value()


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
        return parse_allow_list(self.tool_policy)


def parse_allow_list(text: str) -> dict[str, set[str]]:
    """Parse "name=a|b,other=c" into {"name": {"a", "b"}, "other": {"c"}} (blank entries are ignored).
    Used for TOOL_POLICY (caller -> tools) and OAUTH_CLIENT_AUDIENCES (caller -> services it may call)."""
    allowed: dict[str, set[str]] = {}
    for entry in text.split(","):
        name, _, values = entry.partition("=")
        if name.strip():
            allowed[name.strip()] = {value.strip() for value in values.split("|") if value.strip()}
    return allowed
