"""OpenTelemetry traces: stored in the app database (tracestore) and sent to OTEL_EXPORTER_OTLP_ENDPOINT if set.

One trace per chat turn (`chat.turn`) and per bot run (`bot.run`): pydantic-ai agent, model and tool spans →
httpx calls to agentgateway, which continues the trace from the `traceparent` header (its spans join it).
Every span carries the acting user, session, bot and run. Prompts, answers and tool results are in the
spans (redacted, capped) unless TRACE_CONTENT=false.
"""

import os

from opentelemetry import trace

tracer = trace.get_tracer("agentic-bots")
_enabled = False


def setup() -> bool:
    """Call before any httpx client is created (agents build their provider clients at import)."""
    global _enabled
    from app.config import get_settings

    settings = get_settings()
    endpoint = settings.otel_exporter_otlp_endpoint
    if _enabled or not (endpoint or settings.trace_store):
        return _enabled
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.httpx import HTTPX2ClientInstrumentor, HTTPXClientInstrumentor
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from pydantic_ai import Agent, InstrumentationSettings

    from app import tracestore

    provider = TracerProvider(resource=Resource.create({
        "service.name": os.environ.get("OTEL_SERVICE_NAME", "agentic-bots-backend")}))
    provider.add_span_processor(tracestore.AttributionProcessor())  # first: the exporters see the attributes
    if settings.trace_store:
        provider.add_span_processor(BatchSpanProcessor(tracestore.DbSpanExporter(), schedule_delay_millis=1000))
    if endpoint:
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{endpoint.rstrip('/')}/v1/traces")))
    trace.set_tracer_provider(provider)
    content = settings.trace_content
    Agent.instrument_all(InstrumentationSettings(include_content=content, include_binary_content=False))
    HTTPXClientInstrumentor().instrument()
    HTTPX2ClientInstrumentor().instrument()  # pydantic-ai, openai and mcp use httpx2
    _enabled = True
    return True


def instrument_app(app) -> None:
    if _enabled:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        # Inbound requests are traced for A2A only: chat turns and runs open their own root spans, and the UI's
        # polling of /api/ would bury them. /otlp/ is the gateway sending its spans.
        FastAPIInstrumentor.instrument_app(app, excluded_urls="/api/,/otlp/")


def flush() -> None:
    """Export pending spans now (tests, shutdown)."""
    provider = trace.get_tracer_provider()
    if hasattr(provider, "force_flush"):
        provider.force_flush()


def trace_id() -> str | None:
    """Hex id of the current trace, or None when tracing is off."""
    ctx = trace.get_current_span().get_span_context()
    return format(ctx.trace_id, "032x") if ctx.is_valid else None
