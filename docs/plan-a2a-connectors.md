# Plan: agent-to-agent, connectors, memory and what to copy from Grok Bot / dots / Muse

Status 2026-10-17. "Done" means it is in the repo and covered by unit tests. "Verify first" items are
unconfirmed assumptions; do not build on them until they have been checked.

## 1. Where we stand

| Area | State |
|---|---|
| Bots from chat (SKILL.md), runs, follow-ups, update-by-prompt | done, e2e tested against LiteLLM |
| Tools via agentgateway (MCP): hackernews, web search/fetch | done |
| Per-bot memory (`memory_*` tools, last-run record, Memory tab with Forget) | done |
| Verbosity per bot (`metadata.verbosity: short\|medium\|verbose`, selector in the UI) | done |
| A2A between our own bots (in-process, `a2a` connector) | done (layer 1) |
| A2A over the network: each bot is an A2A 1.0 server behind agentgateway; bots can call allow-listed remote agents | done (layer 2) |
| Schedules | **missing**: bots tell users they cannot run weekly, which is correct today |
| SaaS connectors via open-connector (`apps` target, per-user token) | gateway side verified live; a real Gmail connection is not yet tested |
| Login: password + generic OIDC (PKCE), admin role | done (OIDC tested against a fake provider only) |
| Gateway identity: JWT per call, per-bot tool authorization, rate limits | done, verified live (§4.1, §7) |
| Tracing (OpenTelemetry → app DB per user/session, optional OTLP collector), audit log, Admin page | done (§7) |
| Guardrails: secret redaction, injection flag, per-turn limits | done (§7); approval = granting the tool at bot setup (decided 2026-10-17) |

## 2. What the products do (research summary)

| Feature | Grok Bot (xAI) | dots (OpenAI) | Muse (Meta) | Us |
|---|---|---|---|---|
| Create by chat | yes + profile | name + rules + brief | onboarding chat | yes |
| Many bots per user | roster | 1 (more planned) | 1 | yes |
| Reusable skills | yes (chat or demo recording) | – | – | SKILL.md (one per bot) |
| Schedules | ≥5 min, 50/bot, "Test run" | Scheduled tab | natural language | **no** |
| Event triggers | Slack, GitHub | proactive read-only | email forwarding | no (CloudEvents ready) |
| Approvals | per action | act / pre-approved / ask / hand off | Read / Write / Sensitive | no |
| Bot-to-bot | DMs, handoff, groups of 2-6, admin kill-switch | enterprise preview | – | delegation tools (layer 1) |
| Memory | per bot, "not authoritative" | inherited, not editable | "forget" | per bot, visible, Forget |
| Sharing | templates without credentials, marketplace | – | – | SKILL.md in git |

Grok Bot is the closest model for us: a roster of single-purpose bots, skills, routines, and bots that talk to each other.

### Top features to copy (priority order)
1. **Schedules**: natural-language schedule → cron in `metadata.schedule`, confirmed back with the time zone, a "Test run" button, pause/resume, and the last N runs per bot. APScheduler in-process first, Temporal later.
2. **Approvals queue**: tools are classed Read / Write / Sensitive-write (MCP `readOnlyHint`/`destructiveHint` + a gateway policy). Write tools pause the run and ask the user, and sensitive writes can never be auto-allowed. (Option only: decided 2026-10-17 that granting the tool at bot setup is the approval; see §6.)
3. **Durable rules in the description** ("never send without approval"), enforced by the platform rather than only by the prompt.
4. **"Save this as a skill"**: turn a good follow-up chat into a new bot or an update.
5. **Bot groups / handoff** (Grok): a coordinator bot with `a2a` plus specialists; an admin switch to disable bot-to-bot messaging.
6. **Templates**: share a SKILL.md without memory, history or credentials (we already store bots this way).
7. **Stale-data policy**: when a run gets no fresh data it says so instead of reusing memory.

## 3. A2A

### Layer 1 (done): in-process delegation, A2A-shaped
- Built-in connector `a2a` (listed in the tool catalog, granted through `allowed-tools`, so the builder can give it to a bot).
  - `a2a_list_agents`
  - `a2a_send_message(agent, message)`: waits for the answer, up to 5 minutes.
  - `a2a_start_task`, `a2a_get_task`: start without waiting, then poll.
  - `a2a_get_last_result(agent)`: reads the target's last-run memory, with no LLM call.
- Every request is a normal **run of the target bot** with trigger `a2a:<caller>`. The request appears in the target's session, and the run has history, events and a memory record.
- **Safety:**
  - The call chain is tracked in a contextvar. A bot cannot call itself or anything already in the chain (a→b→a would deadlock on session locks).
  - Depth is capped at 2.
  - A busy target returns a message instead of an error.
  - Replies are labelled as untrusted data.
- Results use A2A field names (`id`, `contextId`, `status.state`, `artifacts[].parts[]`), so the transport can be swapped without changing the tools.

### Layer 2 (done): network A2A through agentgateway
Both checks from the "verify first" list were done before building:
- fasta2a 2.1.1 works with pydantic-ai 2.54. We use only its server (JSON-RPC, SSE, A2A 1.0 + legacy method names), with our own worker.
- agentgateway v1.6.0: the simplified `mcp:` block and an A2A `routes:` entry run side by side on one `gateways.default` port. No migration to the deprecated `binds:` was needed. `make smoke` still lists all MCP tools.

What runs:
1. **Each bot is an A2A server**:
   - the endpoint is `/a2a/<bot>/`, with the Agent Card at `/a2a/<bot>/.well-known/agent-card.json`, generated from SKILL.md;
   - the app is built lazily and rebuilt when the bot changes.
   - The worker turns every A2A task into a Run of the bot (trigger `a2a:user:<caller>`). fasta2a's task store is in memory, and the Runs table is the durable record.
2. **agentgateway route** `routes: - name: a2a` with `pathPrefix: /a2a/` and `policies: {a2a: {}}` points to the backend:
   - `A2A_BACKEND` is `backend:8000` in `make up-all`, and `host.docker.internal:8000` with `make up` + `make dev`.
   - nginx (UI) does not proxy `/a2a`, so the gateway is the only way in.
   - Verified: a real `message/send` → `tasks/get` → completed, through `:3000`.
3. **Remote agents**:
   - `a2a_remote_list_agents` and `a2a_remote_send_message` call the agents allow-listed in `config/a2a-agents.json` (`{"agents": {"id": {"url", "headers"}}}`, env vars expanded).
   - Answers are untrusted data.

Still open:
- **Auth:** done. agentgateway requires a backend-signed JWT (`jwtAuth: strict` on the gateway), and the backend's `/a2a` checks it again and serves only bots the token's `sub` may see. The caller is recorded in the trigger (`a2a:user:<sub>`), with one task store per caller. Still open: TLS before exposing beyond localhost.
- **Contexts:** map an A2A `contextId` to its own conversation; today every external caller shares the bot session.
- Changing a bot (e.g. its verbosity) rebuilds its A2A app and drops fasta2a's in-memory task store, so an external task in flight stays "working". Fix this with a Runs-backed Storage.
- The `make up` + `make dev` path for the A2A route (`host.docker.internal:8000`) works (Agent Card fetched through the gateway, 2026-10-17).
- Streaming of partial output (`message/stream` works, but sends status only, no text chunks), cancel of a running bot run, and push notifications.

We do **not** use MCP Tasks or a2a-sdk types internally yet; both are still changing.

## 4. Connectors (Gmail and friends)

| Option | Fit | Notes |
|---|---|---|
| **taylorwilsdon/google_workspace_mcp** | first choice for Gmail/Calendar/Drive | MIT, streamable HTTP, OAuth 2.1 multi-user, `--read-only` mode; one gateway target |
| Google's official Gmail MCP | later | preview, no send |
| **oomol-lab/open-connector** | broad SaaS coverage (1k+ providers) behind one `/mcp`; **chosen**, see 4.1 | Apache-2.0; no tenant model, so per-user isolation is done with per-user runtime tokens |
| Activepieces / Nango (self-host) | multi-tenant OAuth later | Nango handles token storage and refresh per tenant |
| Composio / Arcade / Pipedream / Klavis | hosted, fast to try | data leaves our network; check terms |
| Free, no key: RSS, arXiv, Hugging Face, GitHub public | quick wins | small connectors in `connectors/` |

### 4.1 open-connector behind agentgateway, per user
```
bot run --Bearer JWT(sub=alice)--> agentgateway: jwtAuth, X-User-Id := jwt.sub --target apps, Bearer <alice's token>--> open-connector /mcp
                                    (token picked from OOMOL_USER_TOKENS by user)                              (only alice's connections)
```
- **Service:** `open-connector` in compose, console at `http://localhost:3010` (loopback only). Its
  secrets (encryption key, admin token, bootstrap runtime token) are generated by `make connector-init`
  into `secrets/open-connector.env`, which only that container reads. Setting the bootstrap token turns
  on runtime auth for `/mcp`; no client uses that token.
- **Gateway:** target `apps` with a request transformation that sets `authorization` to
  `"Bearer " + ${OOMOL_USER_TOKENS}[request.headers["x-user-id"]]`. (In v1.6.0, `backendAuth.key` only takes a
  static value; `key.expression` exists on agentgateway main, so switch to it after the next release.) `OOMOL_USER_TOKENS` is a JSON map
  user → token in `secrets/gateway.env` (agentgateway only). An unknown user gets no credential and
  open-connector rejects it; there is no fallback. `failureMode: failOpen` keeps the other targets
  working when `apps` is down or rejects a user.
- **Per user:**
  - A connection in open-connector is named after its user (connection name = login username).
  - `make connector-user USER_ID=<username>` issues a persistent runtime token with
    `allowedConnections` = that user's connection IDs and `allowedActions` = read-only Gmail patterns.
  - The script refuses to issue a token when the user has no connections, because an empty list
    would mean "all connections".
- **App:** sends a short-lived ES256 JWT (`app/tokens.py`, `sub` = bot owner, `bot` claim) on tool calls
  and the catalog. The gateway verifies it with `secrets/jwks.json` and overwrites `X-User-Id` with
  `jwt.sub`, so the header cannot be spoofed. The app never sees a connector secret.
- **Meta-tools:** open-connector exposes `apps_list_connections`, `apps_search_actions`,
  `apps_get_action_guide` and `apps_execute_action`. `allowed-tools` can only grant `execute_action` as a
  whole, so what a user may do is decided by the token's `allowedActions`. Bots with `apps` get a
  prompt rule to pass their `connectionName`.
- **Login:** done: username/password (`app/auth.py`) and any OIDC provider (`app/oidc.py`).
  - Do *not* forward the JWT to open-connector (`OOMOL_CONNECT_JWKS_URI`): JWTs are unrestricted
    there, with no connection grants. Keep one runtime token per user, issued at sign-up.
- **Verified live** (agentgateway v1.6.0, 2026-10-17):
  1. no token or a forged one: 401 on `/mcp` and `/a2a`;
  2. the `shared` subject (no connector token) lists hackernews/web; `apps` is dropped (failOpen), with no error;
  3. a user mapped in `OOMOL_USER_TOKENS` gets the `apps_*` tools;
  4. a `shared` token plus a spoofed `X-User-Id: <that user>` header does not get `apps`, so `jwt.sub` wins;
  5. A2A through the gateway: the token is forwarded (`preserveToken`) and the backend serves the Agent Card.
  - Still to check with a real connection: the response field names of `/api/connections` and
    `/api/runtime-tokens` in `scripts/open_connector.py`, and the Gmail action IDs in `DEFAULT_ACTIONS`.

Rules:
- OAuth tokens live **only in the connector containers** (encrypted at rest), never in the app or the prompt.
- Gmail starts read-only. Sending is allowed for a bot that was granted the send action at setup (§6 item 2).
- Use the minimal scopes.
- Email is untrusted input (prompt injection): it is labelled as data, it is never saved to memory as an instruction, and outbound actions only go through tools the user granted at setup.

## 5. Memory, compaction and TTL

**Today:** per-bot key/value notes plus a `_last_run` record, stored as plain text in SQLite and injected into the prompt (about 6 KB budget), marked as untrusted data.
- Visible in the bot's Memory tab, with Forget per key or for everything.

**Next steps, in order:**
1. **TTL:** an optional `expires_at` per note (`memory_save(..., ttl_days=)`), and default TTLs for platform keys. Clean up expired notes at startup and daily.
2. **Compaction:**
   - Session history: summarise older turns into one summary message once a size threshold is reached. Today we strip old tool payloads.
   - Memory: when a bot is near MAX_KEYS, it merges notes on the next run.
   - Runs: keep the last N runs plus their outputs.
3. **Search, not RAG yet:** SQLite **FTS5** over past run outputs and notes, exposed as a `memory_search` tool. No embeddings needed.
4. Only if keyword search is too weak: **sqlite-vec** with embeddings through LiteLLM. Move to Postgres + pgvector when we go multi-tenant.
5. **Provenance:** store the source of each note (bot, run, tool), so a note that came from an email can be shown and treated differently.

## 6. Suggested next iterations
1. Schedules: done, `metadata.schedule` (cron, `SCHEDULE_TIMEZONE`), a 30-second loop in the backend. Later: a test run
   before saving, pause, and a job queue for several replicas.
2. Approvals: decided 2026-10-17 to auto-approve through the bot setup. Granting a write or send tool (delivery to
   Slack or e-mail included) is the approval, also for scheduled runs; the builder tells the user when a bot can send
   or change things. The per-action queue below is kept as an option, not planned.
3. Connect Gmail read-only for a real user and check the open-connector field names.
4. LLM traffic through agentgateway (`ai` backend): prompt guards and token budgets per user in the gateway.
5. Per-context A2A conversations; memory TTL/compaction, then FTS5 search.
6. Event bus. Internal events use **CloudEvents** 1.0 as the envelope; new producers and consumers keep that format.
   Today: `bot.*` and `run.*` (also to the browser), and the activity events `tool.called`, `llm.called`, `mcp.called`
   and `a2a.called`, made from the stored spans with a `traceparent`, POSTed best effort to `EVENT_SINK_URL`.
   Next: a real broker (NATS or Kafka) behind the same envelope, so consumers survive restarts and can replay.
7. Data lineage lives in the other (data platform) repo: OpenLineage from SQE and Spark, and the lineage/quality
   store. It consumes this repo's activity events from the bus; bots later query lineage and data-quality results
   through a connector.

## 7. Observability, audit, admin and guardrails

**Tracing.** OpenTelemetry. Spans are stored in the app database (`app/tracestore.py`: `Trace` summary rows and
`TraceSpan` rows, SQLite or PostgreSQL) and, when `OTEL_EXPORTER_OTLP_ENDPOINT` is set, sent to that collector as well (Jaeger was removed from compose on 2026-10-18: no login, every user's trace content).
- One trace per chat turn (`chat.turn`), bot run (`bot.run`: user, bot, run id, trigger, actor) and inbound A2A
  request, then the pydantic-ai spans (`invoke_agent`, `chat <model>`, `execute_tool`) and the httpx calls.
  A run started from a chat (builder `run_bot`) gets a span link to it: `Trace.parent_trace_id`.
- Every span gets `user`, `session.id`, `bot`, `run.id`, `actor` from the audit scope (`AttributionProcessor`),
  so a trace and each of its spans can be found per user and session.
- agentgateway exports OTLP/HTTP to the backend (`POST /otlp/v1/traces`, compose network only, with the shared
  `x-trace-token` = `TRACE_INGEST_TOKEN` from `secrets/trace-ingest.env` (written by `make jwt-init`), so the
  connectors cannot forge spans. The gateway sets it with a `requestHeaderModifier` policy on
  `frontendPolicies.tracing` (not the deprecated `config.tracing`). `make gateway-check` validates the config against
  the pinned image: run it before upgrading agentgateway, it refuses to start on unknown fields. nginx proxies only
  `/api/`). The backend stores the spans and forwards them to the collector, if set. MCP calls that reach the gateway without a
  backend parent (external clients) become `gateway` traces of their own. With `make up` + `make dev` they go to the host backend (`A2A_BACKEND`).
- `Trace` keeps counts per trace: spans, errors, LLM calls, tool calls, input/output tokens, services.
- API: `/api/sessions/{id}/traces`, `/api/traces/{id}` (own traces; admins all), `/api/admin/traces`.
  `Message.trace_id` links each chat answer to its trace.
- agentgateway continues the trace from `traceparent`: its spans (`tools/call <target>` per MCP request, with
  `user` and `bot` from the JWT) sit in the same trace as the backend.
- The pydantic-ai, openai and mcp libraries use `httpx2`. Without `HTTPX2ClientInstrumentor` there are no client
  spans and no `traceparent` header, so the gateway starts traces of its own.
- Content (prompts, answers, tool arguments and results) is on by default (`TRACE_CONTENT=true`): strings are
  redacted with the guardrail patterns and capped (`TRACE_MAX_ATTR_CHARS`); the repeated message history
  (`gen_ai.input.messages`, `pydantic_ai.all_messages`) is dropped, the session history has it. Retention:
  `TRACE_RETENTION_DAYS`. Next: limit content per bot or user where needed.
- `Run.trace_id` links a run to its trace.

**Audit log.** `AuditEvent` in the app database (`app/audit.py`): the short, durable record of who did what.
- Kinds:
  - `auth.login|login_failed|register|logout|token_issued`, `admin.role_changed`;
  - `run.started|finished`, `tool.call` (tool, args, status, a redacted result preview);
  - `bot.created|updated|deleted`, `memory.deleted`, `session.cleared`, `a2a.inbound`;
  - `guardrail.redacted|injection|usage_limit|prompt_too_long`.
- Each event records:
  - `user` (who it acted as: the bot owner for runs);
  - `actor` (who caused it: a user, `a2a:<bot>`, `a2a:user:<sub>`);
  - bot, run, session and trace id.
- Details are redacted.

**Admin.** `User.role = "admin"`. The first account is the admin. A database from before roles gets its oldest
account promoted at startup. `make admin USER_ID=` promotes a user.
- The Admin page (sidebar, admins only) has four tabs:
  - Activity: the live audit log, filtered by kind prefix, user or bot;
  - Traces: all traces, filtered by kind, user, session or bot; click for the waterfall;
  - Runs: all users, with tokens, tool calls and trace links; click a run for its timeline and output;
  - Users: roles.
- It links to the agentgateway UI, and to an external trace UI when `TRACE_UI_URL` is set.
- API: `/api/admin/{overview,users,bots,runs,runs/{id},audit,traces}`.

**Guardrails.**
| Where | What |
|---|---|
| gateway | JWT required; `mcpAuthorization`: a bot only lists and calls the tools in its token's `tools` claim; `localRateLimit` per user (300 MCP and 30 A2A requests a minute) |
| app, per turn | prompt size (`MAX_PROMPT_CHARS`), total tokens (`TURN_TOKEN_LIMIT`), tool calls (`TURN_TOOL_CALL_LIMIT`), model requests (`max-steps`) |
| app, tool results | secrets (API keys, GitHub/Slack/AWS/Google keys, JWTs, bearer tokens, private keys, `password=` values) are replaced by `[REDACTED]` before the model sees them |
| app, tool results | instruction-like text ("ignore previous instructions", `<system>`, …) is flagged to the model as data and audited; never blocked |
| app, answers and audit | the stored answer and every audit detail are redacted too (streamed text is not) |
| A2A | same owner only, loop and self-call refusal, depth 2 |
- Not done:
  - the approvals queue for write actions;
  - LLM-side guards (moderation, PII), which belong in an agentgateway `ai` route or LiteLLM guardrails;
  - redacting the live text stream;
  - `mcpGuardrails` in agentgateway, because its remote-processor protocol is not documented.
