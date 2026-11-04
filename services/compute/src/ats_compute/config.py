"""Compute agent settings (environment only; secrets from the k8s Secret)."""
from functools import lru_cache

from ats_common.config import McpServiceSettings


class ComputeSettings(McpServiceSettings):
    service_name: str = "ats-compute"

    # Sandbox (see services/sandbox): cluster-internal, no network, no secrets
    sandbox_url: str = "http://ats-sandbox"
    sandbox_run_timeout_s: float = 10.0       # default wall time of one run inside the sandbox
    sandbox_max_timeout_s: float = 30.0       # callers can ask for more, up to this
    sandbox_breaker_failures: int = 5
    sandbox_breaker_reset_s: float = 30.0

    # Input limits
    max_code_chars: int = 20_000

    tool_policy: str = "ats-orchestrator=execute_code|cancel_request"
    port: int = 8002


@lru_cache
def get_settings() -> ComputeSettings:
    return ComputeSettings()
