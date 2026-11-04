"""Agent error codes (format DOMAIN-CATEGORY-NNNN; registered in the shared catalog). LLM codes are shared."""
from ats_common.errors import Errors, register


class AgentErrors:
    INPUT_INVALID = register("AGT-INV-0001", 422, "The screening request is missing data or is too large.",
                             "Send a job description and resume text within the documented limits.")
    REQUEST_CANCELLED = register("AGT-CAN-0001", 499, "The request was cancelled.",
                                 "Start a new request if you still need a result.")
    STEP_FAILED = register("AGT-INT-0001", 500, "A screening step could not be completed.",
                           "Retry the screening; if it fails again, contact support with the requestId.",
                           retryable=True)
    LLM_TIMEOUT = Errors.LLM_TIMEOUT
    LLM_UNAVAILABLE = Errors.LLM_UNAVAILABLE
