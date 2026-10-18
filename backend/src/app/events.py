"""Event bus. Every event is a CloudEvents 1.0 (structured JSON) envelope, type `com.agenticbots.*`.

Three kinds of consumers:
- handlers (`on(type_prefix, fn)`): in-process consumers, called inline (they must be quick and must not raise);
- subscribers (`subscribe()`): live, best-effort queues, e.g. the browser's /api/events stream (bot.* and run.* only);
- the sink (EVENT_SINK_URL): every event is POSTed in batches (`application/cloudevents-batch+json`) from a
  background thread, for consumers outside this process such as the lineage/quality repo. Best effort: a
  failed batch is logged and dropped.
`publish()` may be called from any thread: the activity events come from the span exporter thread.
Swapping the sink for a real broker (NATS, Kafka) keeps the envelope.
"""

import asyncio
import logging
import queue
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

SOURCE = "/agentic-bots"
OWN = "com.agenticbots."
log = logging.getLogger("app.events")
_subscribers: set[asyncio.Queue] = set()
_handlers: list[tuple[str, Callable[[dict], None]]] = []
_loop: asyncio.AbstractEventLoop | None = None  # the loop the subscriber queues belong to
_outbox: queue.Queue = queue.Queue(maxsize=10_000)
_sender: threading.Thread | None = None


def cloudevent(type_: str, data: Any, subject: str | None = None, **extensions: str) -> dict:
    ev = {
        "specversion": "1.0",
        "id": str(uuid.uuid4()),
        "source": SOURCE,
        "type": f"{OWN}{type_}",
        "time": datetime.now(timezone.utc).isoformat(),
        "datacontenttype": "application/json",
        "data": data,
    }
    if subject:
        ev["subject"] = subject
    ev.update({k: v for k, v in extensions.items() if v})  # e.g. traceparent (distributed tracing extension)
    return ev


def publish(type_: str, data: Any, subject: str | None = None, **extensions: str) -> dict:
    ev = cloudevent(type_, data, subject, **extensions)
    emit(ev)
    return ev


def emit(ev: dict) -> None:
    for prefix, handler in list(_handlers):
        if ev["type"].startswith(prefix):
            try:
                handler(ev)
            except Exception:  # noqa: BLE001  one consumer must not break the producer
                log.exception("event handler for %s failed", ev["type"])
    broadcast(ev)
    _sink(ev)


def on(type_prefix: str, handler: Callable[[dict], None]) -> None:
    """Register an in-process consumer for events whose type starts with `type_prefix`."""
    _handlers.append((type_prefix, handler))


def off(handler: Callable[[dict], None]) -> None:
    _handlers[:] = [(p, h) for p, h in _handlers if h != handler]


def broadcast(ev: dict) -> dict:
    """Hand the event to the live subscribers, on their event loop (asyncio queues are not thread-safe)."""
    loop = _loop
    if loop is None or not _subscribers:
        return ev
    try:
        on_loop = asyncio.get_running_loop() is loop
    except RuntimeError:
        on_loop = False
    if on_loop:
        _put(ev)
    else:
        try:
            loop.call_soon_threadsafe(_put, ev)
        except RuntimeError:  # loop closed (shutdown)
            pass
    return ev


def _put(ev: dict) -> None:
    for q in list(_subscribers):
        try:
            q.put_nowait(ev)
        except asyncio.QueueFull:
            pass  # slow consumer; it will resync from the REST API


def subscribe() -> asyncio.Queue:
    """Call from the event loop (an async endpoint)."""
    global _loop
    _loop = asyncio.get_running_loop()
    q: asyncio.Queue = asyncio.Queue(maxsize=1000)
    _subscribers.add(q)
    return q


def unsubscribe(q: asyncio.Queue) -> None:
    _subscribers.discard(q)


# --- sink (EVENT_SINK_URL) --------------------------------------------------------------------------------

def _sink(ev: dict) -> None:
    from app.config import get_settings

    if not get_settings().event_sink_url:
        return
    _start_sender()
    try:
        _outbox.put_nowait(ev)
    except queue.Full:
        log.warning("event sink backlog full: dropping %s", ev["type"])


def _start_sender() -> None:
    global _sender
    if _sender is None or not _sender.is_alive():
        _sender = threading.Thread(target=_send_loop, name="event-sink", daemon=True)
        _sender.start()


def _send_loop() -> None:
    while True:
        batch = [_outbox.get()]
        while len(batch) < 100:
            try:
                batch.append(_outbox.get(timeout=0.5))
            except queue.Empty:
                break
        send(batch)


def send(batch: list[dict]) -> None:
    """POST one batch to the sink. `requests` is not instrumented: no spans about events about spans."""
    import json

    import requests

    from app.config import get_settings

    s = get_settings()
    headers = {"content-type": "application/cloudevents-batch+json"}
    if s.event_sink_token:
        headers["authorization"] = f"Bearer {s.event_sink_token}"
    try:
        r = requests.post(s.event_sink_url, data=json.dumps(batch, default=str), headers=headers, timeout=5)
        if r.status_code >= 300:
            log.warning("event sink answered %s for %d events", r.status_code, len(batch))
    except requests.RequestException as e:
        log.warning("event sink failed for %d events: %s", len(batch), e)
