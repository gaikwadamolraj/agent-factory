"""OpenTelemetry setup: traces, metrics and logs exported over OTLP/HTTP (Grafana LGTM: Tempo, Prometheus, Loki).

Call `setup_observability(settings)` once at startup; it configures logging too. With `otel_enabled=False` only
console logging is configured and the OTEL API stays a no-op.
"""
import logging
from dataclasses import dataclass
from typing import Optional

from opentelemetry import metrics, trace
from opentelemetry.sdk.resources import Resource

from ats_common.config import CommonSettings
from ats_common.constants import HEALTH_PATHS
from ats_common.logging import get_logger, setup_logging, shutdown_logging

log = get_logger(__name__)


@dataclass
class Observability:
    tracer_provider: object = None
    meter_provider: object = None
    logger_provider: object = None

    def shutdown(self) -> None:
        for provider in (self.tracer_provider, self.meter_provider, self.logger_provider):
            if provider is not None:
                try:
                    provider.shutdown()
                except Exception:  # never fail shutdown because of telemetry
                    pass
        shutdown_logging()


def setup_observability(settings: CommonSettings) -> Observability:
    """Configure logging (always) and OTEL providers + auto-instrumentation (when enabled)."""
    if not settings.otel_enabled:
        setup_logging(settings.service_name, settings.app_env, settings.log_level, settings.log_json)
        return Observability()

    from opentelemetry._logs import set_logger_provider
    from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
    from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
    from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    endpoint = settings.otel_exporter_otlp_endpoint.rstrip("/")
    resource = Resource.create({
        "service.name": settings.service_name,
        "service.version": settings.service_version,
        "deployment.environment": str(settings.app_env),
    })

    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{endpoint}/v1/traces")))
    trace.set_tracer_provider(tracer_provider)

    meter_provider = MeterProvider(resource=resource, metric_readers=[PeriodicExportingMetricReader(
        OTLPMetricExporter(endpoint=f"{endpoint}/v1/metrics"), export_interval_millis=settings.otel_export_interval_ms)])
    metrics.set_meter_provider(meter_provider)

    logger_provider = LoggerProvider(resource=resource)
    logger_provider.add_log_record_processor(BatchLogRecordProcessor(OTLPLogExporter(endpoint=f"{endpoint}/v1/logs")))
    set_logger_provider(logger_provider)
    otel_handler = LoggingHandler(level=logging.INFO, logger_provider=logger_provider)

    setup_logging(settings.service_name, settings.app_env, settings.log_level, settings.log_json, [otel_handler])
    _instrument_clients()
    log.info("telemetry.enabled", extra={"otlp_endpoint": endpoint})
    return Observability(tracer_provider, meter_provider, logger_provider)


def _instrument_clients() -> None:
    """Auto-instrument outgoing HTTP, SQLAlchemy and Redis calls (spans + context propagation)."""
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.instrumentation.redis import RedisInstrumentor
    from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

    HTTPXClientInstrumentor().instrument()
    RedisInstrumentor().instrument()
    SQLAlchemyInstrumentor().instrument(enable_commenter=False)


def instrument_asgi(app, excluded_paths: Optional[list[str]] = None):
    """Wrap an ASGI app so every request becomes a server span (health checks excluded)."""
    from opentelemetry.instrumentation.asgi import OpenTelemetryMiddleware

    excluded = ",".join(excluded_paths or sorted(HEALTH_PATHS))
    return OpenTelemetryMiddleware(app, excluded_urls=excluded)


def get_tracer(name: str):
    return trace.get_tracer(name)


def get_meter(name: str):
    return metrics.get_meter(name)
