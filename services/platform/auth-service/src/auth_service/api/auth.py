"""/api/v1/auth: sign-in, silent refresh, sign-out and current user.

* login   -> sets ats_access (short-lived) + ats_refresh (rotating, allow-listed in Redis); returns the session.
* refresh -> verifies + rotates the refresh token (old jti consumed), sets new cookies. Called silently by the UI.
* logout  -> revokes the refresh token and clears both cookies.
* me      -> current user, client and projects.
Every other service verifies the access token itself (JWKS); there is no per-request call to this service.
Tokens travel only in httpOnly cookies; API clients may send `Authorization: Bearer <access token>` instead.
"""
from fastapi import APIRouter, Depends, Request, Response
from sqlmodel.ext.asyncio.session import AsyncSession

from agent_common.constants import COOKIE_ACCESS_TOKEN, COOKIE_REFRESH_TOKEN
from agent_common.db import get_session
from agent_common.db.repositories import ClientRepository, ProjectRepository, UserRepository
from agent_common.db.tables import User
from agent_common.errors import AppError, Errors
from agent_common.logging import get_logger, log_event
from agent_common.redis_keys import digest
from agent_common.security import UserClaims
from auth_service import metrics
from auth_service.api.deps import current_user
from auth_service.api.schemas import (ClientOut, InviteAcceptRequest, LoginRequest, ProjectOut, SessionOut, UserOut)
from auth_service.config import AuthSettings
from auth_service.container import Container, get_container
from auth_service.errors import AuthErrors

router = APIRouter(prefix="/auth", tags=["auth"])
log = get_logger(__name__)


async def _session_out(session: AsyncSession, user: User, expires_in: int | None = None) -> SessionOut:
    """User, client and the user's projects (what the UI needs to pick X-Client-Id / X-Project-Id)."""
    client = await ClientRepository(session).get(user.client_id)
    projects = await ProjectRepository(session).list_for_user(user.id, user.client_id)
    return SessionOut(user=UserOut.model_validate(user), client=ClientOut.model_validate(client),
                      projects=[ProjectOut.model_validate(p) for p in projects], expires_in=expires_in)


def _set_cookie(response: Response, settings: AuthSettings, name: str, value: str, max_age: int) -> None:
    response.set_cookie(name, value, max_age=max_age, httponly=True, secure=settings.cookie_secure,
                        samesite=settings.cookie_samesite, path="/")


def _clear_cookies(response: Response, settings: AuthSettings) -> None:
    for name in (COOKIE_ACCESS_TOKEN, COOKIE_REFRESH_TOKEN):
        response.delete_cookie(name, path="/", secure=settings.cookie_secure, httponly=True,
                               samesite=settings.cookie_samesite)


async def _issue_session_cookies(c: Container, response: Response, user: User) -> int:
    """Set access + refresh cookies and allow-list the refresh token. Returns the access lifetime (seconds).
    The access token carries the user's current authz version (read fresh, not from the in-process cache)."""
    authz_ver = await c.authz.current_version(user.id, user.client_id, fresh=True)
    access = c.tokens.access_token(UserClaims(user.id, user.client_id, user.role, user.username, authz_ver))
    refresh = c.tokens.refresh_token(user.id)
    await c.refresh_tokens.remember(refresh.jti, user.id, refresh.expires_in)
    _set_cookie(response, c.settings, COOKIE_ACCESS_TOKEN, access.value, access.expires_in)
    _set_cookie(response, c.settings, COOKIE_REFRESH_TOKEN, refresh.value, refresh.expires_in)
    return access.expires_in


@router.post("/login", response_model=SessionOut, response_model_by_alias=True)
async def login(body: LoginRequest, response: Response, session: AsyncSession = Depends(get_session),
                c: Container = Depends(get_container)):
    """Verify credentials; set the auth cookies and return the session. Locked for 15 minutes after
    LOGIN_MAX_FAILURES failures of one username (AUTH-RTL-0001 + retryAfter)."""
    retry_after = await c.login_throttle.retry_after(body.username)
    if retry_after is not None:
        metrics.logins_total.add(1, {"outcome": "throttled"})
        raise AppError(AuthErrors.LOGIN_THROTTLED, retry_after=retry_after)

    users = UserRepository(session)
    user = await users.get_by_username(body.username)
    verified = c.passwords.verify(body.password, user.password_hash) if user is not None else None
    if user is None or not user.is_active or verified is None or not verified.ok:
        await c.login_throttle.record_failure(body.username)
        metrics.logins_total.add(1, {"outcome": "failed"})
        log_event(log, "auth.login_failed", username_digest=digest(body.username.lower()))
        raise AppError(Errors.AUTH_CREDENTIALS)     # same answer for unknown user and wrong password

    await c.login_throttle.reset(body.username)
    # Transparent upgrade: a correct password stored under an older/no pepper is rehashed with the current pepper.
    if verified.needs_rehash:
        user.password_hash = c.passwords.hash(body.password)
        session.add(user)
        log_event(log, "auth.password_rehashed", user_id=user.id)
    await users.mark_login(user)
    expires_in = await _issue_session_cookies(c, response, user)
    metrics.logins_total.add(1, {"outcome": "ok"})
    log_event(log, "auth.login_succeeded", user_id=user.id, username=user.username, client_id=user.client_id)
    return await _session_out(session, user, expires_in)


@router.post("/refresh", response_model=SessionOut, response_model_by_alias=True)
async def refresh(request: Request, response: Response, session: AsyncSession = Depends(get_session),
                  c: Container = Depends(get_container)):
    """Rotate both tokens using the refresh cookie (no credentials needed). 401 means: sign in again."""
    token = request.cookies.get(COOKIE_REFRESH_TOKEN)
    if not token:
        raise AppError(Errors.AUTH_TOKEN_MISSING)
    claims = await c.tokens.verify_refresh(token)
    owner = await c.refresh_tokens.consume(claims.jti)       # single use: a replayed refresh token is rejected
    user = await UserRepository(session).get(claims.user_id) if owner == claims.user_id else None
    if user is None or not user.is_active:
        _clear_cookies(response, c.settings)
        log_event(log, "auth.refresh_rejected", user_id=claims.user_id)
        raise AppError(Errors.AUTH_TOKEN_INVALID)
    expires_in = await _issue_session_cookies(c, response, user)
    metrics.logins_total.add(1, {"outcome": "refreshed"})
    return await _session_out(session, user, expires_in)


@router.post("/logout", status_code=204)
async def logout(request: Request, response: Response, c: Container = Depends(get_container)):
    """Revoke the refresh token and clear the auth cookies (idempotent; works with an expired session too)."""
    token = request.cookies.get(COOKIE_REFRESH_TOKEN)
    if token:
        try:
            await c.refresh_tokens.revoke((await c.tokens.verify_refresh(token)).jti)
        except AppError:
            pass                                    # invalid/expired refresh token: nothing to revoke
    _clear_cookies(response, c.settings)
    response.status_code = 204
    return response


@router.post("/invite/accept", response_model=SessionOut, response_model_by_alias=True)
async def accept_invite(body: InviteAcceptRequest, response: Response, c: Container = Depends(get_container)):
    """Accept an invite: set a password (current pepper), activate the pending user, apply the invited groups/projects,
    consume the invite (single use) and sign the user in. Public (the invitee has no session yet)."""
    from datetime import datetime, timezone

    from sqlmodel import select

    from agent_common.db.platform_tables import GroupMember, UserInvite
    from agent_common.db.repositories import MembershipRepository

    if not c.invites.verify_signature(body.token):
        raise AppError(AuthErrors.INVITE_INVALID)
    client_id = c.invites.client_id_of(body.token)
    if client_id is None:
        raise AppError(AuthErrors.INVITE_INVALID)

    async with c.db.tenant_session(client_id) as session:       # invite + memberships under tenant context (RLS)
        invite = (await session.exec(select(UserInvite).where(
            UserInvite.token_hash == c.invites.token_hash(body.token)))).first()
        if invite is None or invite.status != "pending":
            raise AppError(AuthErrors.INVITE_INVALID)
        if invite.expires_at < datetime.now(timezone.utc):
            invite.status = "expired"
            session.add(invite)
            raise AppError(AuthErrors.INVITE_EXPIRED)

        users = UserRepository(session)
        user = await users.get_by_username(invite.email)
        if user is None:                                        # pending user missing: create it now
            user = User(client_id=client_id, username=invite.email, email=invite.email, password_hash="!",
                        role=invite.role, is_active=False)
            await users.add(user)
        user.password_hash = c.passwords.hash(body.password)    # the invitee's own password, current pepper
        user.is_active = True
        session.add(user)
        for group_id in invite.groups:
            session.add(GroupMember(client_id=client_id, group_id=group_id, user_id=user.id))
        if invite.projects:
            await MembershipRepository(session).add(user.id, invite.projects)
        invite.status = "accepted"
        invite.accepted_at = datetime.now(timezone.utc)
        session.add(invite)
        await session.commit()
        await c.authz.bump_user(user.id)                        # memberships/active changed
        expires_in = await _issue_session_cookies(c, response, user)
        log_event(log, "auth.invite_accepted", user_id=user.id, client_id=client_id)
        return await _session_out(session, user, expires_in)


@router.get("/me", response_model=SessionOut, response_model_by_alias=True)
async def me(claims: UserClaims = Depends(current_user), session: AsyncSession = Depends(get_session)):
    """Current user, client and projects (used by the UI to pick X-Client-Id / X-Project-Id)."""
    user = await UserRepository(session).get(claims.user_id)
    if user is None or not user.is_active:
        raise AppError(Errors.AUTH_TOKEN_INVALID)
    return await _session_out(session, user)

