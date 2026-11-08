"""POST /oauth/token: OAuth2 client_credentials grant for service-to-service tokens (cluster-internal).

Form fields (application/x-www-form-urlencoded, RFC 6749 section 4.4 with the `audience` parameter):
    grant_type=client_credentials  client_id=<calling service>  client_secret=<its secret>  audience=<called service>

The caller -> audience pairs come from OAUTH_CLIENT_AUDIENCES. Wrong secrets are counted per client id; after
CLIENT_AUTH_MAX_FAILURES the client is locked for 15 minutes (AUTH-RTL-0002), like a user's sign-in.
"""
from fastapi import APIRouter, Depends, Form, Response

from agent_common.errors import AppError
from agent_common.logging import get_logger, log_event
from auth_service import metrics
from auth_service.api.schemas import TokenOut
from auth_service.constants import GRANT_CLIENT_CREDENTIALS, SERVICE_NAME_PATTERN, TOKEN_PATH, TOKEN_TYPE_BEARER
from auth_service.container import Container, get_container
from auth_service.errors import AuthErrors

router = APIRouter(tags=["oauth"])
log = get_logger(__name__)


def _client_label(c: Container, client_id: str) -> str:
    """Metric label: the client id when it is a configured client, else "unknown" (bounded cardinality)."""
    return client_id if client_id in c.clients.client_ids else "unknown"


@router.post(TOKEN_PATH, response_model=TokenOut)
async def issue_service_token(response: Response,
                              grant_type: str = Form(max_length=64),
                              client_id: str = Form(pattern=SERVICE_NAME_PATTERN),
                              client_secret: str = Form(min_length=1, max_length=512),
                              audience: str = Form(pattern=SERVICE_NAME_PATTERN),
                              c: Container = Depends(get_container)):
    """Service token for `client_id` to call `audience`. Errors: AUTH-INV-0004 (grant), AUTH-RTL-0002 (locked),
    AUTH-INV-0003 (unknown client or wrong secret), AUTH-FBD-0003 (pair not allowed), VAL-INV-0001 (bad fields)."""
    if grant_type != GRANT_CLIENT_CREDENTIALS:
        raise AppError(AuthErrors.GRANT_UNSUPPORTED)

    retry_after = await c.client_throttle.retry_after(client_id)
    if retry_after is not None:
        metrics.service_tokens_total.add(1, {"client": _client_label(c, client_id), "outcome": "throttled"})
        raise AppError(AuthErrors.CLIENT_THROTTLED, retry_after=retry_after)

    if not c.clients.authenticate(client_id, client_secret):
        await c.client_throttle.record_failure(client_id)
        metrics.service_tokens_total.add(1, {"client": _client_label(c, client_id), "outcome": "bad_credentials"})
        log_event(log, "oauth.client_auth_failed", client=_client_label(c, client_id))
        raise AppError(AuthErrors.CLIENT_CREDENTIALS)
    await c.client_throttle.reset(client_id)

    if not c.clients.may_call(client_id, audience):
        metrics.service_tokens_total.add(1, {"client": client_id, "outcome": "forbidden"})
        log_event(log, "oauth.audience_forbidden", client=client_id, audience=audience)
        raise AppError(AuthErrors.AUDIENCE_FORBIDDEN)

    token = c.tokens.service_token(client_id, audience)
    metrics.service_tokens_total.add(1, {"client": client_id, "audience": audience, "outcome": "ok"})
    response.headers["Cache-Control"] = "no-store"          # RFC 6749 section 5.1
    return TokenOut(access_token=token.value, token_type=TOKEN_TYPE_BEARER, expires_in=token.expires_in)
