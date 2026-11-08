"""Signed, expiring, single-use invite tokens (docs/impl_agent_factory.md section 6b).

A token is `{client_id}.{random}.{sig}` where `sig = b64url(HMAC_SHA256(invite_signing_key, "{client_id}.{random}"))`.
The HMAC lets the server reject a tampered/forged token before any database lookup; the embedded client id lets the
public accept endpoint set the right tenant context (RLS) before it looks the invite up. Only the token's SHA-256 hash
is stored (`user_invites.token_hash`), so the database never holds a usable token; single use + expiry are enforced on
accept (status flips to `accepted`, `expires_at` is checked). The signing key comes from the secret store
(agent_common.secrets) as `invite_signing_key`, fetched lazily so the service starts even when invites are unused.
"""
import base64
import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

from agent_common.secrets import get_secret_provider, require_secret

_RANDOM_BYTES = 32
_SIG_BYTES = 16


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


class InviteService:
    """Creates and verifies invite tokens and builds the invite link."""

    def __init__(self, secret_provider, ttl_hours: int, base_url: str):
        self._secrets = secret_provider
        self._ttl = timedelta(hours=ttl_hours)
        self._base_url = base_url.rstrip("/")
        self._key: bytes | None = None

    @classmethod
    def from_settings(cls, settings) -> "InviteService":
        return cls(get_secret_provider(settings), settings.invite_ttl_hours, settings.invite_base_url)

    def _signing_key(self) -> bytes:
        """The HMAC signing key from the secret store (fetched once). Raises if `invite_signing_key` is not set."""
        if self._key is None:
            self._key = require_secret(self._secrets, "invite_signing_key").encode("utf-8")
        return self._key

    def _sign(self, payload: str) -> str:
        mac = hmac.new(self._signing_key(), payload.encode("ascii"), hashlib.sha256).digest()[:_SIG_BYTES]
        return _b64(mac)

    def create(self, client_id: int) -> tuple[str, str, datetime]:
        """Return (token, token_hash, expires_at) for a new invite into `client_id`."""
        payload = f"{client_id}.{_b64(secrets.token_bytes(_RANDOM_BYTES))}"
        token = f"{payload}.{self._sign(payload)}"
        return token, self.token_hash(token), datetime.now(timezone.utc) + self._ttl

    def verify_signature(self, token: str) -> bool:
        """True when the token is well-formed and its HMAC signature is valid (constant-time); no DB lookup."""
        payload, signature = token.rsplit(".", 1) if token.count(".") >= 2 else ("", "")
        if not payload or not signature:
            return False
        return hmac.compare_digest(signature, self._sign(payload))

    @staticmethod
    def client_id_of(token: str) -> int | None:
        """The client id embedded in a token (for the public accept endpoint to set tenant context), or None."""
        head = token.partition(".")[0]
        return int(head) if head.isdigit() else None

    @staticmethod
    def token_hash(token: str) -> str:
        """SHA-256 hex of the token (what is stored and looked up; the token itself is never stored)."""
        return hashlib.sha256(token.encode("ascii")).hexdigest()

    def build_link(self, token: str) -> str:
        """The invite link the inviter shares (the notifier delivers it; today it is logged/returned)."""
        return f"{self._base_url}?token={token}"
