"""Error catalog and error responses.

Code format: {DOMAIN}-{CATEGORY}-{NNNN}, e.g. WFL-TMO-0002. Codes reveal nothing about internal services,
functions or stack; the meaning lives only in this catalog. Every code has an HTTP status, a message a user or an
agent can act on, a "what to do next" hint and whether retrying makes sense.

DOMAIN   AUTH auth · USR user management · TEN tenant · VAL validation · CHAT chats · FILE uploads ·
         SCR screening · AGT agent · CMP compute ·
         WFL workflow/turn · LLM model · DB database · SYS system
CATEGORY MIS missing · INV invalid · EXP expired · FBD forbidden · NFD not found · CNF conflict · LIM limit ·
         TMO timeout · RTL rate limited · UNA unavailable · CAN cancelled · INT internal

Services add their own codes with `register(...)`; numbers are unique per DOMAIN-CATEGORY.
"""
import json
import re
from dataclasses import dataclass
from typing import Any, Optional

from ats_common.constants import HEADER_RETRY_AFTER
from ats_common.context import current_context

_CODE_PATTERN = re.compile(r"^[A-Z]{2,5}-[A-Z]{3}-\d{4}$")


@dataclass(frozen=True)
class ErrorSpec:
    code: str
    status: int
    message: str          # what happened, safe to show to users and agents
    hint: str             # what to do next
    retryable: bool = False


_CATALOG: dict[str, ErrorSpec] = {}


def register(code: str, status: int, message: str, hint: str, retryable: bool = False) -> ErrorSpec:
    """Add a code to the catalog (idempotent for identical specs; conflicting duplicates raise)."""
    if not _CODE_PATTERN.match(code):
        raise ValueError(f"Invalid error code format: {code} (expected DOMAIN-CAT-NNNN)")
    spec = ErrorSpec(code, status, message, hint, retryable)
    existing = _CATALOG.get(code)
    if existing and existing != spec:
        raise ValueError(f"Error code {code} is already registered with a different meaning")
    _CATALOG[code] = spec
    return spec


def get_spec(code: str) -> Optional[ErrorSpec]:
    return _CATALOG.get(code)


def catalog() -> dict[str, ErrorSpec]:
    return dict(_CATALOG)


class AppError(Exception):
    """Raise anywhere; converted to a safe JSON error response by the error handlers / middleware."""

    def __init__(self, spec: ErrorSpec, detail: Optional[str] = None, retry_after: Optional[int] = None,
                 fields: Optional[list[dict]] = None):
        super().__init__(f"{spec.code}: {detail or spec.message}")
        self.spec, self.detail, self.retry_after, self.fields = spec, detail, retry_after, fields


def error_body(spec: ErrorSpec, detail: Optional[str] = None, retry_after: Optional[int] = None,
               fields: Optional[list[dict]] = None) -> dict[str, Any]:
    """Response envelope (camelCase): {"error": {code, message, hint, retryable, retryAfter, requestId, fields}}."""
    body: dict[str, Any] = {
        "code": spec.code,
        "message": detail or spec.message,
        "hint": spec.hint,
        "retryable": spec.retryable,
        "requestId": current_context().request_id or None,
    }
    if retry_after is not None:
        body["retryAfter"] = retry_after
    if fields:
        body["fields"] = fields
    return {"error": body}


def error_headers(retry_after: Optional[int]) -> dict[str, str]:
    return {HEADER_RETRY_AFTER: str(retry_after)} if retry_after is not None else {}


def app_error_body(err: AppError) -> dict[str, Any]:
    return error_body(err.spec, err.detail, err.retry_after, err.fields)


async def send_error(send, spec: ErrorSpec, detail: Optional[str] = None, retry_after: Optional[int] = None,
                     extra_headers: Optional[dict[str, str]] = None) -> None:
    """Write an error response from pure-ASGI middleware (same shape as the FastAPI handlers)."""
    payload = json.dumps(error_body(spec, detail, retry_after)).encode()
    headers = {"content-type": "application/json", "content-length": str(len(payload)),
               **error_headers(retry_after), **(extra_headers or {})}
    await send({"type": "http.response.start", "status": spec.status,
                "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()]})
    await send({"type": "http.response.body", "body": payload})


# ---------------------------------------------------------------------------------------------- common codes
class Errors:
    """Codes shared by all services. Service-specific codes live in each service's `errors.py`."""

    AUTH_TOKEN_MISSING = register("AUTH-MIS-0001", 401, "Sign-in required.",
                                  "Send 'Authorization: Bearer <token>' obtained from the login endpoint.")
    AUTH_TOKEN_INVALID = register("AUTH-INV-0001", 401, "Your session token is not valid.", "Sign in again.")
    AUTH_TOKEN_EXPIRED = register("AUTH-EXP-0001", 401, "Your session has expired.", "Sign in again.")
    AUTH_CREDENTIALS = register("AUTH-INV-0002", 401, "Username or password is incorrect.",
                                "Check your credentials and try again.")
    AUTH_FORBIDDEN = register("AUTH-FBD-0001", 403, "You do not have permission for this action.",
                              "Ask an administrator of your client for access.")
    SERVICE_FORBIDDEN = register("AUTH-FBD-0002", 403, "This caller is not allowed to use this operation.",
                                 "Check the tool policy for the calling service.")

    TENANT_HEADERS_MISSING = register("TEN-MIS-0001", 400, "Client and project are required.",
                                      "Send the X-Client-Id and X-Project-Id headers.")
    TENANT_FORBIDDEN = register("TEN-FBD-0001", 403, "You do not have access to this client or project.",
                                "Pick a project you are a member of.")

    VALIDATION_FAILED = register("VAL-INV-0001", 422, "Some fields are missing or invalid.",
                                 "Fix the fields listed in 'fields' and send the request again.")
    PAYLOAD_TOO_LARGE = register("VAL-LIM-0001", 413, "The request is too large.", "Send a smaller payload.")
    NOT_FOUND = register("SYS-NFD-0001", 404, "The requested resource was not found.",
                         "Check the identifier, or refresh the list.")
    RATE_LIMITED = register("SYS-RTL-0001", 429, "Too many requests.",
                            "Wait for the number of seconds in 'retryAfter', then retry.", retryable=True)
    UPSTREAM_TIMEOUT = register("SYS-TMO-0001", 504, "A dependent service took too long to answer.",
                                "Retry in a moment; if it keeps happening, contact support.", retryable=True)
    UPSTREAM_UNAVAILABLE = register("SYS-UNA-0001", 503, "A dependent service is temporarily unavailable.",
                                    "Retry after 'retryAfter' seconds.", retryable=True)
    LLM_TIMEOUT = register("LLM-TMO-0001", 504, "The language model took too long to answer.",
                           "Retry in a moment; long inputs or a busy model take longer.", retryable=True)
    LLM_UNAVAILABLE = register("LLM-UNA-0001", 503, "The language model is temporarily unavailable.",
                               "Retry after 'retryAfter' seconds.", retryable=True)
    LLM_MALFORMED = register("LLM-INV-0001", 502, "The language model returned an answer in an unexpected format.",
                             "Send the question again; rephrasing it often helps.", retryable=True)
    INTERNAL = register("SYS-INT-0001", 500, "Something went wrong on our side.",
                        "Retry once; if it fails again, contact support with the requestId.", retryable=True)
