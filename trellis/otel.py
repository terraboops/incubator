"""Optional OpenTelemetry instrumentation for Trellis.

When the `otel` extra is installed and OTEL_EXPORTER_OTLP_ENDPOINT is set,
provides real distributed tracing. Otherwise, all operations are no-ops with
zero overhead.

Usage:
    from trellis.otel import get_tracer
    tracer = get_tracer()
    with tracer.start_as_current_span("my.operation") as span:
        span.set_attribute("key", "value")
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from typing import Any

logger = logging.getLogger(__name__)

_tracer = None
_initialized = False


class _NoOpSpan:
    """Span that does nothing — zero overhead when OTEL is not installed."""

    def set_attribute(self, key: str, value: Any) -> None:
        pass

    def set_status(self, status: Any, description: str | None = None) -> None:
        pass

    def add_event(self, name: str, attributes: dict | None = None) -> None:
        pass

    def record_exception(self, exception: Exception) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class _NoOpTracer:
    """Tracer that returns no-op spans — used when OTEL is not available."""

    @contextmanager
    def start_as_current_span(self, name: str, **kwargs):
        yield _NoOpSpan()

    def start_span(self, name: str, **kwargs) -> _NoOpSpan:
        return _NoOpSpan()


def get_tracer(name: str = "trellis") -> Any:
    """Get a tracer instance. Returns a real OTEL tracer or a no-op stub."""
    global _tracer, _initialized

    if _initialized:
        return _tracer

    _initialized = True

    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    if not endpoint:
        _tracer = _NoOpTracer()
        return _tracer

    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        resource = Resource.create(
            {
                "service.name": os.environ.get("OTEL_SERVICE_NAME", "trellis"),
                "service.version": _get_version(),
            }
        )
        provider = TracerProvider(resource=resource)
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        trace.set_tracer_provider(provider)
        _tracer = trace.get_tracer(name)
        logger.info("OTEL tracing enabled → %s", endpoint)
        return _tracer
    except ImportError:
        logger.debug("OTEL packages not installed — tracing disabled")
        _tracer = _NoOpTracer()
        return _tracer
    except Exception as e:
        logger.warning("OTEL initialization failed: %s — tracing disabled", e)
        _tracer = _NoOpTracer()
        return _tracer


def instrument_fastapi(app: Any) -> None:
    """Instrument a FastAPI app with OTEL middleware if available."""
    if not os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        return
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(app)
        logger.info("OTEL FastAPI instrumentation enabled")
    except ImportError:
        pass


def _get_version() -> str:
    try:
        from importlib.metadata import version

        return version("trellis")
    except Exception:
        return "unknown"
