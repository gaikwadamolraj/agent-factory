"""/api/v1/auth: sign-in, silent refresh and sign-out with httpOnly cookies (tokens never reach JavaScript).

* login   -> sets ats_access (short-lived) + ats_refresh (rotating, allow-listed in Redis); returns the session.
* refresh -> verifies + rotates the refresh token (old jti deleted), sets new cookies. Called silently by the UI.
* logout  -> revokes the refresh token and clears both cookies.
API clients may still send `Authorization: Bearer <access token>`.
"""
from fastapi import APIRouter, Depends, Request, Response
from sqlmodel.ext.asyncio.session import AsyncSession

from ats_common.constants import COOKIE_ACCESS_TOKEN, COOKIE_REFRESH_TOKEN
from ats_common.db import get_session
from ats_common.db.repositories import ClientRepository, ProjectRepository, UserRepository
from ats_common.db.tables import User
from ats_common.errors import AppError, Errors
from ats_common.logging import get_logger, log_event
from ats_common.redis_keys import Keys, build_key, digest, ttl
from ats_common.security import (UserClaims, create_refresh_token, create_user_token, decode_refresh_token,
                                 verify_password)
from orchestrator import metrics
from orchestrator.api.deps import current_user
from orchestrator.api.v1.schemas import ClientOut, LoginRequest, ProjectOut, SessionOut, UserOut
from orchestrator.config import OrchestratorSettings
from orchestrator.container import get_container
from orchestrator.errors import OrchErrors

router = APIRouter(prefix="/auth", tags=["auth"])
log = get_logger(__name__)


async def _session_out(session: AsyncSession, user: User) -> dict:
    client = await ClientRepository(session).get(user.client_id)
    projects = await ProjectRepository(session).list_for_user(user.id, user.client_id)
    return {"user": UserOut.model_validate(user), "client": ClientOut.model_validate(client),
            "projects": [ProjectOut.model_validate(p) for p in projects]}


def _set_cookie(response: Response, settings: OrchestratorSettings, name: str, value: str, max_age: int) -> None:
    response.set_cookie(name, value, max_age=max_age, httponly=True, secure=settings.cookie_secure,
                        samesite=settings.cookie_samesite, path="/")


async def _issue_tokens(response: Response, user: User) -> int:
    """Set access + refresh cookies and allow-list the refresh jti. Returns access lifetime (seconds)."""
    c = get_container()
    secret = c.settings.require_jwt_secret()
    access, access_ttl = create_user_token(secret, UserClaims(user.id, user.client_id, user.role, user.username),
                                           c.settings.access_token_minutes, c.settings.jwt_algorithm)
    refresh, jti, refresh_ttl = create_refresh_token(secret, user.id, c.settings.refresh_token_days,
                                                     c.settings.jwt_algorithm)
    await c.redis.setex(build_key(Keys.REFRESH_TOKEN, c.settings.app_env, jti=jti), refresh_ttl, str(user.id))
    _set_cookie(response, c.settings, COOKIE_ACCESS_TOKEN, access, access_ttl)
    _set_cookie(response, c.settings, COOKIE_REFRESH_TOKEN, refresh, refresh_ttl)
    return access_ttl


def _clear_cookies(response: Response, settings: OrchestratorSettings) -> None:
    for name in (COOKIE_ACCESS_TOKEN, COOKIE_REFRESH_TOKEN):
        response.delete_cookie(name, path="/", secure=settings.cookie_secure, httponly=True,
                               samesite=settings.cookie_samesite)


@router.post("/login", response_model=SessionOut, response_model_by_alias=True)
async def login(body: LoginRequest, response: Response, session: AsyncSession = Depends(get_session)):
    """Verify credentials; sets auth cookies and returns the user, client and projects."""
    c = get_container()
    attempts_key = build_key(Keys.LOGIN_ATTEMPTS, c.settings.app_env, username_digest=digest(body.username.lower()))
    failures = int(await c.redis.get(attempts_key) or 0)
    if failures >= c.settings.login_max_failures:
        metrics.logins_total.add(1, {"outcome": "throttled"})
        raise AppError(OrchErrors.LOGIN_THROTTLED, retry_after=max(1, await c.redis.ttl(attempts_key)))

    user = await UserRepository(session).get_by_username(body.username)
    if not user or not user.is_active or not verify_password(body.password, user.password_hash):
        await c.redis.incr(attempts_key)
        await c.redis.expire(attempts_key, ttl(Keys.LOGIN_ATTEMPTS))
        metrics.logins_total.add(1, {"outcome": "failed"})
        log_event(log, "auth.login_failed", username_digest=digest(body.username.lower()))
        raise AppError(Errors.AUTH_CREDENTIALS)

    await c.redis.delete(attempts_key)
    await UserRepository(session).mark_login(user)
    expires_in = await _issue_tokens(response, user)
    metrics.logins_total.add(1, {"outcome": "ok"})
    log_event(log, "auth.login_succeeded", user_id=user.id, username=user.username, client_id=user.client_id)
    return SessionOut(expires_in=expires_in, **await _session_out(session, user))


@router.post("/refresh", response_model=SessionOut, response_model_by_alias=True)
async def refresh(request: Request, response: Response, session: AsyncSession = Depends(get_session)):
    """Rotate tokens using the refresh cookie (no credentials needed). 401 means: sign in again."""
    c = get_container()
    token = request.cookies.get(COOKIE_REFRESH_TOKEN)
    if not token:
        raise AppError(Errors.AUTH_TOKEN_MISSING)
    claims = decode_refresh_token(c.settings.require_jwt_secret(), token, c.settings.jwt_algorithm)
    key = build_key(Keys.REFRESH_TOKEN, c.settings.app_env, jti=claims.jti)
    owner = await c.redis.getdel(key)          # single use: a replayed refresh token is rejected
    user = await UserRepository(session).get(claims.user_id) if owner == str(claims.user_id) else None
    if user is None or not user.is_active:
        _clear_cookies(response, c.settings)
        log_event(log, "auth.refresh_rejected", user_id=claims.user_id)
        raise AppError(Errors.AUTH_TOKEN_INVALID)
    expires_in = await _issue_tokens(response, user)
    metrics.logins_total.add(1, {"outcome": "refreshed"})
    return SessionOut(expires_in=expires_in, **await _session_out(session, user))


@router.post("/logout", status_code=204)
async def logout(request: Request, response: Response):
    """Revoke the refresh token and clear the auth cookies (idempotent)."""
    c = get_container()
    token = request.cookies.get(COOKIE_REFRESH_TOKEN)
    if token:
        try:
            claims = decode_refresh_token(c.settings.require_jwt_secret(), token, c.settings.jwt_algorithm)
            await c.redis.delete(build_key(Keys.REFRESH_TOKEN, c.settings.app_env, jti=claims.jti))
        except AppError:
            pass
    _clear_cookies(response, c.settings)
    response.status_code = 204
    return response


@router.get("/me", response_model=SessionOut, response_model_by_alias=True)
async def me(claims: UserClaims = Depends(current_user), session: AsyncSession = Depends(get_session)):
    """Current user, client and projects (used by the UI to pick X-Client-Id / X-Project-Id)."""
    user = await UserRepository(session).get(claims.user_id)
    if user is None or not user.is_active:
        raise AppError(Errors.AUTH_TOKEN_INVALID)
    return SessionOut(**await _session_out(session, user))
