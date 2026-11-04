"""Request-scoped context (request id, user, tenant, chat, turn) shared by logging, telemetry and error responses.

Middleware sets the values once per request; every log record and span picks them up automatically.
"""
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class RequestContext:
    request_id: str = ""
    user_id: Optional[int] = None
    username: str = ""
    role: str = ""
    client_id: Optional[int] = None
    project_id: Optional[int] = None
    caller_service: str = ""           # set for service-to-service calls
    extra: dict = field(default_factory=dict)  # e.g. chat_id, turn_id

    def as_log_fields(self) -> dict:
        fields = {
            "request_id": self.request_id or None,
            "user_id": self.user_id,
            "username": self.username or None,
            "client_id": self.client_id,
            "project_id": self.project_id,
            "caller_service": self.caller_service or None,
            **self.extra,
        }
        return {k: v for k, v in fields.items() if v not in (None, "")}


_current: ContextVar[RequestContext] = ContextVar("ats_request_context", default=RequestContext())


def current_context() -> RequestContext:
    return _current.get()


def set_context(ctx: RequestContext):
    """Returns a token for `reset_context`."""
    return _current.set(ctx)


def reset_context(token) -> None:
    _current.reset(token)


def bind(**extra) -> None:
    """Attach extra fields (chat_id, turn_id, ...) to the current request context."""
    ctx = _current.get()
    _current.set(RequestContext(**{**ctx.__dict__, "extra": {**ctx.extra, **extra}}))
