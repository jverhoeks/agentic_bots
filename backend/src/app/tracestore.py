"""Traces in the app database: every chat turn, bot run, tool call, MCP and A2A request, per user and session.

Two sources write here, through one serialised `store()`:
- the backend's own spans (`DbSpanExporter`, added to the tracer provider in `telemetry.setup`);
- the gateway's spans, which agentgateway exports over OTLP/HTTP to `POST /otlp/v1/traces` (`router`).
  That endpoint is only reachable inside the compose network (the backend publishes no port and nginx
  proxies only /api/), and it needs the shared `x-trace-token` (TRACE_INGEST_TOKEN): other containers on that
  network, such as the connectors, cannot add spans in some user's name. Without a token set it refuses all.
  Received spans are forwarded to OTEL_EXPORTER_OTLP_ENDPOINT (a collector) when it is set.

`AttributionProcessor` stamps the acting user, session, bot and run (the audit scope) on every span when it
starts, so LLM, tool and HTTP spans can be filtered per user and session. The `Trace` table summarises each
trace for the lists; spans load on demand. String attributes are redacted (guardrails) and capped.
Per model request, pydantic-ai repeats the whole conversation in `gen_ai.input.messages`: that is dropped
(it grows quadratically and the session history already holds it); the prompt, each answer and every tool
call with its arguments and result stay.
"""

import hmac
import json
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence

from fastapi import APIRouter, Request, Response
from fastapi.concurrency import run_in_threadpool
from opentelemetry.sdk.trace import ReadableSpan, SpanProcessor
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.trace import SpanKind, StatusCode
from sqlalchemy.exc import IntegrityError
from sqlmodel import delete, select

from app import events, guardrails
from app.config import get_settings
from app.db import db
from app.models import Trace, TraceSpan

log = logging.getLogger("app.tracestore")
_lock = threading.Lock()
_last_prune = 0.0

DROP = {"gen_ai.input.messages", "pydantic_ai.all_messages"}
# audit scope key -> span attribute (the gateway sets `user` and `bot` from the JWT)
SCOPE_ATTRS = {"user": "user", "session_id": "session.id", "bot": "bot", "run_id": "run.id", "actor": "actor"}
_KINDS = {1: "internal", 2: "server", 3: "client", 4: "producer", 5: "consumer"}
_STATUS = {0: "unset", 1: "ok", 2: "error"}


class AttributionProcessor(SpanProcessor):
    """Copy the audit scope (user, session, bot, run, actor) onto every span as it starts."""

    def on_start(self, span, parent_context=None) -> None:
        from app import audit

        have = span.attributes or {}
        for key, attr in SCOPE_ATTRS.items():
            if (value := audit.current().get(key)) and attr not in have:
                span.set_attribute(attr, value)


class DbSpanExporter(SpanExporter):
    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        try:
            store([_from_sdk(s) for s in spans])
        except Exception:  # noqa: BLE001  tracing must never break the app
            log.exception("storing %d spans failed", len(spans))
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        pass


# --- conversion -------------------------------------------------------------------------------------------

def _ts(nanos: int) -> datetime:
    return datetime.fromtimestamp(nanos / 1e9, timezone.utc)


def _utc(dt: datetime) -> datetime:
    """SQLite hands datetimes back without a zone: they are UTC."""
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _from_sdk(s: ReadableSpan) -> dict:
    ctx, parent = s.context, s.parent
    return {
        "trace_id": format(ctx.trace_id, "032x"), "span_id": format(ctx.span_id, "016x"),
        "parent_id": format(parent.span_id, "016x") if parent else None, "name": s.name,
        "service": str(s.resource.attributes.get("service.name", "")),
        "kind": {SpanKind.SERVER: "server", SpanKind.CLIENT: "client", SpanKind.PRODUCER: "producer",
                 SpanKind.CONSUMER: "consumer"}.get(s.kind, "internal"),
        "start": s.start_time or 0, "end": s.end_time or s.start_time or 0,
        "status": {StatusCode.OK: "ok", StatusCode.ERROR: "error"}.get(s.status.status_code, "unset"),
        "status_message": s.status.description,
        "attrs": dict(s.attributes or {}),
        "events": [{"name": e.name, "ts": e.timestamp, "attrs": dict(e.attributes or {})} for e in s.events],
        "links": [format(link.context.trace_id, "032x") for link in s.links],
    }


def _any(v) -> Any:
    kind = v.WhichOneof("value")
    if kind == "array_value":
        return [_any(x) for x in v.array_value.values]
    if kind == "kvlist_value":
        return {kv.key: _any(kv.value) for kv in v.kvlist_value.values}
    if kind == "bytes_value":
        return v.bytes_value.hex()
    return getattr(v, kind) if kind else None


def _from_otlp(body: bytes, content_type: str) -> list[dict]:
    from google.protobuf import json_format
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

    req = ExportTraceServiceRequest()
    if "json" in content_type:
        json_format.Parse(body, req, ignore_unknown_fields=True)
    else:
        req.ParseFromString(body)
    out = []
    for rs in req.resource_spans:
        service = next((_any(a.value) for a in rs.resource.attributes if a.key == "service.name"), "")
        for ss in rs.scope_spans:
            for sp in ss.spans:
                out.append({
                    "trace_id": sp.trace_id.hex(), "span_id": sp.span_id.hex(),
                    "parent_id": sp.parent_span_id.hex() if sp.parent_span_id.strip(b"\0") else None, "name": sp.name, "service": str(service),
                    "kind": _KINDS.get(sp.kind, "internal"), "start": sp.start_time_unix_nano,
                    "end": sp.end_time_unix_nano or sp.start_time_unix_nano,
                    "status": _STATUS.get(sp.status.code, "unset"), "status_message": sp.status.message or None,
                    "attrs": {a.key: _any(a.value) for a in sp.attributes},
                    "events": [{"name": e.name, "ts": e.time_unix_nano, "attrs": {a.key: _any(a.value) for a in e.attributes}}
                               for e in sp.events],
                    "links": [link.trace_id.hex() for link in sp.links],
                })
    return out


def _clean(value: Any, cap: int) -> Any:
    if isinstance(value, str):
        text, _ = guardrails.redact(value)
        return text if len(text) <= cap else f"{text[:cap]}… [{len(text) - cap} more chars]"
    if isinstance(value, (list, tuple)):
        return [_clean(v, cap) for v in value]
    if isinstance(value, dict):
        return {k: _clean(v, cap) for k, v in value.items()}
    return value


def _attrs(attrs: dict) -> dict:
    cap = get_settings().trace_max_attr_chars
    return {k: _clean(v, cap) for k, v in attrs.items() if k not in DROP}


# --- storage ----------------------------------------------------------------------------------------------

def _int(v) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _root_kind(sp: dict) -> str:
    a = sp["attrs"]
    if a.get("trace.kind"):
        return str(a["trace.kind"])
    path = str(a.get("url.path") or a.get("http.target") or a.get("http.route") or a.get("http.path") or sp["name"])
    if "/a2a" in path:
        return "a2a"
    return "gateway" if sp["service"] == "agentgateway" else "other"


def store(spans: list[dict]) -> None:
    """Write spans and update their trace summaries. Serialised: the exporter thread and the OTLP endpoint share it."""
    if not spans or not get_settings().trace_store:
        return
    with _lock:
        by_trace: dict[str, list[dict]] = {}
        for sp in spans:
            by_trace.setdefault(sp["trace_id"], []).append(sp)
        with db() as s:
            out = _write(s, by_trace)
            try:
                s.commit()
            except IntegrityError:  # another process created a summary first: retry once
                s.rollback()
                out = _write(s, by_trace)
                s.commit()
        _prune()
    for ev in out:  # once stored
        events.emit(ev)


def _write(s, by_trace: dict[str, list[dict]]) -> list[dict]:
    """Add the spans; return the activity events for them (published after the commit)."""
    out = []
    for tid, group in by_trace.items():
        t = _summarise(s, tid, group)
        for sp in group:
            a = sp["attrs"]
            clean = _attrs(a)
            row = TraceSpan(
                trace_id=tid, span_id=sp["span_id"], parent_id=sp["parent_id"], name=sp["name"][:200],
                service=sp["service"], kind=sp["kind"], start=_ts(sp["start"]),
                duration_ms=max(0, sp["end"] - sp["start"]) / 1e6, status=sp["status"],
                status_message=(sp["status_message"] or "")[:1000] or None,
                # gateway spans carry only the JWT user, on the request span: take who and where from the trace
                user=_str(a.get("user")) or t.user, session_id=_str(a.get("session.id")) or t.session_id,
                bot=_str(a.get("bot")) or t.bot,
                attrs_json=json.dumps(clean, default=str),
                events_json=json.dumps([{**e, "ts": _ts(e["ts"]).isoformat(), "attrs": _attrs(e["attrs"])}
                                        for e in sp["events"]], default=str))
            s.add(row)
            if ev := _activity(sp, row, clean, t):
                out.append(ev)
    return out


# --- activity events (CloudEvents on the bus) -------------------------------------------------------------

def _activity(sp: dict, row: TraceSpan, a: dict, t: Trace) -> dict | None:
    """One `com.agenticbots.*` event per tool, LLM, MCP or A2A call, from its (redacted) span.

    tool.called  pydantic-ai `execute_tool` spans: every tool a bot or the builder ran (MCP tools included);
    llm.called   pydantic-ai `chat` spans: model and tokens;
    mcp.called   agentgateway request spans (`tools/call <tool>`, `tools/list`, ...), also from external clients;
    a2a.called   the root span of an A2A request to one of our bots.
    `traceparent` (CloudEvents distributed tracing extension) points at the span, so a consumer can open the trace.
    """
    op = a.get("gen_ai.operation.name")
    data: dict[str, Any] = {
        "user": row.user, "session_id": row.session_id, "bot": row.bot, "run_id": _str(a.get("run.id")) or t.run_id,
        "trace_id": row.trace_id, "span_id": row.span_id, "start": row.start.isoformat(),
        "duration_ms": round(row.duration_ms, 1), "status": row.status, "error": row.status_message,
    }
    if op == "execute_tool":
        type_ = "tool.called"
        data |= {"tool": a.get("gen_ai.tool.name"), "call_id": a.get("gen_ai.tool.call.id")}
        if get_settings().event_content:
            data |= {"arguments": a.get("gen_ai.tool.call.arguments"), "result": a.get("gen_ai.tool.call.result")}
    elif op == "chat":
        type_ = "llm.called"
        data |= {"model": a.get("gen_ai.response.model") or a.get("gen_ai.request.model"),
                 "input_tokens": _int(a.get("gen_ai.usage.input_tokens")),
                 "output_tokens": _int(a.get("gen_ai.usage.output_tokens"))}
    elif sp["service"] == "agentgateway" and sp["kind"] == "server":
        type_ = "mcp.called"
        method, _, target = sp["name"].partition(" ")
        data |= {"method": str(a.get("mcp.method") or method), "target": target or None}
    elif sp["parent_id"] is None and _root_kind(sp) == "a2a":
        type_ = "a2a.called"
        data |= {"name": sp["name"]}
    else:
        return None
    return events.cloudevent(type_, data, subject=row.bot, traceparent=f"00-{row.trace_id}-{row.span_id}-01")


def _str(v) -> str | None:
    return str(v) if v not in (None, "") else None


def _summarise(s, tid: str, group: list[dict]) -> Trace:
    t = s.get(Trace, tid)
    first, last = _ts(min(sp["start"] for sp in group)), _ts(max(sp["end"] for sp in group))
    if t is None:
        t = Trace(trace_id=tid, started_at=first, ended_at=last)
    t.started_at, t.ended_at = min(_utc(t.started_at), first), max(_utc(t.ended_at), last)
    services = set(t.services.split())
    for sp in group:
        a = sp["attrs"]
        t.spans += 1
        t.errors += sp["status"] == "error"
        op = a.get("gen_ai.operation.name")
        if op == "chat":
            t.llm_calls += 1
            t.input_tokens += _int(a.get("gen_ai.usage.input_tokens"))
            t.output_tokens += _int(a.get("gen_ai.usage.output_tokens"))
        elif op == "execute_tool":
            t.tool_calls += 1
        services.add(sp["service"])
        t.user = t.user or _str(a.get("user"))
        t.session_id = t.session_id or _str(a.get("session.id"))
        t.bot = t.bot or _str(a.get("bot"))
        t.run_id = t.run_id or _str(a.get("run.id"))
        t.actor = t.actor or _str(a.get("actor"))
        t.parent_trace_id = t.parent_trace_id or next(iter(sp["links"]), None)
        if sp["parent_id"] is None:
            t.name, t.kind = sp["name"][:200], _root_kind(sp)
    t.services = " ".join(sorted(x for x in services if x))
    s.add(t)
    return t


def _prune() -> None:
    global _last_prune
    days = get_settings().trace_retention_days
    if not days or time.time() - _last_prune < 3600:
        return
    _last_prune = time.time()
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    with db() as s:
        s.exec(delete(TraceSpan).where(TraceSpan.start < cutoff))
        s.exec(delete(Trace).where(Trace.ended_at < cutoff))
        s.commit()


# --- queries ----------------------------------------------------------------------------------------------

def traces(*, user: str | None = None, session_id: str | None = None, bot: str | None = None,
           kind: str | None = None, before: str | None = None, limit: int = 100) -> list[Trace]:
    q = select(Trace)
    for col, value in ((Trace.user, user), (Trace.session_id, session_id), (Trace.bot, bot), (Trace.kind, kind)):
        if value:
            q = q.where(col == value)
    if before:
        q = q.where(Trace.started_at < _utc(datetime.fromisoformat(before)))
    with db() as s:
        return list(s.exec(q.order_by(Trace.started_at.desc()).limit(min(limit, 500))).all())


def get(trace_id: str) -> Trace | None:
    with db() as s:
        return s.get(Trace, trace_id)


def spans(trace_id: str) -> list[TraceSpan]:
    with db() as s:
        return list(s.exec(select(TraceSpan).where(TraceSpan.trace_id == trace_id).order_by(TraceSpan.start)).all())


def trace_dict(t: Trace) -> dict:
    return {**t.model_dump(), "started_at": _utc(t.started_at).isoformat(), "ended_at": _utc(t.ended_at).isoformat(),
            "duration_ms": (t.ended_at - t.started_at).total_seconds() * 1000, "services": t.services.split()}


def span_dict(sp: TraceSpan) -> dict:
    return {"span_id": sp.span_id, "parent_id": sp.parent_id, "name": sp.name, "service": sp.service, "kind": sp.kind,
            "start": _utc(sp.start).isoformat(), "duration_ms": sp.duration_ms, "status": sp.status,
            "status_message": sp.status_message, "user": sp.user, "session_id": sp.session_id, "bot": sp.bot,
            "attrs": json.loads(sp.attrs_json), "events": json.loads(sp.events_json)}


def detail(t: Trace) -> dict:
    return {"trace": trace_dict(t), "spans": [span_dict(sp) for sp in spans(t.trace_id)]}


# --- OTLP/HTTP receiver (agentgateway) --------------------------------------------------------------------

router = APIRouter()


def _forward(body: bytes, content_type: str) -> None:
    """Pass the gateway's spans on to the OTLP collector. `requests` is not instrumented: no spans about spans."""
    endpoint = get_settings().otel_exporter_otlp_endpoint
    if not endpoint:
        return
    import requests

    try:
        requests.post(f"{endpoint.rstrip('/')}/v1/traces", data=body, headers={"content-type": content_type}, timeout=5)
    except requests.RequestException as e:
        log.warning("forwarding gateway spans failed: %s", e)


@router.post("/otlp/v1/traces", include_in_schema=False)
async def receive(request: Request) -> Response:
    token = get_settings().trace_ingest_token
    if not token or not hmac.compare_digest(request.headers.get("x-trace-token", "").encode(), token.encode()):
        return Response("trace token required", status_code=401)
    body = await request.body()
    content_type = request.headers.get("content-type", "application/x-protobuf")
    try:
        spans = _from_otlp(body, content_type)
    except Exception as e:  # noqa: BLE001
        return Response(f"bad OTLP payload: {e}", status_code=400)
    await run_in_threadpool(store, spans)
    await run_in_threadpool(_forward, body, content_type)
    if "json" in content_type:
        return Response("{}", media_type="application/json")
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceResponse

    return Response(ExportTraceServiceResponse().SerializeToString(), media_type="application/x-protobuf")
