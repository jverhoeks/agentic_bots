"""Audit log: who did what, when, as whom (table AuditEvent), shown on the admin page.

The acting context (user, actor, bot, run) lives in a contextvar: request handlers and `bot_runner._run`
set it with `scope(...)`, and deeper code (runtime, tools, guardrails) calls `record(kind, ...)`.
Runs are tasks that copy the context of the request that started them, so `_run` always sets its own.
"""

import json
import logging
from contextvars import ContextVar
from typing import Any

from sqlmodel import select

from app import guardrails, telemetry
from app.db import db
from app.models import AuditEvent

log = logging.getLogger("app.audit")
_scope: ContextVar[dict] = ContextVar("audit_scope", default={})
MAX_DETAIL = 4000


def scope(**values: str | None) -> None:
    """Set the acting context for the current task (merged into the existing one)."""
    _scope.set({**_scope.get(), **{k: v for k, v in values.items() if v is not None}})


def current() -> dict:
    return dict(_scope.get())


def _detail(detail: dict | None) -> str:
    text, _ = guardrails.redact(json.dumps(detail or {}, default=str))
    return text if len(text) <= MAX_DETAIL else json.dumps({"truncated": text[:MAX_DETAIL]})


def record(kind: str, *, user: str | None = None, actor: str | None = None, bot: str | None = None,
           run_id: str | None = None, session_id: str | None = None, detail: dict | None = None) -> None:
    """Append an event; explicit arguments override the context. Never raises: auditing must not break a run."""
    ctx = _scope.get()
    try:
        ev = AuditEvent(kind=kind, user=user or ctx.get("user"), actor=actor or ctx.get("actor"),
                        bot=bot or ctx.get("bot"), run_id=run_id or ctx.get("run_id"),
                        session_id=session_id or ctx.get("session_id"), detail_json=_detail(detail),
                        trace_id=telemetry.trace_id())
        with db() as s:
            s.add(ev)
            s.commit()
    except Exception:  # noqa: BLE001
        log.exception("audit record failed: %s", kind)


def events(*, kind: str | None = None, user: str | None = None, bot: str | None = None, run_id: str | None = None,
           before: int | None = None, limit: int = 200) -> list[AuditEvent]:
    q = select(AuditEvent)
    if kind:
        q = q.where(AuditEvent.kind.startswith(kind))
    if user:
        q = q.where(AuditEvent.user == user)
    if bot:
        q = q.where(AuditEvent.bot == bot)
    if run_id:
        q = q.where(AuditEvent.run_id == run_id)
    if before:
        q = q.where(AuditEvent.id < before)
    with db() as s:
        return list(s.exec(q.order_by(AuditEvent.id.desc()).limit(min(limit, 1000))).all())


def event_dict(e: AuditEvent) -> dict[str, Any]:
    return {"id": e.id, "ts": e.ts.isoformat(), "kind": e.kind, "user": e.user, "actor": e.actor, "bot": e.bot,
            "run_id": e.run_id, "session_id": e.session_id, "detail": json.loads(e.detail_json or "{}"),
            "trace_id": e.trace_id}
