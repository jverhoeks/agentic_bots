"""Admin API: everything that happens, across users (runs, tool calls, guardrail hits, logins).

Sources: the audit log (app.audit), runs and messages, the traces in the app database (app.tracestore:
every chat turn, run, tool, MCP and A2A call), and links to an external trace UI (TRACE_UI_URL, if set) and the agentgateway UI.
"""

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import func, select

from app import audit, spec, tracestore
from app.agents import bot_runner
from app.agents.runtime import message_dict
from app.auth import ADMIN, require_admin, set_role
from app.config import get_settings
from app.db import db
from app.models import AuditEvent, Message, Run, Trace, User, now

router = APIRouter(prefix="/api/admin", dependencies=[Depends(require_admin)])


class RolePatch(BaseModel):
    role: str | None  # "admin" or null


def _bot_owner(name: str) -> str | None:
    try:
        b = spec.get_bot(name)
    except ValueError:
        return None
    return b.owner if b else None


@router.get("/overview")
def overview():
    s_ = get_settings()
    since = now() - timedelta(hours=24)
    with db() as s:
        runs = s.exec(select(Run.status, func.count()).where(Run.started_at >= since).group_by(Run.status)).all()
        kinds = s.exec(select(AuditEvent.kind, func.count()).where(AuditEvent.ts >= since)
                       .group_by(AuditEvent.kind)).all()
        users = s.exec(select(func.count()).select_from(User)).one()
        traces = s.exec(select(Trace.kind, func.count()).where(Trace.started_at >= since)
                        .group_by(Trace.kind)).all()
    return {
        "users": users,
        "bots": len(spec.list_bots()),
        "runs_24h": dict(runs),
        "events_24h": dict(kinds),
        "guardrails_24h": sum(n for k, n in kinds if k.startswith("guardrail.")),
        "traces_24h": dict(traces),
        "links": {"traces": s_.trace_ui_url, "gateway": s_.gateway_ui_url},
    }


@router.get("/users")
def users():
    with db() as s:
        rows = s.exec(select(User).order_by(User.created_at)).all()
        last = dict(s.exec(select(AuditEvent.user, func.max(AuditEvent.ts)).group_by(AuditEvent.user)).all())
    bots = spec.list_bots()
    return [{"id": u.id, "name": u.name, "role": u.role, "created_at": u.created_at.isoformat(),
             "bots": sum(1 for b in bots if b.owner == u.id),
             "last_seen": last[u.id].isoformat() if last.get(u.id) else None} for u in rows]


@router.patch("/users/{user_id}")
def patch_user(user_id: str, body: RolePatch, admin: str = Depends(require_admin)):
    if user_id == admin:
        raise HTTPException(400, "you cannot change your own role")
    if body.role not in (None, ADMIN):
        raise HTTPException(422, f"role must be {ADMIN!r} or null")
    try:
        u = set_role(user_id, body.role)
    except ValueError as e:
        raise HTTPException(404, str(e))
    audit.record("admin.role_changed", user=user_id, actor=admin, detail={"role": body.role})
    return {"id": u.id, "role": u.role}


@router.get("/bots")
def bots():
    return [{**b.model_dump(exclude={"instructions"}), "session_id": bot_runner.session_id_for(b.name)}
            for b in spec.list_bots()]


@router.get("/runs")
def runs(user: str | None = None, bot: str | None = None, status: str | None = None, limit: int = 100):
    q = select(Run)
    if bot:
        q = q.where(Run.bot_name == bot)
    if status:
        q = q.where(Run.status == status)
    if user:
        q = q.where(Run.owner == user)
    with db() as s:
        rows = s.exec(q.order_by(Run.started_at.desc()).limit(min(limit, 500))).all()
    # runs from before Run.owner existed: fall back to the bot's current owner
    return [{**bot_runner.run_dict(r), "owner": r.owner or _bot_owner(r.bot_name)} for r in rows]


@router.get("/runs/{run_id}")
def run(run_id: str):
    with db() as s:
        r = s.get(Run, run_id)
        if not r:
            raise HTTPException(404, "unknown run")
        msgs = s.exec(select(Message).where(Message.run_id == run_id).order_by(Message.id)).all()
    return {"run": {**bot_runner.run_dict(r), "owner": r.owner or _bot_owner(r.bot_name)},
            "messages": [message_dict(m) for m in msgs],
            "events": [audit.event_dict(e) for e in reversed(audit.events(run_id=run_id, limit=1000))]}


@router.get("/audit")
def audit_log(kind: str | None = None, user: str | None = None, bot: str | None = None,
              run_id: str | None = None, before: int | None = None, limit: int = 200):
    """Newest first; page with `before=<id of the last event>`. `kind` matches a prefix (e.g. "guardrail.")."""
    return [audit.event_dict(e) for e in audit.events(kind=kind, user=user, bot=bot, run_id=run_id,
                                                      before=before, limit=limit)]


@router.get("/traces")
def traces(user: str | None = None, session_id: str | None = None, bot: str | None = None, kind: str | None = None,
           before: str | None = None, limit: int = 100):
    """Newest first; page with `before=<started_at of the last trace>`. Spans: GET /api/traces/{id}."""
    return [tracestore.trace_dict(t) for t in tracestore.traces(user=user, session_id=session_id, bot=bot, kind=kind,
                                                               before=before, limit=limit)]
