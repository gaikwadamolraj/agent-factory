"""Compute agent error codes (format DOMAIN-CATEGORY-NNNN; registered in the shared catalog)."""
from agent_common.errors import register


class ComputeErrors:
    INPUT_INVALID = register("CMP-INV-0001", 422, "The code to run is missing, too large, or not Python.",
                             "Send Python code within the documented size limit.")
    SANDBOX_UNAVAILABLE = register("CMP-UNA-0001", 503, "The code sandbox is temporarily unavailable.",
                                   "Retry after 'retryAfter' seconds.", retryable=True)
