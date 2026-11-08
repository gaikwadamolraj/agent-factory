"""Password hashing: bcrypt (per-password salt) plus a versioned, server-side pepper.

A stored hash is `bcrypt(base64(HMAC_SHA256(pepper_vN, password)))`, tagged with the pepper version it used:

    v2:$2b$12$....          # hashed with pepper version 2
    $2b$12$....             # legacy / unpeppered (version 0)

Why a pepper on top of bcrypt's salt: the salt stops precomputation and cross-account comparison, but it lives next
to the hash; a pepper is a secret kept out of the database (agent_common.secrets - k8s Secret today, a vault later), so
a leaked database alone cannot be brute-forced. HMAC-SHA256 first also removes bcrypt's 72-byte truncation of long
passwords. Peppers are versioned so they can be rotated: add `login_pepper_v{N+1}` and set it current; old hashes keep
verifying under their own version and are transparently upgraded to the newest pepper on the next correct login, so a
rotation never locks anyone out.

Secrets: peppers come from the secret store as `login_pepper_v1`, `login_pepper_v2`, ...; the current version is
`LOGIN_PEPPER_CURRENT_VERSION` (0 = no pepper, the default, so existing bcrypt hashes keep working unchanged).

CLI (hash by hand, e.g. the first admin of a new client):
    cd services/platform/auth-service
    uv run python -c "from auth_service.passwords import hash_password; print(hash_password('<password>'))"
"""
import base64
import hashlib
import hmac
from dataclasses import dataclass
from functools import lru_cache

import bcrypt

BCRYPT_ROUNDS = 12
_VERSION_SEP = ":"
NO_PEPPER_VERSION = 0


@dataclass(frozen=True)
class VerifyResult:
    """Outcome of verifying a password: whether it matched and whether the stored hash should be upgraded."""

    ok: bool
    needs_rehash: bool     # True when it matched but under an older pepper version than the current one


class PasswordHasher:
    """Hashes and verifies passwords with a versioned pepper. Thread-safe and cheap to share."""

    def __init__(self, peppers: dict[int, str], current_version: int, rounds: int = BCRYPT_ROUNDS):
        self._peppers = dict(peppers)
        self._current = current_version
        self._rounds = rounds

    def hash(self, password: str) -> str:
        """A fresh hash of `password` under the current pepper version (tagged)."""
        bcrypt_hash = bcrypt.hashpw(self._prepared(password, self._current), bcrypt.gensalt(rounds=self._rounds))
        return self._encode(self._current, bcrypt_hash.decode("utf-8"))

    def verify(self, password: str, stored: str) -> VerifyResult:
        """Verify `password` against a stored hash. Never raises: a malformed hash or an unknown pepper version
        reports ok=False. `needs_rehash` is True on a match under a non-current pepper version."""
        try:
            version, bcrypt_hash = self._decode(stored)
            if version != NO_PEPPER_VERSION and version not in self._peppers:
                return VerifyResult(False, False)       # pepper for that version is not configured: cannot verify
            matches = bcrypt.checkpw(self._prepared(password, version), bcrypt_hash.encode("utf-8"))
        except (ValueError, KeyError):
            return VerifyResult(False, False)
        return VerifyResult(matches, matches and version != self._current)

    # ----------------------------------------------------------------- internals
    def _prepared(self, password: str, version: int) -> bytes:
        """The bytes actually fed to bcrypt: the plain password for version 0, else base64(HMAC(pepper, password))."""
        if version == NO_PEPPER_VERSION:
            return password.encode("utf-8")[:72]        # bcrypt truncates at 72 bytes; match legacy behaviour
        pepper = self._peppers[version]
        mac = hmac.new(pepper.encode("utf-8"), password.encode("utf-8"), hashlib.sha256).digest()
        return base64.b64encode(mac)                     # 44 bytes, well under bcrypt's 72-byte limit

    @staticmethod
    def _encode(version: int, bcrypt_hash: str) -> str:
        return bcrypt_hash if version == NO_PEPPER_VERSION else f"v{version}{_VERSION_SEP}{bcrypt_hash}"

    @staticmethod
    def _decode(stored: str) -> tuple[int, str]:
        """(version, bcrypt hash) of a stored value. An untagged value (starts with '$') is version 0 (legacy)."""
        if stored.startswith("v") and _VERSION_SEP in stored:
            tag, _, rest = stored.partition(_VERSION_SEP)
            return int(tag[1:]), rest
        return NO_PEPPER_VERSION, stored


def build_password_hasher(settings) -> PasswordHasher:
    """Build the hasher from settings + the secret store. Loads every pepper up to the current version and fails fast
    when the current pepper is missing (so a misconfiguration cannot silently fall back to no pepper)."""
    from agent_common.secrets import get_secret_provider

    provider = get_secret_provider(settings)
    current = int(getattr(settings, "login_pepper_current_version", 0) or 0)
    peppers: dict[int, str] = {}
    for version in range(1, current + 1):
        secret = provider.get_secret(f"login_pepper_v{version}")
        if secret:
            peppers[version] = secret
    if current > NO_PEPPER_VERSION and current not in peppers:
        raise RuntimeError(f"LOGIN_PEPPER_CURRENT_VERSION={current} but secret 'login_pepper_v{current}' is not set.")
    return PasswordHasher(peppers, current)


@lru_cache
def _default_hasher() -> PasswordHasher:
    """The process-wide hasher from the service settings (for the module-level helpers and the CLI)."""
    from auth_service.config import get_settings

    return build_password_hasher(get_settings())


def hash_password(password: str) -> str:
    """bcrypt+pepper hash of `password` under the current pepper version."""
    return _default_hasher().hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """Does `password` match the stored hash? False for a malformed hash (never raises)."""
    return _default_hasher().verify(password, password_hash).ok
