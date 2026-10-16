"""Persisted pydantic-ai message history, with compaction so follow-ups stay cheap."""

import dataclasses
import json

from pydantic_ai import ModelMessage, ModelMessagesTypeAdapter, ModelRequest, ToolReturnPart, UserPromptPart

from app.config import get_settings

_KEEP_CHARS = 300


def load(history_json: str) -> list[ModelMessage]:
    return ModelMessagesTypeAdapter.validate_json(history_json or "[]")


def dump(messages: list[ModelMessage]) -> str:
    return ModelMessagesTypeAdapter.dump_json(compact(messages)).decode()


def compact(messages: list[ModelMessage], full_turns: int | None = None) -> list[ModelMessage]:
    """Keep the last `full_turns` user turns intact; shrink tool outputs in older turns.

    Tool call / return pairs stay in place (models require them), only the payload is replaced.
    """
    full_turns = full_turns if full_turns is not None else get_settings().history_full_turns
    turn_starts = [
        i
        for i, m in enumerate(messages)
        if isinstance(m, ModelRequest) and any(isinstance(p, UserPromptPart) for p in m.parts)
    ]
    if len(turn_starts) <= full_turns:
        return messages
    cutoff = turn_starts[-full_turns]
    out: list[ModelMessage] = []
    for i, m in enumerate(messages):
        if i < cutoff and isinstance(m, ModelRequest):
            m = dataclasses.replace(m, parts=[_shrink(p) for p in m.parts])
        out.append(m)
    return out


def _shrink(part):
    if not isinstance(part, ToolReturnPart):
        return part
    text = part.content if isinstance(part.content, str) else json.dumps(part.content, default=str)
    if len(text) <= _KEEP_CHARS:
        return part
    return dataclasses.replace(
        part, content=f"{text[:_KEEP_CHARS]}… [older tool output truncated; call the tool again if needed]"
    )
