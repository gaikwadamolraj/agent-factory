"""auth-service settings (environment only; keys and client secrets come from the k8s Secret at runtime)."""
import os
from functools import lru_cache
from typing import Literal, Optional

from pydantic import SecretStr

from agent_common.config import CommonSettings, parse_allow_list
from agent_common.constants import JWT_AUDIENCE_REFRESH, JWT_AUDIENCE_USER
from auth_service.constants import CLIENT_SECRET_ENV_PREFIX, MIN_CLIENT_SECRET_LENGTH, SERVICE_NAME

# Audiences that only the auth-service issues to users; a service token may never carry them.
RESERVED_AUDIENCES = frozenset({JWT_AUDIENCE_USER, JWT_AUDIENCE_REFRESH})


def _pem(value: Optional[SecretStr]) -> Optional[str]:
    """PEM text of a secret; a one-line value with literal "\\n" (handy in .env files) becomes multi-line."""
    if value is None or not value.get_secret_value().strip():
        return None
    return value.get_secret_value().replace("\\n", "\n").strip()


def client_secret_env_name(client_id: str) -> str:
    """Environment variable of a client's secret: ats-orchestrator -> CLIENT_SECRET_ATS_ORCHESTRATOR."""
    return CLIENT_SECRET_ENV_PREFIX + client_id.upper().replace("-", "_")


class AuthSettings(CommonSettings):
    service_name: str = SERVICE_NAME

    # Signing keys (PEM, EC P-256): the current key signs; the previous key's public part stays in the JWKS during
    # a rotation so tokens signed before it remain valid until they expire.
    auth_signing_key: Optional[SecretStr] = None
    auth_signing_key_previous: Optional[SecretStr] = None

    # Token lifetimes
    access_token_minutes: int = 5            # short: the UI refreshes silently at 80% of it
    refresh_token_days: int = 1              # rotated on every use; a stolen one dies within a day
    service_token_minutes: int = 5           # client_credentials tokens; callers cache them until ~80%

    # Browser cookies (httpOnly; tokens never reach JavaScript)
    cookie_secure: bool = True               # set false only for plain-http local dev
    cookie_samesite: Literal["strict", "lax"] = "strict"

    # Brute-force protection: failures per username / per client id within 15 minutes before a temporary lock
    login_max_failures: int = 5
    client_auth_max_failures: int = 5

    # Versioned password pepper (auth_service.passwords). 0 = no pepper (default; existing bcrypt hashes keep working).
    # Set to N and provide secret `login_pepper_v{1..N}` to pepper; rotate by raising it and adding the new secret.
    login_pepper_current_version: int = 0

    # Signed invite tokens (docs/impl_agent_factory.md section 6b). HMAC key from the secret store (`invite_signing_key`).
    invite_ttl_hours: int = 72
    invite_base_url: str = "http://localhost/admin/invite"   # the accept-invite page the link points at

    # Delegated identity: which service callers may use /api/v1/identity (service token, aud auth-service).
    identity_allowed_callers: str = "registry-service"

    def identity_caller_set(self) -> set[str]:
        return {c.strip() for c in self.identity_allowed_callers.split(",") if c.strip()}

    # Which service may get tokens for which services: "caller=aud1|aud2,caller2=aud3". Each caller's secret is in
    # CLIENT_SECRET_<CALLER> (k8s Secret key client-secret-<caller>).
    oauth_client_audiences: str = ""

    # API
    cors_origins: str = ""                   # comma-separated; the browser uses the frontend proxy (same origin)
    max_request_kb: int = 64                 # sign-in, user and token requests are tiny

    def signing_key_pem(self) -> str:
        """PEM of the current signing key. Raises RuntimeError when AUTH_SIGNING_KEY is not set."""
        pem = _pem(self.auth_signing_key)
        if pem is None:
            raise RuntimeError("AUTH_SIGNING_KEY must be set (PEM of an EC P-256 private key).")
        return pem

    def previous_signing_key_pem(self) -> Optional[str]:
        """PEM of the previous signing key during a rotation, else None."""
        return _pem(self.auth_signing_key_previous)

    def client_audiences(self) -> dict[str, set[str]]:
        """caller -> services it may get tokens for. Raises RuntimeError when a reserved audience is listed."""
        policy = parse_allow_list(self.oauth_client_audiences)
        for caller, audiences in policy.items():
            if audiences & RESERVED_AUDIENCES:
                raise RuntimeError(f"OAUTH_CLIENT_AUDIENCES: '{caller}' lists a user-token audience.")
        return policy

    def client_secrets(self) -> dict[str, str]:
        """Secret of every client in OAUTH_CLIENT_AUDIENCES, read from CLIENT_SECRET_<CLIENT>. A client without a
        secret (or with one shorter than 32 characters) is left out and cannot get tokens."""
        secrets: dict[str, str] = {}
        for client_id in self.client_audiences():
            value = os.environ.get(client_secret_env_name(client_id), "")
            if len(value) >= MIN_CLIENT_SECRET_LENGTH:
                secrets[client_id] = value
        return secrets

    @property
    def cors_origin_list(self) -> list[str]:
        origins = [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]
        return origins if (origins or self.is_prod) else ["*"]


@lru_cache
def get_settings() -> AuthSettings:
    return AuthSettings()
