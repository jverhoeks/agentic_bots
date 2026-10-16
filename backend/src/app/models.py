"""Runtime state (SQLite). Bot definitions live in bots/*/SKILL.md, not here."""

from datetime import datetime, timezone

from sqlmodel import Field, SQLModel


def now() -> datetime:
    return datetime.now(timezone.utc)


class Session(SQLModel, table=True):
    id: str = Field(primary_key=True)  # "main" or "bot-<name>"
    kind: str  # main | bot
    bot_name: str | None = Field(default=None, index=True)
    title: str
    history_json: str = "[]"  # pydantic-ai message history (what the model sees)
    created_at: datetime = Field(default_factory=now)


class Message(SQLModel, table=True):
    """What the UI renders. Synthetic run prompts are never stored here."""

    id: int | None = Field(default=None, primary_key=True)
    session_id: str = Field(index=True)
    role: str  # user | assistant | error
    content: str
    tool_calls_json: str = "[]"
    run_id: str | None = None
    created_at: datetime = Field(default_factory=now)


class Run(SQLModel, table=True):
    id: str = Field(primary_key=True)
    bot_name: str = Field(index=True)
    session_id: str
    trigger: str = "manual"
    status: str = "running"  # running | ok | failed
    started_at: datetime = Field(default_factory=now)
    finished_at: datetime | None = None
    usage_json: str = "{}"
    error: str | None = None
