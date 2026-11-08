"""Orchestrator entry point: `uvicorn ats_orchestrator.main:app`. Schema is managed by the schema service (Job)."""
from contextlib import asynccontextmanager

from fastapi import FastAPI

from agent_common.fastapi_errors import install_error_handlers
from agent_common.llm import Purpose, model_for
from agent_common.logging import get_logger, log_event
from agent_common.telemetry import instrument_asgi, setup_observability
from ats_orchestrator.api.v1 import router as v1_router
from ats_orchestrator.config import get_settings
from ats_orchestrator.container import close_container, init_container
from ats_orchestrator.errors import OrchErrors
from ats_orchestrator.middleware import install_middleware

settings = get_settings()
observability = setup_observability(settings)
log = get_logger("ats_orchestrator")
_ = OrchErrors  # register service error codes at import


@asynccontextmanager
async def lifespan(_: FastAPI):
    await init_container(settings)
    log_event(log, "service.started", env=settings.app_env, chat_model=model_for(Purpose.CHAT),
              agent=settings.agent_mcp_url, skill=settings.skill_version or "agent default")
    yield
    await close_container()
    observability.shutdown()


def create_app() -> FastAPI:
    docs = not settings.is_prod  # no OpenAPI/docs in PROD
    application = FastAPI(title="ATS Orchestrator API", version="1.0.0", lifespan=lifespan,
                          docs_url="/docs" if docs else None, redoc_url=None,
                          openapi_url="/openapi.json" if docs else None)
    install_error_handlers(application)
    install_middleware(application, settings)
    application.include_router(v1_router)

    @application.get("/healthz", tags=["health"], include_in_schema=False)
    async def healthz():
        return {"status": "ok"}

    return application


_fastapi_app = create_app()
app = instrument_asgi(_fastapi_app) if settings.otel_enabled else _fastapi_app
