"""Constants shared by every service. Service-specific constants live in each service's `constants.py`."""
from enum import StrEnum


class AppEnv(StrEnum):
    """Deployment environment. Behaviour that must differ per environment checks `Settings.is_prod`."""

    DEV = "DEV"
    STAGE = "STAGE"
    UAT = "UAT"
    PRE_PROD = "PRE_PROD"
    PROD = "PROD"


class Role(StrEnum):
    """User role inside a client (tenant)."""

    ADMIN = "admin"          # manages the client's projects and users
    RECRUITER = "recruiter"  # screens candidates in the projects they belong to
    VIEWER = "viewer"        # read-only


# ---- Health-check endpoints: never logged or traced (probes call them every few seconds)
HEALTH_PATHS = frozenset({"/healthz", "/readyz"})

# ---- HTTP headers (external names follow industry convention)
HEADER_REQUEST_ID = "X-Request-Id"
HEADER_CLIENT_ID = "X-Client-Id"
HEADER_PROJECT_ID = "X-Project-Id"
HEADER_SERVED_BY = "X-Served-By"          # never sent in PROD
HEADER_RETRY_AFTER = "Retry-After"
HEADER_AUTHORIZATION = "Authorization"
HEADER_CSRF = "X-Requested-With"         # required on cookie-authenticated writes (CSRF guard)

# ---- Auth cookies (httpOnly; never readable by JavaScript)
COOKIE_ACCESS_TOKEN = "ats_access"
COOKIE_REFRESH_TOKEN = "ats_refresh"

# ---- JWTs: signed only by the auth-service (ES256, public keys at its JWKS endpoint).
# User tokens and service-to-service tokens are never interchangeable (different audience).
# A service audience is the called service's name (= folder = deployment = Service DNS name).
JWT_ALGORITHM = "ES256"
JWT_AUDIENCE_USER = "ats-api"
JWT_AUDIENCE_REFRESH = "ats-refresh"     # only the auth-service accepts it
JWT_AUDIENCE_SKILL = "agent-skill"
JWT_AUDIENCE_COMPUTE = "agent-compute"
JWT_AUDIENCE_KNOWLEDGE_GRAPH = "agent-knowledge-graph"
JWT_AUDIENCE_GUARDRAIL = "agent-guardrail"
JWT_AUDIENCE_DOCUMENT = "agent-document"         # document-reader agent (PDF/DOCX/XLSX extraction), MCP only
JWT_AUDIENCE_REGISTRY = "registry-service"       # admin/authoring plane (packs, skills, policies): agents call it over MCP
JWT_AUDIENCE_AUTH = "auth-service"               # identity writes registry-service delegates to the auth-service
TOKEN_KIND_SERVICE = "service"           # `kind` claim of service tokens
CLAIM_AUTHZ_VERSION = "authz_ver"        # access tokens: the user's authz version at issue time (agent_common.authz)

# ---- Limits
MAX_REQUEST_ID_LENGTH = 64
MAX_LOG_FIELD_LENGTH = 2_000


# ---- Durable runs (workflow_runs projection; docs/impl_temporal.md section 3)
class RunStatus(StrEnum):
    """Lifecycle of one durable run, as the application records it.

    Temporal owns the authoritative execution state; these are the values the product shows and filters on.
    They live here, in the one module a Temporal workflow can safely import (it holds literals only, so the
    workflow sandbox re-imports it without complaint), which is why the workflow and the projection cannot
    drift apart on spelling.
    """

    QUEUED = "queued"        # a row exists and the engine accepted the run; no worker has picked it up yet
    RUNNING = "running"      # a worker started executing it
    WAITING = "waiting"      # blocked on a signal: an approval, more evidence, a human decision
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


# These hold the *values*, not the members: `Enum.__hash__` hashes the member name, so a set of members
# would never match the plain strings that come back from the database.
# Terminal states: the run is over and `completed_at` is set.
RUN_TERMINAL_STATUSES = frozenset({RunStatus.COMPLETED.value, RunStatus.FAILED.value, RunStatus.CANCELLED.value})
# States a re-submitted request is allowed to start again, matching Temporal's ALLOW_DUPLICATE_FAILED_ONLY
# id-reuse policy: a run that finished or is still going is reattached to, a failed one may be retried.
RUN_RESTARTABLE_STATUSES = frozenset({RunStatus.FAILED.value, RunStatus.CANCELLED.value})
