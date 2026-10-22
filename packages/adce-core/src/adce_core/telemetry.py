"""OpenTelemetry for every service: spans for agents, model calls and HTTP requests.

Off by default. `setup(service)` turns it on when OTEL_EXPORTER_OTLP_ENDPOINT is set and the `telemetry` extra is
installed (opentelemetry-sdk and the OTLP exporter); otherwise every span is the API's no-op and costs nothing.

- `span(name, **attributes)`: a context manager used around each agent run (`agent.<name>`, in BaseAgent) and each
  model gateway call (`gateway.<route>`). Attributes are ids and counts only, never data.
- `instrument(app)`: a FastAPI middleware that opens one server span per request (`<METHOD> <route>`), with the
  W3C trace context of the caller when it sends one, so a request through the API, kg-mcp and the worker is one trace.
- `inject(headers)`: adds the current trace context to outgoing HTTP headers (the kg-mcp client uses it).

Model calls are also traced in Langfuse (adce_gateway.tracing), with prompts, tokens and cost; OpenTelemetry carries
the timing and the call tree across services.
"""
from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

log = logging.getLogger(__name__)
_SETUP = False


def setup(service: str) -> bool:
    """Configure the tracer provider for this process. Returns whether spans are exported."""
    global _SETUP
    if _SETUP or not os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        return _SETUP
    try:
        from opentelemetry import trace  # pyright: ignore[reportMissingImports]
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (  # pyright: ignore[reportMissingImports]
            OTLPSpanExporter,
        )
        from opentelemetry.sdk.resources import Resource  # pyright: ignore[reportMissingImports]
        from opentelemetry.sdk.trace import TracerProvider  # pyright: ignore[reportMissingImports]
        from opentelemetry.sdk.trace.export import BatchSpanProcessor  # pyright: ignore[reportMissingImports]
    except ImportError:
        log.info("OTEL_EXPORTER_OTLP_ENDPOINT is set but the telemetry extra is not installed (adce-core[telemetry])")
        return False
    provider = TracerProvider(resource=Resource.create({"service.name": service,
                                                        "deployment.environment": os.environ.get("ADCE_ENV", "local")}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)
    _SETUP = True
    return True


def _tracer() -> Any | None:
    try:
        from opentelemetry import trace  # pyright: ignore[reportMissingImports]
    except ImportError:
        return None
    return trace.get_tracer("adce")


@contextmanager
def span(name: str, **attributes: Any) -> Iterator[Any]:
    """A span (no-op without OpenTelemetry). Exceptions are recorded on the span and re-raised."""
    tracer = _tracer()
    if tracer is None:
        yield None
        return
    attrs = {f"adce.{k}": v for k, v in attributes.items() if isinstance(v, (str, int, float, bool))}
    with tracer.start_as_current_span(name, attributes=attrs) as s:
        yield s


def inject(headers: dict[str, str]) -> dict[str, str]:
    try:
        from opentelemetry.propagate import inject as _inject  # pyright: ignore[reportMissingImports]
    except ImportError:
        return headers
    _inject(headers)
    return headers


def instrument(app: Any, service: str) -> None:
    """One server span per request, continuing the caller's trace."""
    setup(service)

    @app.middleware("http")
    async def _otel(request: Any, call_next: Any) -> Any:
        tracer = _tracer()
        if tracer is None:
            return await call_next(request)
        try:
            from opentelemetry.propagate import extract  # pyright: ignore[reportMissingImports]
            from opentelemetry.trace import SpanKind  # pyright: ignore[reportMissingImports]
        except ImportError:
            return await call_next(request)
        with tracer.start_as_current_span(f"{request.method} {request.url.path}", kind=SpanKind.SERVER,
                                          context=extract(dict(request.headers)),
                                          attributes={"http.request.method": request.method,
                                                      "url.path": request.url.path, "service.name": service}) as s:
            response = await call_next(request)
            s.set_attribute("http.response.status_code", response.status_code)
            return response
