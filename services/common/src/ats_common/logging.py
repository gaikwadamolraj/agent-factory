"""Structured, non-blocking logging with OpenTelemetry export.

* Records are enqueued (QueueHandler) and written by a background QueueListener, so request handlers never block
  on log I/O.
* Output is JSON (one object per line) with request context (request_id, user_id, client_id, project_id, ...).
* When telemetry is enabled the same records are exported over OTLP (Loki in Grafana).

Usage:
    log = get_logger(__name__)
    log_event(log, "turn.started", chat_id=chat.id, action="screen")
"""
import json
import logging
import logging.handlers
import queue
from datetime import datetime, timezone
from typing import Any, Optional

from ats_common.constants import HEALTH_PATHS, MAX_LOG_FIELD_LENGTH
from ats_common.context import current_context

_RESERVED = set(logging.makeLogRecord({}).__dict__) | {"message", "asctime"}
_listener: Optional[logging.handlers.QueueListener] = None


class ContextFilter(logging.Filter):
    """Copies the request context (request_id, user, tenant, chat, turn) onto every record as plain attributes, so
    console output and OTEL log attributes (Loki structured metadata) both carry them."""

    def filter(self, record: logging.LogRecord) -> bool:
        for key, value in current_context().as_log_fields().items():
            if not hasattr(record, key):
                setattr(record, key, value)
        return True


class HealthCheckFilter(logging.Filter):
    """Drops access-log lines for health checks (uvicorn args: client, method, path, http version, status)."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args if isinstance(record.args, tuple) else ()
        path = str(args[2]).split("?", 1)[0] if len(args) > 2 else ""
        return path not in HEALTH_PATHS


def _trim(value: Any) -> Any:
    if isinstance(value, str) and len(value) > MAX_LOG_FIELD_LENGTH:
        return value[:MAX_LOG_FIELD_LENGTH] + "...(truncated)"
    return value


class JsonFormatter(logging.Formatter):
    def __init__(self, service_name: str, app_env: str):
        super().__init__()
        self.static = {"service": service_name, "env": app_env}

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
            **self.static,
            **{k: _trim(v) for k, v in record.__dict__.items() if k not in _RESERVED},
        }
        if record.exc_info:
            payload["error_type"] = record.exc_info[0].__name__ if record.exc_info[0] else None
            payload["stack"] = self.formatException(record.exc_info)  # logs only, never API responses
        return json.dumps(payload, default=str, ensure_ascii=False)


class TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        extra = " ".join(f"{k}={_trim(v)}" for k, v in record.__dict__.items() if k not in _RESERVED)
        base = f"{self.formatTime(record)} {record.levelname:<5} {record.name}: {record.getMessage()}"
        text = " | ".join(p for p in (base, extra) if p)
        return text + ("\n" + self.formatException(record.exc_info) if record.exc_info else "")


def setup_logging(service_name: str, app_env: str, level: str = "INFO", json_output: bool = True,
                  extra_handlers: Optional[list[logging.Handler]] = None) -> None:
    """Configure the root logger once per process. Safe to call again (replaces handlers)."""
    global _listener
    if _listener is not None:
        _listener.stop()

    console = logging.StreamHandler()
    console.setFormatter(JsonFormatter(service_name, app_env) if json_output else TextFormatter())
    handlers = [console, *(extra_handlers or [])]

    log_queue: queue.Queue = queue.Queue(maxsize=10_000)
    queue_handler = logging.handlers.QueueHandler(log_queue)
    queue_handler.addFilter(ContextFilter())  # context must be captured on the calling thread

    root = logging.getLogger()
    root.handlers[:] = [queue_handler]
    root.setLevel(level.upper())
    access = logging.getLogger("uvicorn.access")   # only active when uvicorn runs without --no-access-log
    if not any(isinstance(f, HealthCheckFilter) for f in access.filters):
        access.addFilter(HealthCheckFilter())
    for noisy in ("httpx", "httpcore", "urllib3", "sqlalchemy.engine", "openai", "mcp.server.streamable_http"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _listener = logging.handlers.QueueListener(log_queue, *handlers, respect_handler_level=True)
    _listener.start()


def shutdown_logging() -> None:
    """Flush queued records (call on shutdown)."""
    global _listener
    if _listener is not None:
        _listener.stop()
        _listener = None


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def _primitive(value: Any) -> Any:
    """OTEL log attributes must be primitives (or homogeneous lists of them)."""
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, (list, tuple, set)):
        return ",".join(str(v) for v in value)
    return json.dumps(value, default=str)[:MAX_LOG_FIELD_LENGTH]


def log_event(logger: logging.Logger, event: str, level: int = logging.INFO, **fields: Any) -> None:
    """Log a named event with structured fields, e.g. log_event(log, "screening.completed", score=72.5)."""
    logger.log(level, event, extra={k: _primitive(v) for k, v in fields.items() if k not in _RESERVED})
