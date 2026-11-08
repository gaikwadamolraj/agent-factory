"""FastAPI exception handlers: every error leaves the service as the catalog envelope, never with a stack trace."""
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic.alias_generators import to_camel
from starlette.exceptions import HTTPException as StarletteHTTPException

from agent_common.errors import AppError, Errors, app_error_body, error_body, error_headers
from agent_common.logging import get_logger

log = get_logger("ats.errors")

_STATUS_TO_SPEC = {401: Errors.AUTH_TOKEN_INVALID, 403: Errors.AUTH_FORBIDDEN, 404: Errors.NOT_FOUND,
                   413: Errors.PAYLOAD_TOO_LARGE, 429: Errors.RATE_LIMITED}


def _field_errors(exc: RequestValidationError) -> list[dict]:
    fields = []
    for err in exc.errors():
        loc = [to_camel(str(p)) for p in err.get("loc", ()) if p not in ("body", "query", "path", "header")]
        fields.append({"field": ".".join(loc) or "body", "issue": err.get("msg", "invalid")})
    return fields


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError):
        level = log.error if exc.spec.status >= 500 else log.info
        level("request.failed", extra={"code": exc.spec.code, "status": exc.spec.status})
        return JSONResponse(app_error_body(exc), status_code=exc.spec.status, headers=error_headers(exc.retry_after))

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError):
        return JSONResponse(error_body(Errors.VALIDATION_FAILED, fields=_field_errors(exc)),
                            status_code=Errors.VALIDATION_FAILED.status)

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, exc: StarletteHTTPException):
        spec = _STATUS_TO_SPEC.get(exc.status_code, Errors.NOT_FOUND if exc.status_code == 405 else Errors.INTERNAL)
        return JSONResponse(error_body(spec), status_code=exc.status_code if exc.status_code < 500 else spec.status)

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception):
        log.exception("request.unhandled_error")
        return JSONResponse(error_body(Errors.INTERNAL), status_code=Errors.INTERNAL.status)
