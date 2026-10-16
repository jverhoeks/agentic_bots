"""In-process event bus. Every event is a CloudEvents 1.0 (structured JSON) envelope."""

import asyncio
import uuid
from datetime import datetime, timezone
from typing import Any

SOURCE = "/agentic-bots"
_subscribers: set[asyncio.Queue] = set()


def cloudevent(type_: str, data: Any, subject: str | None = None) -> dict:
    ev = {
        "specversion": "1.0",
        "id": str(uuid.uuid4()),
        "source": SOURCE,
        "type": f"com.agenticbots.{type_}",
        "time": datetime.now(timezone.utc).isoformat(),
        "datacontenttype": "application/json",
        "data": data,
    }
    if subject:
        ev["subject"] = subject
    return ev


def publish(type_: str, data: Any, subject: str | None = None) -> dict:
    ev = cloudevent(type_, data, subject)
    for q in list(_subscribers):
        try:
            q.put_nowait(ev)
        except asyncio.QueueFull:
            pass  # slow consumer; it will resync from the REST API
    return ev


def subscribe() -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue(maxsize=1000)
    _subscribers.add(q)
    return q


def unsubscribe(q: asyncio.Queue) -> None:
    _subscribers.discard(q)
