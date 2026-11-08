"""Service clients of the client_credentials grant: who they are, their secrets, which services they may call.

Secrets: each calling service has its own random secret in ats-platform-secrets (`client-secret-<service>`); the
calling service and the auth-service read the SAME key of the same Secret, so there is nothing to keep in sync.
Storing hashes in the Secret instead would add a manual hashing step and protect nothing (whoever can read the
Secret also reads the plain value next to it). In memory the auth-service keeps only SHA-256 digests and compares
digests in constant time (`hmac.compare_digest`), also for unknown client ids, so timing reveals nothing.
"""
import hashlib
import hmac

from auth_service.config import AuthSettings

# Digest compared against when the client id is unknown, so that path costs the same as a wrong secret.
_UNKNOWN_CLIENT_DIGEST = hashlib.sha256(b"unknown-client").digest()


def _digest(secret: str) -> bytes:
    return hashlib.sha256(secret.encode("utf-8")).digest()


class ServiceClients:
    """Client registry: authenticate(client_id, secret) and may_call(client_id, audience)."""

    def __init__(self, audiences: dict[str, set[str]], secrets: dict[str, str]):
        self._audiences = audiences
        self._digests = {client_id: _digest(secret) for client_id, secret in secrets.items()}

    @classmethod
    def from_settings(cls, settings: AuthSettings) -> "ServiceClients":
        """Policy from OAUTH_CLIENT_AUDIENCES, secrets from CLIENT_SECRET_<CLIENT>."""
        return cls(settings.client_audiences(), settings.client_secrets())

    @property
    def client_ids(self) -> list[str]:
        """Clients that can authenticate (listed in the policy and with a secret)."""
        return sorted(self._digests)

    def authenticate(self, client_id: str, client_secret: str) -> bool:
        """Is `client_secret` the secret of `client_id`? Constant-time; False for unknown clients."""
        expected = self._digests.get(client_id)
        matches = hmac.compare_digest(_digest(client_secret), expected or _UNKNOWN_CLIENT_DIGEST)
        return matches and expected is not None

    def may_call(self, client_id: str, audience: str) -> bool:
        """Is `client_id` allowed to get tokens for `audience` (OAUTH_CLIENT_AUDIENCES)?"""
        return audience in self._audiences.get(client_id, set())
