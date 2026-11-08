"""The environment secret backend: today's default. In Kubernetes a Secret is mounted either as environment
variables or as files; this provider reads both, matching how agent_common.config reads settings from the environment.

Lookup order for a secret `name` (an optional `prefix` is applied to the env-var forms):
1. `{PREFIX}{NAME}_FILE` points at a file -> the file's trimmed contents (k8s `volumeMounts` of a Secret);
2. `{PREFIX}{NAME}` environment variable -> its value;
3. otherwise None.

Names are upper-cased for the environment lookup (secrets are referenced by logical name, e.g. "database_url").
"""
import os
from pathlib import Path
from typing import Optional


class EnvSecretProvider:
    """Reads secrets from environment variables and `*_FILE`-mounted files (the k8s Secret backend)."""

    def __init__(self, prefix: str = ""):
        self._prefix = prefix.upper()

    def _env_name(self, name: str) -> str:
        """Logical secret name -> environment variable name (uppercase, with the configured prefix)."""
        return f"{self._prefix}{name.upper()}"

    def get_secret(self, name: str) -> Optional[str]:
        """Return the secret from a mounted file first, then the environment variable, else None."""
        env_name = self._env_name(name)
        # A `*_FILE` variable pointing at a readable file wins (k8s mounts a Secret as a file for larger values).
        file_path = os.environ.get(f"{env_name}_FILE")
        if file_path:
            path = Path(file_path)
            if path.is_file():
                return path.read_text(encoding="utf-8").strip()
        value = os.environ.get(env_name)
        return value if value not in (None, "") else None
