"""Each bot as an A2A 1.0 server at `/a2a/<bot>/` (Agent Card at `/a2a/<bot>/.well-known/agent-card.json`).

Built on fasta2a (JSON-RPC, SSE streaming, task store). Our worker turns every A2A task into a normal
run of the bot (trigger `a2a:user:<caller>`, the token's `sub`), so it appears in the bot's session, run history and memory.

Not exposed by the UI's nginx: remote callers reach it through agentgateway's A2A route. Every
request needs a JWT from app.tokens (agentgateway checks it, and so do we): a caller only reaches
bots its `sub` may see. fasta2a's task store is in memory; the durable record is our Runs table.
"""

import asyncio
import json
import uuid
from dataclasses import dataclass

import jwt
from fasta2a import FastA2A, Worker
from fasta2a.broker import InMemoryBroker
from fasta2a.schema import AgentProvider, Artifact, Message, Skill, TaskIdParams, TaskSendParams
from fasta2a.storage import InMemoryStorage
from opentelemetry import trace
from starlette.types import Receive, Scope, Send

from app import audit, spec, tokens
from app.agents import a2a, bot_runner
from app.db import db
from app.models import Run

PREFIX = "/a2a"


@dataclass
class BotWorker(Worker):
    bot_name: str = ""
    caller: str = ""  # `sub` of the token: who asked (one app per bot and caller)

    async def run_task(self, params: TaskSendParams) -> None:
        task_id, context_id = params["id"], params["context_id"]
        text = "\n".join(p["text"] for p in params["message"].get("parts", []) if p.get("text"))
        await self.storage.update_task(task_id, state="working")
        await self.publish_status(task_id, context_id, "working")

        bot = spec.get_bot(self.bot_name)
        audit.record("a2a.inbound", user=self.caller, actor=f"a2a:user:{self.caller}", bot=self.bot_name,
                     detail={"task_id": task_id, "message": text[:500]})
        if not bot:
            return await self._fail(task_id, f"bot '{self.bot_name}' no longer exists")
        try:
            run = bot_runner.start_run(bot, trigger=f"a2a:user:{self.caller}",
                                       prompt=a2a.task_prompt(f"an external A2A client of user '{self.caller}'", text))
        except RuntimeError as e:
            return await self._fail(task_id, str(e))
        await bot_runner.wait(run.id)
        with db() as s:
            r = s.get(Run, run.id)
        if r.status != "ok":
            return await self._fail(task_id, r.error or "run failed")
        output = a2a.run_output(run.id) or ""
        artifact = Artifact(artifact_id=str(uuid.uuid4()), name="result", parts=[{"text": output}])
        await self.storage.update_task(task_id, state="completed", new_artifacts=[artifact],
                                       new_messages=[_agent_message(output)])

    async def _fail(self, task_id: str, reason: str) -> None:
        await self.storage.update_task(task_id, state="failed", new_messages=[_agent_message(reason)])

    async def cancel_task(self, params: TaskIdParams) -> None:
        await self.storage.update_task(params["id"], state="canceled")

    def build_message_history(self, history: list[Message]) -> list:
        return history  # the bot session holds the real history

    def build_artifacts(self, result) -> list[Artifact]:
        return []


def _agent_message(text: str) -> Message:
    return Message(role="agent", parts=[{"text": text}], message_id=str(uuid.uuid4()))


class A2AMount:
    """ASGI app for `/a2a/<bot>/...`: one FastA2A app per bot and caller, (re)built when the bot changes.

    Per caller, so runs record who asked and one caller cannot read another caller's tasks.
    """

    def __init__(self) -> None:
        self._apps: dict[tuple[str, str], tuple[str, FastA2A, asyncio.Event, asyncio.Task]] = {}
        self._lock = asyncio.Lock()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return
        name, _, rest = scope["path"].removeprefix(PREFIX).lstrip("/").partition("/")
        user = _caller(scope)
        if user is None:
            return await _error(send, 401, "a bearer token from the agent gateway is required")
        try:
            bot = spec.get_bot(name)
        except ValueError:
            bot = None
        if not bot or not spec.visible_to(bot, user):
            return await _error(send, 404, f"no bot named {name!r}")
        audit.scope(user=user, actor=f"a2a:user:{user}", bot=name)  # on the request's spans (tracestore)
        if span := trace.get_current_span():
            span.set_attributes({"trace.kind": "a2a", "user": user, "bot": name, "actor": f"a2a:user:{user}"})
        app = await self._app(bot, user, scope)
        root = f"{scope.get('root_path', '')}{PREFIX}/{name}"
        await app({**scope, "root_path": root, "path": f"/{rest}"}, receive, send)

    async def _app(self, bot: spec.BotSpec, caller: str, scope: Scope) -> FastA2A:
        version, key = bot.updated_at or "", (bot.name, caller)
        async with self._lock:
            if (cached := self._apps.get(key)) and cached[0] == version:
                return cached[1]
            if cached:
                await _stop(cached)
            storage, broker = InMemoryStorage(), InMemoryBroker()
            app = FastA2A(
                storage=storage, broker=broker, name=bot.title, url=f"{_base_url(scope)}{PREFIX}/{bot.name}/",
                version=version[:10] or "1.0.0", description=bot.description, docs_url=None,
                provider=AgentProvider(organization="agentic-bots", url=_base_url(scope)),
                skills=[Skill(id=bot.name, name=bot.title, description=bot.description, tags=bot.tools,
                              input_modes=["text/plain"], output_modes=["text/markdown"])],
                default_input_modes=["text/plain"], default_output_modes=["text/markdown"],
            )
            worker = BotWorker(broker=broker, storage=storage, bot_name=bot.name, caller=caller)
            ready, stop = asyncio.Event(), asyncio.Event()
            # anyio task groups must be exited by the task that entered them: one owner task per app
            owner = asyncio.create_task(_serve(app, worker, ready, stop))
            await asyncio.wait({owner, asyncio.create_task(ready.wait())}, return_when=asyncio.FIRST_COMPLETED)
            if owner.done():
                owner.result()  # startup failed: raise it
            self._apps[key] = (version, app, stop, owner)
            return app

    async def aclose(self) -> None:
        for cached in self._apps.values():
            await _stop(cached)
        self._apps.clear()


async def _serve(app: FastA2A, worker: BotWorker, ready: asyncio.Event, stop: asyncio.Event) -> None:
    async with app.task_manager, worker.run():
        ready.set()
        await stop.wait()


async def _stop(cached: tuple) -> None:
    _, _, stop, owner = cached
    stop.set()
    await owner


def _base_url(scope: Scope) -> str:
    headers = {k.decode(): v.decode() for k, v in scope.get("headers", [])}
    proto = headers.get("x-forwarded-proto") or scope.get("scheme", "http")
    host = headers.get("x-forwarded-host") or headers.get("host") or "localhost:8000"
    return f"{proto}://{host}"


def _caller(scope: Scope) -> str | None:
    """`sub` of a valid bearer token, or None."""
    auth = dict(scope.get("headers", [])).get(b"authorization", b"").decode()
    if not auth.lower().startswith("bearer "):
        return None
    try:
        return tokens.verify(auth[7:].strip())["sub"]
    except jwt.InvalidTokenError:
        return None


async def _error(send: Send, status: int, detail: str) -> None:
    body = json.dumps({"detail": detail}).encode()
    await send({"type": "http.response.start", "status": status, "headers": [(b"content-type", b"application/json")]})
    await send({"type": "http.response.body", "body": body})


mount = A2AMount()
