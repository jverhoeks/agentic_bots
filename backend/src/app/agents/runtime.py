"""Shared execution: run an agent turn in a session, stream UI events, persist results."""

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

from pydantic_ai import (
    Agent,
    AgentRunResultEvent,
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    PartDeltaEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
)
from pydantic_ai.exceptions import UsageLimitExceeded

from app import audit, guardrails, history, telemetry
from app.db import db
from app.models import Message, Session

_locks: dict[str, asyncio.Lock] = {}


def session_lock(session_id: str) -> asyncio.Lock:
    return _locks.setdefault(session_id, asyncio.Lock())


def _preview(value: Any, limit: int = 400) -> str:
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return text if len(text) <= limit else text[:limit] + "…"


def _usage(usage) -> dict:
    return {
        "requests": usage.requests,
        "tool_calls": usage.tool_calls,
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
    }


async def execute(
    agent: Agent,
    session_id: str,
    prompt: str,
    *,
    show_prompt: bool = True,
    run_id: str | None = None,
    max_steps: int = 25,
    deps: Any = None,
) -> AsyncIterator[dict]:
    """Run one agent turn against the session history and yield UI events.

    `show_prompt=False` is used for scheduled/manual bot runs: the synthetic task prompt goes
    into the model history but not into the visible conversation.
    Events: text_delta, tool_call, tool_result, done (with the stored message), error.
    Every tool call and guardrail hit goes to the audit log, in the caller's `audit.scope`.
    """
    if too_long := guardrails.check_prompt(prompt):
        audit.record("guardrail.prompt_too_long", session_id=session_id, run_id=run_id, detail={"chars": len(prompt)})
        yield {"type": "error", "error": too_long, "message": None}
        return
    with db() as s:
        sess = s.get(Session, session_id)
        past = history.load(sess.history_json)
        if show_prompt:
            s.add(Message(session_id=session_id, role="user", content=prompt, trace_id=telemetry.trace_id()))
            s.commit()

    tool_calls: dict[str, dict] = {}
    result = None
    try:
        async with agent.run_stream_events(
            prompt, message_history=past, usage_limits=guardrails.usage_limits(max_steps), deps=deps
        ) as events:
            async for ev in events:
                if isinstance(ev, PartStartEvent) and isinstance(ev.part, TextPart) and ev.part.content:
                    yield {"type": "text_delta", "text": ev.part.content}
                elif isinstance(ev, PartDeltaEvent) and isinstance(ev.delta, TextPartDelta):
                    yield {"type": "text_delta", "text": ev.delta.content_delta}
                elif isinstance(ev, FunctionToolCallEvent):
                    call = {"id": ev.part.tool_call_id, "name": ev.part.tool_name, "args": ev.part.args_as_dict(), "status": "running"}
                    tool_calls[call["id"]] = call
                    yield {"type": "tool_call", **call}
                elif isinstance(ev, FunctionToolResultEvent):
                    call = tool_calls.get(ev.part.tool_call_id, {"id": ev.part.tool_call_id, "name": ev.part.tool_name})
                    failed = getattr(ev.part, "part_kind", "") == "retry-prompt"
                    call.update(status="error" if failed else "ok", preview=guardrails.redact(_preview(ev.part.content))[0])
                    audit.record("tool.call", session_id=session_id, run_id=run_id,
                                 detail={"tool": call["name"], "args": call.get("args"), "status": call["status"],
                                         "result": call["preview"][:300]})
                    yield {"type": "tool_result", **call}
                elif isinstance(ev, AgentRunResultEvent):
                    result = ev.result
            result = result or events.result
        output = result.output if isinstance(result.output, str) else json.dumps(result.output, default=str)
        output, redacted = guardrails.redact(output)
        if redacted:
            audit.record("guardrail.redacted", session_id=session_id, run_id=run_id, detail={"where": "answer", "count": redacted})
        with db() as s:
            sess = s.get(Session, session_id)
            sess.history_json = history.dump(result.all_messages())
            msg = Message(
                session_id=session_id,
                role="assistant",
                content=output,
                tool_calls_json=json.dumps(list(tool_calls.values()), default=str),
                run_id=run_id,
                trace_id=telemetry.trace_id(),
            )
            s.add(sess)
            s.add(msg)
            s.commit()
            s.refresh(msg)
        yield {"type": "done", "message": message_dict(msg), "usage": _usage(result.usage)}
    except Exception as e:  # surface every failure in the conversation instead of a dead stream
        err = f"{type(e).__name__}: {e}"
        if isinstance(e, UsageLimitExceeded):
            audit.record("guardrail.usage_limit", session_id=session_id, run_id=run_id, detail={"error": str(e)})
        with db() as s:
            msg = Message(session_id=session_id, role="error", content=err, run_id=run_id, trace_id=telemetry.trace_id(),
                          tool_calls_json=json.dumps(list(tool_calls.values()), default=str))
            s.add(msg)
            s.commit()
            s.refresh(msg)
        yield {"type": "error", "error": err, "message": message_dict(msg)}


def message_dict(m: Message) -> dict:
    return {
        "id": m.id,
        "session_id": m.session_id,
        "role": m.role,
        "content": m.content,
        "tool_calls": json.loads(m.tool_calls_json or "[]"),
        "run_id": m.run_id,
        "trace_id": m.trace_id,
        "created_at": m.created_at.isoformat(),
    }
