# Agentic Bots (POC)

Create bots by chatting with them. In the **Main** session you describe a task, for example *"create a bot to get the daily AI news from hackernews"*. A builder agent turns that into a bot definition. Each bot then gets its own **session**: press **Run now** to execute the task, and ask follow-up questions in the same conversation.

```
Angular :4200 ──REST/SSE──> FastAPI :8000 ──> pydantic-ai ──> LiteLLM (external, models)
                                 │                  │
                        bots/*/SKILL.md        MCP (streamable HTTP)
                        data/bots.db                ▼
                                       agentgateway :3000/mcp   (admin UI :15000/ui)
                                         ├─ hackernews → connectors :8100/hackernews/mcp
                                         └─ web        → connectors :8100/web/mcp
```

## Quick start

Prerequisites: `uv`, Docker, and Node 22/24 (Angular doesn't support Node 25; the Makefile picks up Homebrew `node@24` if it is installed).

```bash
cp .env.example .env      # set LITELLM_BASE_URL, LITELLM_API_KEY and a tool-capable model alias
make install
make up                   # agentgateway + connectors (docker compose)
make llm-smoke            # should print "pong"
make smoke                # lists tools through the gateway
make dev                  # backend :8000 + frontend :4200
```

Open http://localhost:4200 and ask Main to create a bot. The repo ships with **no bots**: every bot is written by the builder agent from your chat, as `bots/<name>/SKILL.md`.

## Open standards

| What | Standard | Where |
|---|---|---|
| Bot definition | [Agent Skills](https://agentskills.io) `SKILL.md` (frontmatter `name`, `description`, `allowed-tools`, `metadata`, plus markdown instructions) | `bots/<name>/SKILL.md` |
| Tools | [MCP](https://modelcontextprotocol.io) streamable HTTP | every connector is an MCP server |
| Tool registry / gateway | [agentgateway](https://agentgateway.dev) | `gateway/config.yaml` |
| App → tools config | `mcp.json` (`mcpServers`) | `config/mcp.json`; the same file works in Claude Code, VS Code, goose… |
| Models | OpenAI-compatible API | LiteLLM via `.env` |
| Events | [CloudEvents](https://cloudevents.io) 1.0 JSON | `GET /api/events` (SSE) |

Bots are plain files: you can edit `bots/*/SKILL.md` by hand or keep them in git. Only runtime data is stored in SQLite (`data/bots.db`): sessions, messages, agent history and runs.

## Layout

```
backend/     FastAPI + pydantic-ai     src/app/{main,config,llm,gateway,spec,models,db,events,history}.py
             agents/{builder,builder_tools,bot_runner,runtime}.py, tests/
connectors/  MCP servers (one container) src/connectors/{hackernews,web,search_providers,app}.py
gateway/     agentgateway config
config/      mcp.json (points the app at the gateway)
bots/        SKILL.md bot definitions (created by the builder; empty in git)
frontend/    Angular (standalone, signals, zoneless)
scripts/     mcp_smoke.py
```

## How it works

- **Builder (Main session).** It uses `BUILDER_MODEL` and has the tools `list_tools`, `list_models`, `list_bots`, `get_bot`, `create_bot`, `update_bot` and `run_bot`. Validation errors (unknown tool, model not allowed, bad name) are returned to the model so it can fix them itself.
- **Bot runs.**
  - The agent is built from `SKILL.md`, using its model and only its `allowed-tools`, with the gateway toolset filtered client-side.
  - A run adds a hidden *"Execute your task now"* turn, and the result appears as a message tagged with the run id.
  - Follow-up questions reuse the same agent history.
  - Tool outputs from older turns are truncated so the history stays small.
- **Events.**
  - `bot.created`, `bot.updated`, `bot.deleted`, `run.started`, `run.progress` and `run.finished` are published as CloudEvents on `/api/events`.
  - The UI uses them for the live run activity, and they are the hook for event triggers and delivery later on.

## Adding a connector

Each connector is one more target in `gateway/config.yaml`. Its tools then appear as `<target>_<tool>` in the Tools page and in the builder's `list_tools`.

```yaml
mcp:
  targets:
  - name: github                      # remote MCP server
    mcp: { host: https://api.githubcopilot.com/mcp/ }
  - name: fetch                       # any stdio MCP server
    stdio: { cmd: uvx, args: ["mcp-server-fetch"] }
```

To write your own connector, add an `MCPServer` module in `connectors/src/connectors/` and mount it in `app.py`. Connector secrets (search API keys, tokens) go **only** to the connector containers in `docker-compose.yml`, never to the app or the agents.

Note that agentgateway fails `initialize` when one target is down, so keep targets healthy or remove them.

## Web search

The search provider is set with `WEB_SEARCH_PROVIDER` in the connectors container: `duckduckgo` (the default, no key needed), `tavily` (`TAVILY_API_KEY`), `brave` (`BRAVE_API_KEY`) or `searxng` (`SEARXNG_URL`).

## Tests

```bash
make test        # unit tests: SKILL.md round trip, validation, history compaction, runtime with TestModel
make test-all    # plus live tests through the gateway (requires `make up`)
make e2e         # functional test of the first use case (requires .env + `make up`)
```

`make e2e` runs the real journey on a temporary database and bots directory:
1. It sends *"create a bot to get the daily AI news from hackernews"* to the builder.
2. It asserts the builder created a bot with hackernews tools.
3. It runs the bot and asserts the digest links its sources.
4. It asks a follow-up question that must use the web tools.

The generated `SKILL.md`, `digest.md` and `follow-up.md` are saved in `data/e2e/` for inspection. The test skips itself when LiteLLM or the gateway isn't reachable.

## Next iterations

- **Multi-tenant gateway.**
  - Turn on `jwtAuth` in `gateway/config.yaml` and have the app issue a JWT per run, with `bot` and `tenant` claims.
  - Enforce per-bot tool access with `mcpAuthorization` CEL rules in the gateway instead of the client-side filter.
  - Hold connector credentials per tenant.
  - The commented blocks in `gateway/config.yaml` show the shape.
- **Scheduling:** `metadata.schedule` (cron) → APScheduler, later Temporal.
- **Bot state:** a key-value tool, for example to skip HN stories already reported.
- **Governance:** a LiteLLM virtual key and budget per bot; OpenTelemetry traces (pydantic-ai, agentgateway and LiteLLM all emit them) → Langfuse.
- **Delivery:** Slack, Teams or email, triggered by `run.finished`.
- **Resilience:** move the chat stream to a background task so a closed browser tab doesn't lose the turn.
