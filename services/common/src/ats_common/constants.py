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

# ---- JWT audiences: user tokens and service-to-service tokens are never interchangeable
JWT_AUDIENCE_USER = "ats-api"
JWT_AUDIENCE_AGENT = "ats-agent"
JWT_AUDIENCE_COMPUTE = "ats-compute"
JWT_AUDIENCE_KNOWLEDGE = "ats-knowledge"
JWT_AUDIENCE_REFRESH = "ats-refresh"
JWT_ISSUER = "ats-platform"

# ---- Limits
MAX_REQUEST_ID_LENGTH = 64
MAX_LOG_FIELD_LENGTH = 2_000
