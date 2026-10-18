"""Runtime state (SQLite or PostgreSQL). Bot definitions live in bots/*/SKILL.md, not here."""

from datetime import datetime, timezone

from sqlmodel import Field, SQLModel


def now() -> datetime:
    return datetime.now(timezone.utc)


class User(SQLModel, table=True):
    id: str = Field(primary_key=True)  # login name (slug); also the user's connection name in open-connector
    name: str
    password_hash: str
    role: str | None = None  # "admin" or None
    oidc_sub: str | None = Field(default=None, index=True)  # "<issuer>|<sub>" of a single sign-on account
    created_at: datetime = Field(default_factory=now)


class Session(SQLModel, table=True):
    id: str = Field(primary_key=True)  # "main-<user>" or "bot-<name>"
    kind: str  # main | bot
    bot_name: str | None = Field(default=None, index=True)
    owner: str | None = Field(default=None, index=True)  # user of a main session (bot sessions follow the bot)
    title: str
    history_json: str = "[]"  # pydantic-ai message history (what the model sees)
    created_at: datetime = Field(default_factory=now)


class Message(SQLModel, table=True):
    """What the UI renders. Synthetic run prompts are never stored here."""

    id: int | None = Field(default=None, primary_key=True)
    session_id: str = Field(index=True)
    role: str  # user | assistant | error | notice (a bot changed itself)
    content: str
    tool_calls_json: str = "[]"
    run_id: str | None = None
    trace_id: str | None = None  # the chat turn or run that produced it (trace view)
    created_at: datetime = Field(default_factory=now)


class Run(SQLModel, table=True):
    id: str = Field(primary_key=True)
    bot_name: str = Field(index=True)
    session_id: str
    trigger: str = "manual"
    owner: str | None = Field(default=None, index=True)  # user the run acts as (bot owner; None = shared)
    actor: str | None = None  # who started it: a user, "a2a:<bot>", "a2a:user:<sub>"
    trace_id: str | None = None  # OpenTelemetry trace (trace view on the admin page)
    status: str = "running"  # running | ok | failed
    started_at: datetime = Field(default_factory=now)
    finished_at: datetime | None = None
    usage_json: str = "{}"
    error: str | None = None


class BotMemory(SQLModel, table=True):
    """Per-bot key/value memory that survives runs. Keys starting with "_" are written by the platform."""

    bot_name: str = Field(primary_key=True)
    key: str = Field(primary_key=True)
    value: str
    updated_at: datetime = Field(default_factory=now)


class AuditEvent(SQLModel, table=True):
    """Append-only record of what happened, for the admin page. `detail_json` is redacted."""

    id: int | None = Field(default=None, primary_key=True)
    ts: datetime = Field(default_factory=now, index=True)
    kind: str = Field(index=True)  # auth.login, run.started, tool.call, guardrail.*, bot.created, a2a.*, ...
    user: str | None = Field(default=None, index=True)  # the user it acted as / for
    actor: str | None = None  # who caused it, when different (another bot, an A2A client)
    bot: str | None = Field(default=None, index=True)
    run_id: str | None = Field(default=None, index=True)
    session_id: str | None = None
    detail_json: str = "{}"
    trace_id: str | None = None


class Trace(SQLModel, table=True):
    """One trace (a chat turn, a bot run, an A2A or MCP request), summarised from its spans."""

    trace_id: str = Field(primary_key=True)
    name: str = ""  # root span: chat.turn, bot.run, the gateway request, ...
    kind: str = Field(default="other", index=True)  # chat | run | a2a | gateway | other
    user: str | None = Field(default=None, index=True)
    session_id: str | None = Field(default=None, index=True)
    bot: str | None = Field(default=None, index=True)
    run_id: str | None = Field(default=None, index=True)
    actor: str | None = None
    parent_trace_id: str | None = None  # the trace that started this one (a chat that ran a bot, ...)
    started_at: datetime = Field(default_factory=now, index=True)
    ended_at: datetime = Field(default_factory=now)
    spans: int = 0
    errors: int = 0
    llm_calls: int = 0
    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    services: str = ""  # space separated: agentic-bots-backend agentgateway


class TraceSpan(SQLModel, table=True):
    """An OpenTelemetry span from the backend or the gateway. `attrs_json` is redacted and capped."""

    id: int | None = Field(default=None, primary_key=True)
    trace_id: str = Field(index=True)
    span_id: str
    parent_id: str | None = None
    name: str
    service: str = ""
    kind: str = "internal"  # internal | server | client | producer | consumer
    start: datetime = Field(index=True)
    duration_ms: float = 0
    status: str = "unset"  # unset | ok | error
    status_message: str | None = None
    user: str | None = Field(default=None, index=True)
    session_id: str | None = Field(default=None, index=True)
    bot: str | None = None
    attrs_json: str = "{}"
    events_json: str = "[]"

