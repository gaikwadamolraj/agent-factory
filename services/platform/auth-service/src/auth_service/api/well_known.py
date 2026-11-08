"""Discovery endpoints: the public signing keys (JWKS) and a minimal OpenID-style configuration document.

Both are public (no secrets in them) and read by every service: AUTH_JWKS_URL points at /.well-known/jwks.json.
"""
from fastapi import APIRouter, Depends, Response

from agent_common.constants import JWT_ALGORITHM
from auth_service.constants import GRANT_CLIENT_CREDENTIALS, JWKS_PATH, OPENID_CONFIGURATION_PATH
from auth_service.container import Container, get_container

router = APIRouter(tags=["discovery"])
JWKS_MAX_AGE_S = 300        # lets HTTP caches hold the keys as long as the services' own JWKS cache


@router.get(JWKS_PATH)
async def jwks(response: Response, c: Container = Depends(get_container)) -> dict:
    """Public keys that verify every token (current key, plus the previous one during a rotation)."""
    response.headers["Cache-Control"] = f"public, max-age={JWKS_MAX_AGE_S}"
    return c.tokens.keys.jwks()


@router.get(OPENID_CONFIGURATION_PATH)
async def openid_configuration(c: Container = Depends(get_container)) -> dict:
    """Issuer, key set and token endpoint (standard snake_case names of OpenID Connect Discovery)."""
    settings = c.settings
    return {"issuer": settings.auth_issuer, "jwks_uri": settings.auth_jwks_url,
            "token_endpoint": settings.auth_token_url,
            "grant_types_supported": [GRANT_CLIENT_CREDENTIALS],
            "token_endpoint_auth_methods_supported": ["client_secret_post"],
            "id_token_signing_alg_values_supported": [JWT_ALGORITHM]}
