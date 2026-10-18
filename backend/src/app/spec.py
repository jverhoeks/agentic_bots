"""Bot definitions stored as Agent Skills (`bots/<name>/SKILL.md`).

Frontmatter follows the Agent Skills format: `name`, `description`, `allowed-tools`
(space separated) and a string-valued `metadata` map for bot-specific settings.
The markdown body holds the bot's instructions.
"""

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from app.config import get_settings

NAME_RE = r"^[a-z0-9]+(-[a-z0-9]+)*$"
Verbosity = Literal["short", "medium", "verbose"]
_FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?(.*)\Z", re.S)


class BotSpec(BaseModel):
    name: str = Field(pattern=NAME_RE, min_length=3, max_length=64)
    title: str = Field(min_length=1, max_length=80)
    description: str = Field(min_length=1, max_length=1024)
    instructions: str = Field(min_length=20)
    model: str
    tools: list[str] = []
    trigger: Literal["manual", "schedule"] = "manual"  # set from `schedule`; later also "event"
    schedule: str | None = None  # cron expression (5 fields, SCHEDULE_TIMEZONE), e.g. "0 7 * * 1-5"
    max_steps: int = Field(25, ge=1, le=100)
    verbosity: Verbosity = "short"  # how much the bot writes in its reports and answers
    owner: str | None = None  # user id; runs use the owner's connector credentials. None = shared (legacy)
    created_at: str | None = None
    updated_at: str | None = None

    @field_validator("schedule")
    @classmethod
    def _cron(cls, v: str | None) -> str | None:
        from croniter import croniter

        v = " ".join((v or "").split()) or None
        if v and (len(v.split()) != 5 or not croniter.is_valid(v)):
            raise ValueError(f"schedule must be a 5-field cron expression like '0 7 * * 1-5', not {v!r}")
        return v

    @model_validator(mode="after")
    def _trigger(self) -> "BotSpec":
        self.trigger = "schedule" if self.schedule else "manual"
        return self

    @field_validator("tools")
    @classmethod
    def _unique(cls, v: list[str]) -> list[str]:
        return list(dict.fromkeys(v))

    def to_skill_md(self) -> str:
        meta = {
            "title": self.title,
            "model": self.model,
            "trigger": self.trigger,
            "schedule": self.schedule or "",
            "max-steps": str(self.max_steps),
            "verbosity": self.verbosity,
            "owner": self.owner or "",
            "created-at": self.created_at or "",
            "updated-at": self.updated_at or "",
        }
        front = {
            "name": self.name,
            "description": self.description,
            "allowed-tools": " ".join(self.tools),
            "metadata": {k: v for k, v in meta.items() if v},
        }
        return f"---\n{yaml.safe_dump(front, sort_keys=False, allow_unicode=True, width=100)}---\n\n{self.instructions.strip()}\n"

    @classmethod
    def from_skill_md(cls, text: str) -> "BotSpec":
        m = _FRONTMATTER.match(text)
        if not m:
            raise ValueError("SKILL.md must start with a YAML frontmatter block")
        front = yaml.safe_load(m.group(1)) or {}
        meta = front.get("metadata") or {}
        return cls(
            name=front.get("name", ""),
            title=meta.get("title") or front.get("name", ""),
            description=front.get("description", ""),
            instructions=m.group(2).strip(),
            model=meta.get("model") or get_settings().default_bot_model,
            tools=(front.get("allowed-tools") or "").split(),
            schedule=meta.get("schedule") or None,
            max_steps=int(meta.get("max-steps", 25)),
            verbosity=meta.get("verbosity") or "short",
            owner=meta.get("owner") or None,
            created_at=meta.get("created-at"),
            updated_at=meta.get("updated-at"),
        )


def _path(name: str) -> Path:
    if not re.match(NAME_RE, name):
        raise ValueError(f"invalid bot name {name!r}")
    return get_settings().bots_dir / name / "SKILL.md"


def visible_to(bot: BotSpec, user: str | None) -> bool:
    return bot.owner is None or bot.owner == user


def list_bots(user: str | None = None) -> list[BotSpec]:
    """All bots, or only those `user` may see (their own plus shared ones without an owner)."""
    root = get_settings().bots_dir
    specs = []
    for p in sorted(root.glob("*/SKILL.md")):
        try:
            specs.append(BotSpec.from_skill_md(p.read_text()))
        except Exception:
            continue  # skip broken files rather than break the listing
    return specs if user is None else [b for b in specs if visible_to(b, user)]


def get_bot(name: str) -> BotSpec | None:
    p = _path(name)
    return BotSpec.from_skill_md(p.read_text()) if p.exists() else None


def raw_skill_md(name: str) -> str | None:
    p = _path(name)
    return p.read_text() if p.exists() else None


def save_bot(spec: BotSpec) -> BotSpec:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    existing = get_bot(spec.name)
    spec = spec.model_copy(update={"created_at": existing.created_at if existing else now, "updated_at": now})
    p = _path(spec.name)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(spec.to_skill_md())
    return spec


def delete_bot(name: str) -> bool:
    p = _path(name)
    if not p.exists():
        return False
    p.unlink()
    try:
        p.parent.rmdir()
    except OSError:
        pass  # directory holds other files (e.g. scripts/ or references/)
    return True
