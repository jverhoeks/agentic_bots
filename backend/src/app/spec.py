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
from pydantic import BaseModel, Field, field_validator

from app.config import get_settings

NAME_RE = r"^[a-z0-9]+(-[a-z0-9]+)*$"
_FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?(.*)\Z", re.S)


class BotSpec(BaseModel):
    name: str = Field(pattern=NAME_RE, min_length=3, max_length=64)
    title: str = Field(min_length=1, max_length=80)
    description: str = Field(min_length=1, max_length=1024)
    instructions: str = Field(min_length=20)
    model: str
    tools: list[str] = []
    trigger: Literal["manual"] = "manual"  # later: "cron" (+ metadata.schedule) | "event"
    max_steps: int = Field(25, ge=1, le=100)
    created_at: str | None = None
    updated_at: str | None = None

    @field_validator("tools")
    @classmethod
    def _unique(cls, v: list[str]) -> list[str]:
        return list(dict.fromkeys(v))

    def to_skill_md(self) -> str:
        meta = {
            "title": self.title,
            "model": self.model,
            "trigger": self.trigger,
            "max-steps": str(self.max_steps),
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
            trigger=meta.get("trigger", "manual"),
            max_steps=int(meta.get("max-steps", 25)),
            created_at=meta.get("created-at"),
            updated_at=meta.get("updated-at"),
        )


def _path(name: str) -> Path:
    if not re.match(NAME_RE, name):
        raise ValueError(f"invalid bot name {name!r}")
    return get_settings().bots_dir / name / "SKILL.md"


def list_bots() -> list[BotSpec]:
    root = get_settings().bots_dir
    specs = []
    for p in sorted(root.glob("*/SKILL.md")):
        try:
            specs.append(BotSpec.from_skill_md(p.read_text()))
        except Exception:
            continue  # skip broken files rather than break the listing
    return specs


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
