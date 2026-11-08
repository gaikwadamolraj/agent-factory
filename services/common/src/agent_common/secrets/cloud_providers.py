"""Future secret backends: OpenBao and the three cloud secret managers. These are deliberate, documented stubs - the
interface and the construction seam exist now, so adding a real backend is one driver plus a `get_secret_provider`
branch, with no caller change (the broader platform vision: a per-client secret backend).

None of these pull in a cloud SDK yet (no new dependencies). Each raises NotImplementedError until implemented; the
docstrings record exactly what the real driver must do.
"""
from typing import Optional


class OpenBaoSecretProvider:
    """OpenBao / HashiCorp Vault KV backend (future).

    Real driver: read the Vault address + token (or k8s auth role) from settings/secret mount, fetch
    `secret/data/<path>/<name>` over HTTPS through agent_common.resilience.resilient_call (timeout, retry, breaker),
    and cache per name with a short TTL. Returns the value or None.
    """

    def __init__(self, *args, **kwargs):
        raise NotImplementedError("OpenBaoSecretProvider is a future backend; set SECRET_BACKEND=env for now.")

    def get_secret(self, name: str) -> Optional[str]:  # pragma: no cover
        raise NotImplementedError


class GcpSecretProvider:
    """Google Cloud Secret Manager backend (future).

    Real driver: resolve `projects/<project>/secrets/<name>/versions/latest` via the Secret Manager API using
    workload-identity credentials; no key material in config. Returns the value or None.
    """

    def __init__(self, *args, **kwargs):
        raise NotImplementedError("GcpSecretProvider is a future backend; set SECRET_BACKEND=env for now.")

    def get_secret(self, name: str) -> Optional[str]:  # pragma: no cover
        raise NotImplementedError


class AwsSecretProvider:
    """AWS Secrets Manager backend (future).

    Real driver: `GetSecretValue` for the secret id via an IAM role (IRSA); supports JSON secrets (one id, many keys).
    Returns the value or None.
    """

    def __init__(self, *args, **kwargs):
        raise NotImplementedError("AwsSecretProvider is a future backend; set SECRET_BACKEND=env for now.")

    def get_secret(self, name: str) -> Optional[str]:  # pragma: no cover
        raise NotImplementedError


class AzureSecretProvider:
    """Azure Key Vault backend (future).

    Real driver: fetch the secret from `https://<vault>.vault.azure.net` using a managed identity
    (DefaultAzureCredential). Returns the value or None.
    """

    def __init__(self, *args, **kwargs):
        raise NotImplementedError("AzureSecretProvider is a future backend; set SECRET_BACKEND=env for now.")

    def get_secret(self, name: str) -> Optional[str]:  # pragma: no cover
        raise NotImplementedError
