"""The secret-provider interface: one `get_secret(name)` seam, many possible backends.

Config values come from the environment (agent_common.config); *secrets* (database passwords, the service client
secret, the versioned login peppers, future per-client API keys) come through this interface so the storage backend
can change - k8s Secret today, a vault tomorrow - without any caller change.

`get_secret` is synchronous because today's backend reads process environment / mounted files (no I/O round trip),
exactly like pydantic-settings reads config. A future network-backed driver (OpenBao, a cloud secret manager) can add
an async variant; callers that only need the env backend stay synchronous.
"""
from typing import Optional, Protocol, runtime_checkable


@runtime_checkable
class SecretProvider(Protocol):
    """Reads a named secret. Returns None when the secret is not configured (callers decide if that is fatal)."""

    def get_secret(self, name: str) -> Optional[str]:
        """Return the secret's value, or None when it is not present."""
        ...


def require_secret(provider: SecretProvider, name: str) -> str:
    """get_secret(name) but raises RuntimeError when the secret is missing - for secrets a service cannot run without."""
    value = provider.get_secret(name)
    if value is None or value == "":
        raise RuntimeError(f"Required secret '{name}' is not configured in the active secret backend.")
    return value
