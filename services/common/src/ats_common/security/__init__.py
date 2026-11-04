"""Passwords (bcrypt) and JWTs.

Three token kinds, never interchangeable (different audience):
* access tokens  aud=ats-api      short-lived, issued at login/refresh: sub=user id, cid=client id, role, username
* refresh tokens aud=ats-refresh  long-lived, rotated on every use, allow-listed in Redis by jti (revocable)
* service tokens aud=ats-agent    minted per call by a calling service: sub=service name (zero trust)
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import bcrypt
import jwt

import uuid

from ats_common.constants import JWT_AUDIENCE_REFRESH, JWT_AUDIENCE_USER, JWT_ISSUER
from ats_common.errors import AppError, Errors

SERVICE_TOKEN_TTL_SECONDS = 300


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except ValueError:
        return False


@dataclass(frozen=True)
class UserClaims:
    user_id: int
    client_id: int
    role: str
    username: str


def create_user_token(secret: str, claims: UserClaims, expires_minutes: int, algorithm: str = "HS256") -> tuple[str, int]:
    """Returns (token, expires_in_seconds)."""
    now = datetime.now(timezone.utc)
    payload = {"iss": JWT_ISSUER, "aud": JWT_AUDIENCE_USER, "sub": str(claims.user_id), "cid": claims.client_id,
               "role": claims.role, "username": claims.username, "iat": now,
               "exp": now + timedelta(minutes=expires_minutes)}
    return jwt.encode(payload, secret, algorithm=algorithm), expires_minutes * 60


def decode_user_token(secret: str, token: str, algorithm: str = "HS256") -> UserClaims:
    """Raises AppError (AUTH-EXP-0001 / AUTH-INV-0001)."""
    try:
        data = jwt.decode(token, secret, algorithms=[algorithm], audience=JWT_AUDIENCE_USER, issuer=JWT_ISSUER,
                          options={"require": ["sub", "exp", "aud", "iss", "cid"]})
        return UserClaims(int(data["sub"]), int(data["cid"]), str(data.get("role", "")), str(data.get("username", "")))
    except jwt.ExpiredSignatureError:
        raise AppError(Errors.AUTH_TOKEN_EXPIRED)
    except (jwt.PyJWTError, ValueError, KeyError):
        raise AppError(Errors.AUTH_TOKEN_INVALID)


def create_service_token(secret: str, caller: str, audience: str, algorithm: str = "HS256",
                         ttl_seconds: int = SERVICE_TOKEN_TTL_SECONDS, on_behalf_of: Optional[int] = None) -> str:
    """Short-lived token a service sends to another service."""
    now = datetime.now(timezone.utc)
    payload = {"iss": JWT_ISSUER, "aud": audience, "sub": caller, "kind": "service", "iat": now,
               "exp": now + timedelta(seconds=ttl_seconds)}
    if on_behalf_of is not None:
        payload["obo"] = on_behalf_of
    return jwt.encode(payload, secret, algorithm=algorithm)


def decode_service_token(secret: str, token: str, audience: str, algorithm: str = "HS256") -> str:
    """Returns the caller service name. Raises AppError for invalid/expired tokens or user tokens."""
    try:
        data = jwt.decode(token, secret, algorithms=[algorithm], audience=audience, issuer=JWT_ISSUER,
                          options={"require": ["sub", "exp", "aud", "iss"]})
    except jwt.ExpiredSignatureError:
        raise AppError(Errors.AUTH_TOKEN_EXPIRED)
    except jwt.PyJWTError:
        raise AppError(Errors.AUTH_TOKEN_INVALID)
    if data.get("kind") != "service":
        raise AppError(Errors.AUTH_TOKEN_INVALID)
    return str(data["sub"])


@dataclass(frozen=True)
class RefreshClaims:
    user_id: int
    jti: str


def create_refresh_token(secret: str, user_id: int, expires_days: int, algorithm: str = "HS256") -> tuple[str, str, int]:
    """Returns (token, jti, expires_in_seconds). Store the jti in the allow-list; rotate it on every refresh."""
    now = datetime.now(timezone.utc)
    jti = uuid.uuid4().hex
    expires_in = expires_days * 24 * 3600
    payload = {"iss": JWT_ISSUER, "aud": JWT_AUDIENCE_REFRESH, "sub": str(user_id), "jti": jti, "iat": now,
               "exp": now + timedelta(seconds=expires_in)}
    return jwt.encode(payload, secret, algorithm=algorithm), jti, expires_in


def decode_refresh_token(secret: str, token: str, algorithm: str = "HS256") -> RefreshClaims:
    """Raises AppError (AUTH-EXP-0001 / AUTH-INV-0001)."""
    try:
        data = jwt.decode(token, secret, algorithms=[algorithm], audience=JWT_AUDIENCE_REFRESH, issuer=JWT_ISSUER,
                          options={"require": ["sub", "exp", "jti"]})
        return RefreshClaims(int(data["sub"]), str(data["jti"]))
    except jwt.ExpiredSignatureError:
        raise AppError(Errors.AUTH_TOKEN_EXPIRED)
    except (jwt.PyJWTError, ValueError, KeyError):
        raise AppError(Errors.AUTH_TOKEN_INVALID)
