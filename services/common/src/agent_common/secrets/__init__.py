"""One `get_secret(name)` interface with a pluggable backend, chosen by config (SECRET_BACKEND).

    from agent_common.secrets import get_secret_provider, require_secret
    secrets = get_secret_provider(settings)
    pepper = require_secret(secrets, "login_pepper_v2")     # raises if missing
    maybe = secrets.get_secret("optional_api_key")          # None if missing

Backends:
* env    - EnvSecretProvider: reads env vars and `*_FILE`-mounted files (the k8s Secret). Real, used today.
* openbao / gcp / aws / azure - documented stubs (cloud_providers.py); no cloud SDK dependency yet.

Adding a real cloud backend is one driver class plus one branch in `get_secret_provider` - no caller change. This is
the seam the broader platform vision needs (a per-client secret backend).
"""
from agent_common.config import CommonSettings
from agent_common.secrets.base import SecretProvider, require_secret
from agent_common.secrets.cloud_providers import (AwsSecretProvider, AzureSecretProvider, GcpSecretProvider,
                                                OpenBaoSecretProvider)
from agent_common.secrets.env_provider import EnvSecretProvider

# Maps a SECRET_BACKEND value to its provider class (env is real; the rest raise until implemented).
_BACKENDS: dict[str, type] = {
    "env": EnvSecretProvider,
    "openbao": OpenBaoSecretProvider,
    "gcp": GcpSecretProvider,
    "aws": AwsSecretProvider,
    "azure": AzureSecretProvider,
}


def get_secret_provider(settings: CommonSettings) -> SecretProvider:
    """Build the secret provider named by settings.secret_backend. Raises ValueError for an unknown backend name."""
    backend = (settings.secret_backend or "env").lower()
    provider_class = _BACKENDS.get(backend)
    if provider_class is None:
        raise ValueError(f"Unknown SECRET_BACKEND '{backend}'; expected one of {sorted(_BACKENDS)}.")
    if provider_class is EnvSecretProvider:
        return EnvSecretProvider(prefix=settings.secret_env_prefix)
    return provider_class()   # future backends build from settings/mounted credentials when implemented


__all__ = [
    "AwsSecretProvider", "AzureSecretProvider", "EnvSecretProvider", "GcpSecretProvider", "OpenBaoSecretProvider",
    "SecretProvider", "get_secret_provider", "require_secret",
]
