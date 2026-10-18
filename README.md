# Agentic Bots

> **Preview.** A proof of concept, under active development. See [License](#license).

**Build bots by chatting with them.** You describe a recurring task in plain language, for example *"create a bot to get the daily AI news from Hacker News"*. A builder agent turns it into a bot: a portable `SKILL.md` file with instructions, a model and the tools it may use. Each bot has its own chat. There it runs its task, answers follow-up questions, and changes itself when you ask.

- 💬 **Chat-built bots.** No forms or YAML to write: the builder reads the tool catalog, grants only the tools the task needs and writes the instructions.
- 🔁 **Runs, follow-ups, schedules.** Press **Run now**, ask about the results in the same conversation, or say *"every weekday at 7"* and the bot runs by itself.
- 🛠 **Self-improving, with a heads-up.** A bot can update its own definition, from its chat or during a run. Each change is validated and audited, and you get a 🔔 notice of what changed.
- 🧰 **Tools through one MCP gateway.** [agentgateway](https://agentgateway.dev) sits in front of every connector (Hacker News, web search, SaaS apps). It enforces per-bot tool access and per-user identity.
- 🤖 **Any model provider.** LiteLLM, OpenAI (or any OpenAI-compatible server), Anthropic and OpenRouter. You can mix providers per bot.
- 🤝 **Agent-to-agent.** Bots call each other. Every bot is also an A2A 1.0 agent.
- 🔍 **Observable.** OpenTelemetry traces for every chat turn and run, a CloudEvents bus, an audit log and an admin page.
- 📦 **Open standards throughout:** Agent Skills, MCP, A2A, CloudEvents, OpenTelemetry, OpenID Connect.

## Contents

- [Quick start](#quick-start)
- [Models](#models)
- [How it works](#how-it-works)
- [Architecture](#architecture)
- [Tools and connectors](#tools-and-connectors)
- [Users and identity](#users-and-identity)
- [Observability and admin](#observability-and-admin)
- [Security](#security)
- [Configuration](#configuration)
- [Development](#development)
- [Roadmap](#roadmap)
- [License](#license)

## Quick start

**You need:**
- `uv`
- Docker
- Node 22 or 24. Angular doesn't support Node 25; the Makefile picks up Homebrew `node@24` if it is installed.
- Access to at least one [model provider](#models).

```bash
cp .env.example .env      # pick a model provider and set its key (see Models)
make install
make up                   # secrets + agentgateway + connectors (docker compose)
make llm-smoke            # lists models and should print "pong"
make smoke                # lists tools through the gateway
make dev                  # backend :8000 + frontend :4200
```

1. Open http://localhost:4200.
2. Create your account. The first account registers itself and is the admin.
3. Ask **Main** for a bot.

New here? Click **Tutorial** in the sidebar. It walks you through the whole loop:
1. It builds a small bot, `tutorial-hn`, that lists the top AI stories on Hacker News.
2. It runs the bot and asks a follow-up question.
3. It changes the bot from its chat and puts it on a schedule.
4. It shows the traces.

Each step has a button that does it for you.

### All in Docker

```bash
cp .env.example .env
make up-all               # gateway + connectors + backend + UI
```

Open http://localhost:8080 (bound to localhost only). Bots (`bots/`) and runtime data (`data/`) are mounted from the host, so they survive rebuilds. To reach a model server on your machine from the container, use `http://host.docker.internal:<port>`. Stop everything with `make down`.

## Models

The backend uses [pydantic-ai](https://ai.pydantic.dev) and supports four providers. Set the key of each provider you use in `.env`.

| Provider | `LLM_PROVIDER` | Settings | Model names (examples) |
|---|---|---|---|
| [LiteLLM](https://docs.litellm.ai) proxy (default) | `litellm` | `LITELLM_BASE_URL`, `LITELLM_API_KEY` | your proxy's aliases, e.g. `claude-sonnet` |
| OpenAI, or any OpenAI-compatible server (Ollama, vLLM, Azure, ...) | `openai` | `OPENAI_API_KEY`, `OPENAI_BASE_URL` (empty = api.openai.com) | `gpt-5`, `llama3` |
| Anthropic | `anthropic` | `ANTHROPIC_API_KEY` | `claude-sonnet-5-5`, `claude-opus-5-5` |
| [OpenRouter](https://openrouter.ai) | `openrouter` | `OPENROUTER_API_KEY` | `anthropic/claude-sonnet-5-5`, `openai/gpt-5` |

- **Bare model names** (`claude-sonnet`) go to `LLM_PROVIDER`.
- **`<provider>:<model>`** picks another configured provider, so providers can be mixed:
  ```bash
  LLM_PROVIDER=litellm
  BUILDER_MODEL=anthropic:claude-sonnet-5-5       # the builder talks to Anthropic directly
  DEFAULT_BOT_MODEL=openrouter:openai/gpt-5       # new bots default to OpenRouter
  ```
  A prefix that isn't a provider stays part of the name (`ollama:llama3` is sent to the default provider as is).
- **Model list.** The model picker and the builder's `list_models` show the models of every configured provider. Models of the default provider appear without a prefix; other providers' models are prefixed. A provider that is down is skipped. `ALLOWED_MODELS` (a comma list) replaces the list. Set it for OpenRouter, which lists hundreds of models.
- The builder needs a model that can **call tools**. `make llm-smoke` shows the configured providers and the models, and pings `BUILDER_MODEL`.

## How it works

```
 You ──"daily AI news from HN"──▶ Main (builder agent)
                                     │ list_tools, list_models, create_bot
                                     ▼
                              bots/daily-ai-news/SKILL.md
                                     │
         ┌───────────────────────────┼─────────────────────────────┐
     Run now / schedule        follow-up questions          "make it shorter"
         ▼                           ▼                             ▼
   bot agent + its tools ──▶ answer in the bot's chat ◀── updates its own SKILL.md (🔔 notice)
```

### Bots are files

A bot is an [Agent Skills](https://agentskills.io) `SKILL.md`. The frontmatter holds the name, description, tools and settings; the body holds the instructions.

```markdown
---
name: daily-ai-news
description: Daily digest of the most relevant AI news on Hacker News.
allowed-tools: hackernews_hn_top_stories hackernews_hn_item web_web_search web_fetch_url
metadata:
  title: Daily AI News
  model: claude-sonnet
  schedule: "0 7 * * 1-5"
  verbosity: short
  owner: alice
---
# Goal
...
```

You can edit `bots/*/SKILL.md` by hand. Bots are your data, like `data/`, so `bots/` is git-ignored and the repo ships with no bots. Keep them in a repo of your own if you want history.

### The builder (Main)

The builder uses `BUILDER_MODEL` and has these tools: `list_tools`, `list_models`, `list_bots`, `get_bot`, `create_bot`, `update_bot` and `run_bot`. It grants only the tools the task needs, plus search and fetch tools when the output invites follow-up research. Validation errors (unknown tool, model not allowed, bad name) go back to the model so it can fix them itself.

### Runs and follow-ups

- **The agent.** Each run builds the agent from `SKILL.md`: its model, its instructions and only its `allowed-tools`.
- **A run** adds a hidden *"Execute your task now"* turn. The result appears in the bot's chat, tagged with the run id.
- **Follow-ups** reuse the same agent history. Tool outputs of older turns are truncated so the history stays small.
- **Length.** `metadata.verbosity` (`short` by default, `medium` or `verbose`) sets how much a bot writes. Change it with the selector in the bot header.

### Schedules

Ask for one (*"every weekday at 7"*) and the builder or the bot sets `metadata.schedule`, a cron expression in `SCHEDULE_TIMEZONE` (default UTC). The backend checks schedules every 30 seconds. Runs missed while the backend is down are not caught up.

### Self-update with a heads-up

A bot can change its own definition:
- in its chat, when you ask (*"also cover GitHub trending"*);
- on its own, in a chat or during a run, when that clearly serves your goal (for example, a source keeps failing).

Every change:
- gets the same checks as the builder;
- is audited;
- posts a 🔔 **notice** in the bot's chat that lists what changed, with new tools in bold;
- shows a banner in the UI.

Requests from other agents (A2A) can never change a bot, and neither can instructions found in tool results, web pages or e-mails.

### Memory

- **Notes.** Each bot keeps key/value notes itself with `memory_save`, `memory_get` and `memory_delete` (always available).
- **Last run.** The platform also records each bot's last run: its output and the tools it used.
- **Storage.** Memory is plain text in the database. It is added to the instructions as untrusted data.
- **UI.** The bot's **Memory** tab shows it, with **Forget** per key or for everything.

### Delivery

Delivery (Slack, e-mail, ...) is a tool like any other. Granting a connector that sends or changes things is your approval for the bot to use it, also in scheduled runs.

### Agent-to-agent (A2A)

- **Calling other bots.** Grant the built-in `a2a` connector and a bot can:
  - list the other bots;
  - ask one and wait for the answer (`a2a_send_message`);
  - start a task and poll it (`a2a_start_task` / `a2a_get_task`);
  - read another bot's last result (`a2a_get_last_result`).
- **Each request is a run** of the target bot (trigger `a2a:<caller>`), visible in that bot's session.
- **Limits.** Loops and self-calls are refused, the depth is capped at 2, and bots reach only bots of the same owner.
- **Every bot is an A2A 1.0 agent** behind the gateway: `http://localhost:3000/a2a/<bot>/`, with its Agent Card at `.../.well-known/agent-card.json`.
- **External agents.** Agents listed in `config/a2a-agents.json` can be called with `a2a_remote_list_agents` and `a2a_remote_send_message`.

More in [docs/plan-a2a-connectors.md](docs/plan-a2a-connectors.md).

## Architecture

```
Angular :4200 ──REST/SSE──▶ FastAPI :8000 ──▶ pydantic-ai ──▶ LiteLLM │ OpenAI │ Anthropic │ OpenRouter
                                 │                  │
                        bots/*/SKILL.md        MCP (streamable HTTP) + per-user JWT
                        data/bots.db                ▼
                        (or PostgreSQL)    agentgateway :3000/mcp, /a2a   (admin UI :15000/ui)
                                             ├─ hackernews → connectors :8100/hackernews/mcp
                                             ├─ web        → connectors :8100/web/mcp
                                             └─ apps       → apps-broker :8120 → open-connector (per-user SaaS)
gateway ──OTLP──▶ backend /otlp ──▶ traces in the app DB (+ an OTLP collector when set)
```

| What | Standard | Where |
|---|---|---|
| Bot definition | [Agent Skills](https://agentskills.io) `SKILL.md` | `bots/<name>/SKILL.md` |
| Tools | [MCP](https://modelcontextprotocol.io), streamable HTTP | every connector is an MCP server |
| Tool registry / gateway | [agentgateway](https://agentgateway.dev) | `gateway/config.yaml` |
| App → tools config | `mcp.json` (`mcpServers`) | `config/mcp.json`; the same file works in Claude Code, VS Code, goose… |
| Models | OpenAI-compatible API, Anthropic Messages API | `.env` (see [Models](#models)) |
| Agent-to-agent | [A2A](https://a2a-protocol.org) 1.0 | `/a2a/<bot>/` on the gateway |
| Events | [CloudEvents](https://cloudevents.io) 1.0 JSON | `GET /api/events` (SSE), optional HTTP sink |
| Traces | [OpenTelemetry](https://opentelemetry.io) (GenAI semconv), OTLP | app DB; optionally an OTLP collector |
| Login | [OpenID Connect](https://openid.net/connect/) (optional) | `OIDC_*` in `.env` |

The database stores runtime data only: sessions, messages, agent history, runs, memory, audit and traces. It is SQLite (`data/bots.db`) by default. For PostgreSQL:
1. Start it with `docker compose --profile postgres up -d postgres`.
2. Set `DATABASE_URL=postgresql+psycopg://bots:<password>@localhost:5432/bots`. The password is `POSTGRES_PASSWORD` in `secrets/postgres.env`, generated at random once by `make secrets-init`.

### Repository layout

```
backend/     FastAPI + pydantic-ai     src/app/{main,config,llm,gateway,spec,models,db,events,history}.py
                                       src/app/{auth,oidc,tokens,admin,audit,guardrails,telemetry,tracestore,memory,scheduler,a2a_server}.py
             agents/{builder,builder_tools,bot_runner,runtime,a2a}.py, tests/
connectors/  MCP servers (one container) src/connectors/{hackernews,web,search_providers,app,apps_broker}.py
gateway/     agentgateway config
config/      mcp.json (points the app at the gateway), a2a-agents.json
bots/        SKILL.md bot definitions (git-ignored)
frontend/    Angular (standalone, signals, zoneless)
docs/        design notes
scripts/     mcp_smoke.py, open_connector.py
```

## Tools and connectors

agentgateway combines all its targets behind one endpoint. Tool names get the target as a prefix (`hackernews_hn_top_stories`). Built-in connectors:

| Target | Tools | Notes |
|---|---|---|
| `hackernews` | `hn_top_stories`, `hn_item` | Algolia HN API, no key |
| `web` | `web_search`, `fetch_url` | pluggable search provider; fetches public pages only |
| `apps` | search, read the guide of, and execute SaaS actions | Gmail, GitHub, Slack, ... via the apps broker and open-connector, per user; no tools until you connect an account |
| `a2a` (in the app) | talk to other bots and agents | see [A2A](#agent-to-agent-a2a) |

### Adding a connector

Add a target to `gateway/config.yaml`. Its tools then appear on the Tools page and in the builder's `list_tools`.

```yaml
mcp:
  targets:
  - name: github                      # remote MCP server
    mcp: { host: https://api.githubcopilot.com/mcp/ }
  - name: fetch                       # any stdio MCP server
    stdio: { cmd: uvx, args: ["mcp-server-fetch"] }
```

To write your own connector, add an `MCPServer` module in `connectors/src/connectors/` and mount it in `app.py`. The gateway runs with `failureMode: failOpen`, so a target that is down is skipped and the others keep working.

> Connector secrets (search API keys, tokens) go **only** to the connector containers, never to the app or the agents. Put them in `secrets/connectors.env`, not in `.env`.

### Web search

Set `WEB_SEARCH_PROVIDER` in `secrets/connectors.env`, then apply it with `docker compose up -d connectors`. The file is created from `connectors/connectors.env.example` by `make secrets-init`. Providers:
- `duckduckgo`: the default, no key needed;
- `tavily`: needs `TAVILY_API_KEY`;
- `brave`: needs `BRAVE_API_KEY`;
- `searxng`: needs `SEARXNG_URL`.

### SaaS apps (Gmail, GitHub, Slack, ...) via open-connector

[open-connector](https://github.com/oomol-lab/open-connector) (Apache-2.0) holds the SaaS accounts. A small **apps broker** (`connectors/src/connectors/apps_broker.py`) sits in front of it and is the `apps` target. The broker is the only thing that holds the open-connector admin token.

**Users connect their own accounts on the Connections page** (sidebar, "⇄ Connections"):

1. Search one of the ~1,600 apps and connect it with an API key or credentials, or with "Sign in with ..." (OAuth).
   - API keys go from the browser straight to the broker (`/connections/api`, proxied by the UI server). The backend only issues a 15-minute token that names the user (`POST /api/auth/connections-token`).
   - open-connector checks the credentials with the provider on save.
2. Grant `apps` to a bot. A user without connections sees no `apps` tools at all.
3. Disconnect on the same page. That revokes the user's runtime token.

How it works:

- The connection name is the user's login name. For each user the broker keeps an open-connector **runtime token** that may use **only that user's connections**. The tokens live in the broker's own volume (`apps-broker-data`).
- On `/mcp` the broker checks the gateway's JWT, then forwards the call with that user's runtime token. The app and the gateway never see a SaaS token.
- Actions are limited by `APPS_ALLOWED_ACTIONS` and `APPS_BLOCKED_ACTIONS` in `secrets/apps-broker.env`. By default the Gmail actions that send, reply, change filters or vacation settings, trash or delete are blocked.
- **OAuth needs an admin once per provider.** The admin sets up the OAuth client (e.g. Google, redirect URI `http://localhost:3010/oauth/callback`) in the open-connector console at http://localhost:3010, signing in with `OOMOL_CONNECT_ADMIN_TOKEN` from `secrets/open-connector.env`. Admins get a "Connector console" link on the Connections page. Until then the page offers the API-key option where the provider has one.
- `make connector-init` (part of `make up`) generates `secrets/open-connector.env` and `secrets/apps-broker.env`. No restart is needed when users connect.

See [docs/plan-a2a-connectors.md](docs/plan-a2a-connectors.md) §4.1.

## Users and identity

- **Login.** Two ways to sign in:
  - **Username and password:** a scrypt hash in the database and a signed HttpOnly cookie.
  - **Single sign-on** with any OpenID Connect provider, when `OIDC_ISSUER` and `OIDC_CLIENT_ID` are set.
    - It uses the code flow with PKCE.
    - Someone's first SSO login creates their account, named after `preferred_username` or the e-mail.
    - Accounts are linked by the provider's `sub`.
- **Accounts.**
  - The first account registers itself in the UI and is the **admin**.
  - After that, registration is closed. Add people with `make user USER_ID=<name>`; in Docker, use `docker compose exec backend python -m app.auth <name>`.
  - `make admin USER_ID=<name>` makes someone an admin. `ALLOW_SIGNUP=true` reopens registration.
- **Ownership.**
  - A bot belongs to the user who had it built (`metadata.owner`).
  - Each user sees only their own bots, sessions, runs and memory, and has their own Main session.
- **Gateway identity.** Every MCP and A2A request to agentgateway carries a short-lived ES256 JWT that the backend signs.
  - **Who:** `sub` is the **bot owner**, also for background and bot-to-bot runs.
  - **What:** a `tools` claim lists the tools the bot may use.
  - **Checked by the gateway:** agentgateway rejects requests without a valid token (`jwtAuth: strict`).
  - **Per-user SaaS access:** the gateway forwards the verified token to the apps broker, which checks it again and uses that user's own open-connector token. A client cannot pick another user.
  - **Keys:** `make jwt-init` creates `secrets/jwt-private.pem` (read by the backend only) and `secrets/jwks.json` (read by the gateway only).
- **External MCP/A2A clients** (Claude, goose, another agent) need a token:
  - Get one with `make token USER_ID=<name>` (valid 12h), or `POST /api/auth/token` while logged in.
  - Send it as `Authorization: Bearer <token>`.
  - A token reaches only that user's bots. The backend checks it too, because `:8000` can be reached without the gateway.

## Observability and admin

### Traces

Every chat turn, bot run, tool call, MCP request and A2A request is a trace in the app database, stored per user and session.
- **Where to see them.** Each answer in the chat has a **trace** link to a waterfall view. Users see their own traces; admins see everyone's (Admin › Traces).
- **Structure.** The turn or run is the root. The model calls, tool calls and the gateway's handling of each MCP call are nested under it.
- **Gateway spans.** agentgateway sends its spans over OTLP to the backend at `/otlp/v1/traces`. That endpoint is reachable only on the compose network and needs the shared `x-trace-token` from `secrets/trace-ingest.env`.
- **Content.** Prompts, answers, tool arguments and results are kept by default.
  - Secrets are redacted, and values are capped at `TRACE_MAX_ATTR_CHARS`.
  - Traces can hold e-mails and other personal data that admins can read. `TRACE_CONTENT=false` keeps only names, timings, tokens and errors.
- **Retention.** `TRACE_RETENTION_DAYS` (default 30) prunes old traces.
- **Export.** `OTEL_EXPORTER_OTLP_ENDPOINT` also sends everything to an OpenTelemetry collector. `TRACE_UI_URL` adds "Trace UI ↗" links (`<url>/trace/<id>`).

### Activity events

Every tool call, LLM call, MCP request and A2A request is a CloudEvent: `com.agenticbots.tool.called`, `llm.called`, `mcp.called` and `a2a.called`. MCP requests count also when they come from external clients of the gateway.
- **Contents.** Each event carries the user, session, bot, run, status and duration, plus a `traceparent` pointing at its span.
- **Sink.** `EVENT_SINK_URL` POSTs the events in batches (`application/cloudevents-batch+json`, with an optional bearer token in `EVENT_SINK_TOKEN`). Delivery is best effort.
- **Tool data.** Tool arguments and results are left out unless `EVENT_CONTENT=true`.
- **Browser.** `/api/events` sends the browser only the `bot.*` and `run.*` events: `bot.created`, `bot.updated`, `bot.deleted`, `run.started`, `run.progress` and `run.finished`.

### Audit log and admin page

The audit log records:
- logins, failed ones included;
- runs and every tool call;
- bot changes;
- A2A requests;
- guardrail hits.

Each entry has the user, who caused it, the bot, the run and the trace id.

The **Admin** page (for admins) has:
- **Activity:** the live audit log;
- **Traces** and **Runs:** for all users, with filters;
- **Users:** roles;
- links to the gateway UI and, if `TRACE_UI_URL` is set, the external trace UI.

## Security

- **Tool scoping in the gateway.** A bot can call only its own tools (`mcpAuthorization` on the token's `tools` claim). Requests are rate-limited per user.
- **Turn limits.** `MAX_PROMPT_CHARS`, `TURN_TOKEN_LIMIT`, `TURN_TOOL_CALL_LIMIT` and `max-steps` cap each turn.
- **Redaction and injection flags.** Secrets are redacted in tool results, answers and the audit log. Prompt-injection text in tool results is flagged to the model and to the audit log.
- **Public web pages only.** `web_fetch_url` resolves the host and refuses loopback, private, link-local (cloud metadata) and other non-public addresses. It checks every redirect and the address it actually connects to. A page with a prompt injection therefore cannot make a bot read the gateway admin API, other containers or services on your machine.
- **Secrets split by container.**
  - Model keys live in `.env`, which only the app reads.
  - Connector keys live in `secrets/connectors.env`, which only the connectors read.
  - The open-connector admin token and the per-user runtime tokens are held by the apps broker only (`secrets/apps-broker.env` and its volume).
  - Passwords and keys are generated at random once (`make secrets-init`, `make jwt-init`, `make connector-init`) and never overwritten.
- **Localhost only.** The Docker UI is bound to `127.0.0.1:8080`.
- **Approval.** Granting a tool that writes or sends is the approval to use it. A per-action approval queue is not planned yet; see [docs/plan-a2a-connectors.md](docs/plan-a2a-connectors.md) §7.

## Configuration

Settings are read from `.env`; `.env.example` documents all of them. The main ones:

| Setting | Default | |
|---|---|---|
| `LLM_PROVIDER` | `litellm` | provider for bare model names |
| `LITELLM_BASE_URL`, `LITELLM_API_KEY` | | LiteLLM proxy |
| `OPENAI_API_KEY`, `OPENAI_BASE_URL` | | OpenAI or an OpenAI-compatible server |
| `ANTHROPIC_API_KEY` | | Anthropic |
| `OPENROUTER_API_KEY` | | OpenRouter |
| `BUILDER_MODEL`, `DEFAULT_BOT_MODEL` | | `model` or `provider:model` |
| `ALLOWED_MODELS` | all listed | comma list of models users may pick |
| `SCHEDULE_TIMEZONE` | `UTC` | time zone of cron schedules |
| `DATABASE_URL` | SQLite in `data/` | or PostgreSQL |
| `OIDC_ISSUER`, `OIDC_CLIENT_ID`, ... | | single sign-on |
| `TRACE_CONTENT`, `TRACE_RETENTION_DAYS` | `true`, `30` | trace content and retention |
| `OTEL_EXPORTER_OTLP_ENDPOINT`, `TRACE_UI_URL` | | external collector and trace UI |
| `EVENT_SINK_URL`, `EVENT_CONTENT` | | CloudEvents sink |

> Keep comments in `.env` on their own lines: docker compose reads `KEY=  # text` as the value `# text`.

## Development

```bash
make test        # unit tests (no network)
make test-all    # plus live tests through the gateway (requires `make up`)
make e2e         # functional test of the first use case (requires a model provider and `make up`)
make gateway-check  # validate gateway/config.yaml before an agentgateway upgrade
```

`make e2e` runs the real journey on a temporary database and bots directory:
1. It asks the builder to *"create a bot to get the daily AI news from hackernews"*.
2. It checks that the builder created a bot with hackernews tools.
3. It runs the bot and checks that the digest links its sources.
4. It asks a follow-up question that must use the web tools.

The generated `SKILL.md`, digest and follow-up answer are saved in `data/e2e/`. The test skips itself when no model provider or gateway is reachable.

## Roadmap

- **Multi-tenant gateway.**
  - Done: per-user JWTs with per-bot tool scoping, rate limits and OIDC login.
  - Done: a Connections page where users connect their own SaaS accounts through the apps broker.
  - Next: LLM traffic through an agentgateway `ai` route (prompt guards, token budgets per user).
- **Scheduling.** Now a simple cron loop in the backend (one process). Later: a job queue or Temporal, for several replicas, retries and catch-up.
- **Memory.** TTL and compaction, then full-text search over past runs.
- **Connectors and approvals.** A2A layer 2, Gmail and others, and an approval queue; see [docs/plan-a2a-connectors.md](docs/plan-a2a-connectors.md).
- **Governance.**
  - A budget per bot.
  - Trace export to Langfuse.
  - Trace content limits per bot or user.
- **Resilience.** Move the chat stream to a background task, so a closed browser tab doesn't lose the turn.

## License

**Preview: no license has been chosen yet.** The source is public so that people can review it. Until a license is published, all rights are reserved: you may read the code, but not use, copy, modify or redistribute it without permission. See [LICENSE](LICENSE).
