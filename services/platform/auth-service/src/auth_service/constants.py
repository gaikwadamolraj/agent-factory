"""auth-service constants (shared ones live in agent_common.constants)."""

SERVICE_NAME = "auth-service"
API_V1 = "/api/v1"

# Paths of this service. The browser reaches /api/v1/* through the frontend proxy (same URLs as before the move).
LOGIN_PATH = f"{API_V1}/auth/login"
REFRESH_PATH = f"{API_V1}/auth/refresh"
LOGOUT_PATH = f"{API_V1}/auth/logout"
TOKEN_PATH = "/oauth/token"                     # client_credentials (service tokens), cluster-internal
JWKS_PATH = "/.well-known/jwks.json"
OPENID_CONFIGURATION_PATH = "/.well-known/openid-configuration"

# Delegated identity, called by registry-service with a service token (aud auth-service). Not a user-token path.
IDENTITY_PREFIX = f"{API_V1}/identity"
# Public: the invitee accepts their invite and sets a password before they have any session.
INVITE_ACCEPT_PATH = f"{API_V1}/auth/invite/accept"

# /api/v1 paths that need no USER access token. Identity paths use a service token (checked in the router), so they
# are excluded from the user-token middleware via a public prefix (see app.py).
PUBLIC_API_PATHS = frozenset({LOGIN_PATH, REFRESH_PATH, LOGOUT_PATH, INVITE_ACCEPT_PATH})

GRANT_CLIENT_CREDENTIALS = "client_credentials"
TOKEN_TYPE_BEARER = "Bearer"
CLIENT_SECRET_ENV_PREFIX = "CLIENT_SECRET_"     # CLIENT_SECRET_ATS_ORCHESTRATOR=<secret of that client>
MIN_CLIENT_SECRET_LENGTH = 32
SERVICE_NAME_PATTERN = r"^[a-z][a-z0-9-]{1,62}$"
