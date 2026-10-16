"""Builder tools: create, inspect and run bots.

Plain async functions (docstrings are what the LLM sees), so they can also be exposed as an
MCP server behind agentgateway later.
"""

from pydantic import ValidationError

from app import events, gateway, spec
from app.agents import bot_runner
from app.config import get_settings
from app.llm import list_models as _list_models


async def list_tools() -> list[dict]:
    """List the tools bots can be given (from the MCP gateway): name, connector, description.

    Grant whole connectors by name (e.g. "web") or single tools (e.g. "hackernews_hn_item").
    """
    return [{k: t[k] for k in ("name", "connector", "description", "read_only")} for t in await gateway.catalog()]


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
        for b in spec.list_bots()
    ]


async def get_bot(name: str) -> str:
    """Get a bot's full definition (its SKILL.md)."""
    return spec.raw_skill_md(name) or f"No bot named '{name}'."


async def _validate_and_save(data: dict, *, created: bool) -> str:
    names = [t["name"] for t in await gateway.catalog()]
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
    bot = spec.save_bot(bot)
    bot_runner.ensure_session(bot)
    events.publish("bot.created" if created else "bot.updated", {"name": bot.name, "title": bot.title}, subject=bot.name)
    verb = "Created" if created else "Updated"
    return f"{verb} bot '{bot.name}' with tools {bot.tools} on model {bot.model}. Saved as bots/{bot.name}/SKILL.md."


async def create_bot(
    name: str,
    title: str,
    description: str,
    instructions: str,
    tools: list[str],
    model: str | None = None,
) -> str:
    """Create a new bot.

    Args:
        name: lowercase slug with hyphens, e.g. "daily-ai-news".
        title: short human title, e.g. "Daily AI News".
        description: one or two sentences: what the bot does and when it is useful.
        instructions: the bot's full task description in markdown (see the guidance in your instructions).
        tools: tool or connector names from list_tools; grant only what the task needs.
        model: model name from list_models; omit for the default.
    """
    if spec.get_bot(name):
        return f"A bot named '{name}' already exists. Use update_bot or pick another name."
    return await _validate_and_save(
        {"name": name, "title": title, "description": description, "instructions": instructions,
         "tools": tools, "model": model},
        created=True,
    )


async def update_bot(
    name: str,
    title: str | None = None,
    description: str | None = None,
    instructions: str | None = None,
    tools: list[str] | None = None,
    model: str | None = None,
) -> str:
    """Update an existing bot. Only the fields you pass change; `instructions` replaces the whole text."""
    bot = spec.get_bot(name)
    if not bot:
        return f"No bot named '{name}'."
    data = bot.model_dump()
    changes = {"title": title, "description": description, "instructions": instructions, "tools": tools, "model": model}
    data.update({k: v for k, v in changes.items() if v is not None})
    return await _validate_and_save(data, created=False)


async def run_bot(name: str) -> str:
    """Start a run of a bot now. It runs in the background; results appear in the bot's own session."""
    bot = spec.get_bot(name)
    if not bot:
        return f"No bot named '{name}'."
    try:
        run = bot_runner.start_run(bot)
    except RuntimeError as e:
        return str(e)
    return f"Started run {run.id} of '{bot.title}'. The result will appear in the '{bot.title}' session in the sidebar."


TOOLS = [list_tools, list_models, list_bots, get_bot, create_bot, update_bot, run_bot]
