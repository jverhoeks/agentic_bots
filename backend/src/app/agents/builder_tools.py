"""Builder tools: create, inspect and run bots.

Plain async functions (docstrings are what the LLM sees), so they can also be exposed as an
MCP server behind agentgateway later.
"""

from pydantic import ValidationError
from pydantic_ai.toolsets import FunctionToolset

from app import audit, events, gateway, scheduler, spec
from app.auth import current_user
from app.agents import bot_runner
from app.config import get_settings
from app.db import db
from app.llm import list_models as _list_models
from app.models import Message

SELF = ("bot-chat", "bot-run")  # `via` of a bot changing itself: the user gets a heads-up in the bot's chat


def _mine(name: str) -> spec.BotSpec | None:
    """A bot the current user may see and change (their own, or a shared one without owner)."""
    bot = spec.get_bot(name)
    return bot if bot and spec.visible_to(bot, current_user.get()) else None


async def list_tools() -> list[dict]:
    """List the tools bots can be given (from the MCP gateway): name, connector, description.

    Grant whole connectors by name (e.g. "web") or single tools (e.g. "hackernews_hn_item").
    """
    return [{k: t[k] for k in ("name", "connector", "description", "read_only")} for t in await gateway.catalog(user=current_user.get())]


async def list_models() -> dict:
    """List the LLM models a bot can use, and the default."""
    try:
        models = await _list_models()
    except Exception as e:
        models, note = [], f"could not list models: {e}"
    else:
        note = None
    return {"default": get_settings().default_bot_model, "models": models, "note": note}


async def list_bots() -> list[dict]:
    """List existing bots with their tools and model."""
    return [
        {"name": b.name, "title": b.title, "description": b.description, "model": b.model, "tools": b.tools}
        for b in spec.list_bots(current_user.get())
    ]


async def get_bot(name: str) -> str:
    """Get a bot's full definition (its SKILL.md)."""
    return spec.raw_skill_md(name) if _mine(name) else f"No bot named '{name}'."


def changes(before: spec.BotSpec, after: spec.BotSpec) -> list[str]:
    """What changed, in words, for the heads-up."""
    out = []
    added, removed = sorted(set(after.tools) - set(before.tools)), sorted(set(before.tools) - set(after.tools))
    if added:
        out.append(f"**new tools** {', '.join(f'`{t}`' for t in added)}")
    if removed:
        out.append(f"removed tools {', '.join(f'`{t}`' for t in removed)}")
    if after.schedule != before.schedule:
        out.append(f"schedule `{before.schedule or 'none'}` → `{after.schedule or 'none'}`")
    for field in ("model", "verbosity", "title"):
        if getattr(after, field) != getattr(before, field):
            out.append(f"{field} `{getattr(before, field)}` → `{getattr(after, field)}`")
    if after.description != before.description:
        out.append("description")
    if after.instructions != before.instructions:
        out.append("instructions")
    return out


def _heads_up(bot: spec.BotSpec, changed: list[str], via: str) -> dict:
    """A bot changed itself: tell the user in its chat (a `notice` message), whatever started the turn."""
    where = "during a run" if via == "bot-run" else "from this chat"
    text = f"🔔 **{bot.title} updated itself** {where}: {'; '.join(changed)}. See SKILL.md for the full definition."
    with db() as s:
        m = Message(session_id=bot_runner.session_id_for(bot.name), role="notice", content=text)
        s.add(m)
        s.commit()
        s.refresh(m)
        return {"id": m.id, "role": m.role, "content": m.content, "tool_calls": [], "created_at": m.created_at.isoformat()}


async def _validate_and_save(data: dict, *, created: bool, via: str = "builder", before: spec.BotSpec | None = None) -> str:
    names = [t["name"] for t in await gateway.catalog(user=current_user.get())]
    tools = gateway.expand_tool_refs(data.get("tools") or [], names)
    unknown = [r for r in data.get("tools") or [] if not gateway.expand_tool_refs([r], names)]
    if unknown:
        return f"Unknown tools {unknown}. Call list_tools and use exact names or connector names."
    data["tools"] = tools
    data["model"] = data.get("model") or get_settings().default_bot_model
    try:
        models = await _list_models()
    except Exception:
        models = []
    if models and data["model"] not in models:
        return f"Model '{data['model']}' is not available. Choose one of: {models}"
    try:
        bot = spec.BotSpec(**data)
    except ValidationError as e:
        return f"Invalid bot definition, fix and retry:\n{e}"
    changed = changes(before, bot) if before else []
    if before and not changed:
        return f"Nothing changed: '{bot.name}' already has this definition."
    bot = spec.save_bot(bot)
    bot_runner.ensure_session(bot)
    data = {"name": bot.name, "title": bot.title, "via": via, "changes": changed}
    if via in SELF:
        data["notice"] = _heads_up(bot, changed, via)
    events.publish("bot.created" if created else "bot.updated", data, subject=bot.name)
    audit.record("bot.created" if created else "bot.updated", bot=bot.name,
                 detail={"tools": bot.tools, "model": bot.model, "schedule": bot.schedule, "via": via, "changes": changed})
    verb = "Created" if created else "Updated"
    when = (f" It runs by itself on '{bot.schedule}' ({get_settings().schedule_timezone}), next at "
            f"{scheduler.next_run(bot.schedule)}." if bot.schedule else "")
    return f"{verb} bot '{bot.name}' with tools {bot.tools} on model {bot.model}.{when} Saved as bots/{bot.name}/SKILL.md."


async def create_bot(
    name: str,
    title: str,
    description: str,
    instructions: str,
    tools: list[str],
    model: str | None = None,
    verbosity: spec.Verbosity = "short",
    schedule: str | None = None,
) -> str:
    """Create a new bot.

    Args:
        name: lowercase slug with hyphens, e.g. "daily-ai-news".
        title: short human title, e.g. "Daily AI News".
        description: one or two sentences: what the bot does and when it is useful.
        instructions: the bot's full task description in markdown (see the guidance in your instructions).
        tools: tool or connector names from list_tools; grant only what the task needs.
        model: model name from list_models; omit for the default.
        verbosity: how much the bot writes: "short" (default), "medium" or "verbose".
        schedule: cron expression (5 fields, in the platform's SCHEDULE_TIMEZONE) to run it by itself, e.g.
            "0 7 * * 1-5" (07:00 on weekdays); omit for manual runs only.
    """
    if spec.get_bot(name):
        return f"A bot named '{name}' already exists. Use update_bot or pick another name."
    return await _validate_and_save(
        {"name": name, "title": title, "description": description, "instructions": instructions,
         "tools": tools, "model": model, "verbosity": verbosity, "schedule": schedule, "owner": current_user.get()},
        created=True,
    )


async def update_bot(
    name: str,
    title: str | None = None,
    description: str | None = None,
    instructions: str | None = None,
    tools: list[str] | None = None,
    model: str | None = None,
    verbosity: spec.Verbosity | None = None,
    schedule: str | None = None,
    *,
    via: str = "builder",
) -> str:
    """Update an existing bot. Only the fields you pass change; `instructions` replaces the whole text.

    `schedule` is a cron expression (see create_bot); pass "" to stop the scheduled runs.
    """
    bot = _mine(name)
    if not bot:
        return f"No bot named '{name}'."
    data = bot.model_dump()
    changes = {"title": title, "description": description, "instructions": instructions, "tools": tools, "model": model,
               "verbosity": verbosity, "schedule": schedule}
    data.update({k: v for k, v in changes.items() if v is not None})
    return await _validate_and_save(data, created=False, via=via, before=bot)


async def run_bot(name: str) -> str:
    """Start a run of a bot now. It runs in the background; results appear in the bot's own session."""
    bot = _mine(name)
    if not bot:
        return f"No bot named '{name}'."
    try:
        run = bot_runner.start_run(bot, actor=current_user.get())
    except RuntimeError as e:
        return str(e)
    return f"Started run {run.id} of '{bot.title}'. The result will appear in the '{bot.title}' session in the sidebar."


TOOLS = [list_tools, list_models, list_bots, get_bot, create_bot, update_bot, run_bot]


def self_edit_toolset(name: str, via: str = "bot-chat") -> FunctionToolset:
    """A bot reads and changes its own definition, in its chat (`bot-chat`) or during a run (`bot-run`), with the
    builder's checks (the acting user must be allowed to change the bot; tools and model are validated). Every
    change is audited and announced to the user in the bot's chat (see `_heads_up`)."""
    tools = FunctionToolset()

    @tools.tool_plain
    async def get_my_definition() -> str:
        """Get your own definition (SKILL.md): description, instructions, tools, model and verbosity."""
        return await get_bot(name)

    @tools.tool_plain
    async def list_available_tools() -> list[dict]:
        """List the tools you could be given (from the MCP gateway), before changing your tools."""
        return await list_tools()

    @tools.tool_plain
    async def update_my_definition(
        title: str | None = None,
        description: str | None = None,
        instructions: str | None = None,
        tools: list[str] | None = None,
        model: str | None = None,
        verbosity: spec.Verbosity | None = None,
        schedule: str | None = None,
    ) -> str:
        """Change your own definition (the user is told what changed). Only the fields you pass change.

        Args:
            instructions: the full revised task instructions in markdown (replaces the whole text: keep
                everything the user did not ask to change).
            tools: the complete new list of tool or connector names (replaces the list).
            model: a model name; verbosity: "short", "medium" or "verbose".
            schedule: cron expression (5 fields, platform time zone) for runs by yourself, e.g. "0 7 * * 1-5";
                "" stops them.
        """
        result = await update_bot(name, title=title, description=description, instructions=instructions, tools=tools,
                                  model=model, verbosity=verbosity, schedule=schedule, via=via)
        if not result.startswith("Updated"):
            return result
        return result + (" The user has been notified. It applies from the next run."
                         if via == "bot-run" else " The user sees a notice. It applies from the user's next message.")

    return tools
