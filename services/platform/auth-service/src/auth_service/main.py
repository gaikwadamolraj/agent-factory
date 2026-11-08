"""auth-service entry point: `uvicorn auth_service.main:app`. Settings and keys are read once at import."""
import atexit

from agent_common.telemetry import instrument_asgi, setup_observability
from auth_service.app import create_app
from auth_service.config import get_settings

settings = get_settings()
observability = setup_observability(settings)
atexit.register(observability.shutdown)        # flush traces/metrics/logs on shutdown
_fastapi_app = create_app(settings)
app = instrument_asgi(_fastapi_app) if settings.otel_enabled else _fastapi_app
