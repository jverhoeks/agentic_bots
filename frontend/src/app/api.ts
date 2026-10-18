import { Injectable } from '@angular/core';

export interface ToolCall {
  id: string;
  name: string;
  args?: Record<string, unknown>;
  status: 'running' | 'ok' | 'error';
  preview?: string;
}

export interface Msg {
  id?: number;
  /** notice: a heads-up from the platform, e.g. the bot changed itself. */
  role: 'user' | 'assistant' | 'error' | 'notice';
  content: string;
  tool_calls: ToolCall[];
  run_id?: string | null;
  trace_id?: string | null;
  created_at?: string;
}

export interface SessionInfo {
  id: string;
  kind: 'main' | 'bot';
  bot_name: string | null;
  title: string;
}

export interface AuthUser {
  id: string;
  name: string;
  role: 'admin' | null;
}

export interface AuthStatus {
  user: AuthUser | null;
  can_register: boolean;
  /** Label of the single sign-on button, when OIDC is configured. */
  oidc: string | null;
}

export interface RunInfo {
  id: string;
  bot_name: string;
  status: 'running' | 'ok' | 'failed';
  trigger: string;
  owner?: string | null;
  actor?: string | null;
  trace_id?: string | null;
  started_at: string;
  finished_at: string | null;
  usage: { requests?: number; tool_calls?: number; input_tokens?: number; output_tokens?: number };
  error: string | null;
}

export type Verbosity = 'short' | 'medium' | 'verbose';

export interface MemoryItem {
  key: string;
  value: string;
  updated_at: string;
}

export interface Bot {
  name: string;
  title: string;
  description: string;
  model: string;
  tools: string[];
  trigger: string;
  schedule?: string | null; // cron, in the backend's SCHEDULE_TIMEZONE
  next_run?: string | null;
  max_steps: number;
  verbosity: Verbosity;
  session_id: string;
  last_run: RunInfo | null;
  created_at?: string;
  updated_at?: string;
}

export interface BotDetail extends Bot {
  instructions: string;
  skill_md: string;
  runs: RunInfo[];
}

export interface ToolInfo {
  name: string;
  connector: string;
  description: string;
  read_only: boolean;
  input_schema: { properties?: Record<string, { type?: string; default?: unknown }> };
}

export interface AuditEvent {
  id: number;
  ts: string;
  kind: string;
  user: string | null;
  actor: string | null;
  bot: string | null;
  run_id: string | null;
  session_id: string | null;
  detail: Record<string, any>;
  trace_id: string | null;
}

export interface AdminOverview {
  users: number;
  bots: number;
  runs_24h: Record<string, number>;
  events_24h: Record<string, number>;
  guardrails_24h: number;
  traces_24h: Record<string, number>;
  links: { traces: string; gateway: string };
}

/** One trace (chat turn, bot run, A2A or MCP request), summarised. */
export interface TraceInfo {
  trace_id: string;
  name: string;
  kind: 'chat' | 'run' | 'a2a' | 'gateway' | 'other' | string;
  user: string | null;
  session_id: string | null;
  bot: string | null;
  run_id: string | null;
  actor: string | null;
  parent_trace_id: string | null;
  started_at: string;
  ended_at: string;
  duration_ms: number;
  spans: number;
  errors: number;
  llm_calls: number;
  tool_calls: number;
  input_tokens: number;
  output_tokens: number;
  services: string[];
}

export interface TraceSpan {
  span_id: string;
  parent_id: string | null;
  name: string;
  service: string;
  kind: string;
  start: string;
  duration_ms: number;
  status: 'unset' | 'ok' | 'error';
  status_message: string | null;
  user: string | null;
  session_id: string | null;
  bot: string | null;
  attrs: Record<string, unknown>;
  events: { name: string; ts: string; attrs: Record<string, unknown> }[];
}

export interface TraceDetail {
  trace: TraceInfo;
  spans: TraceSpan[];
}

export interface AdminUser {
  id: string;
  name: string;
  role: 'admin' | null;
  created_at: string;
  bots: number;
  last_seen: string | null;
}

export type StreamEvent =
  | { type: 'text_delta'; text: string }
  | ({ type: 'tool_call' } & ToolCall)
  | ({ type: 'tool_result' } & ToolCall)
  | { type: 'done'; message: Msg }
  | { type: 'error'; error: string; message?: Msg };

export interface CloudEvent<T = any> {
  specversion: '1.0';
  id: string;
  type: string;
  source: string;
  subject?: string;
  time: string;
  data: T;
}

@Injectable({ providedIn: 'root' })
export class Api {
  /** Called when the session cookie is missing or expired (the store shows the login page). */
  onUnauthorized: () => void = () => {};

  private async json<T>(url: string, init?: RequestInit): Promise<T> {
    const r = await fetch(url, init);
    if (r.status === 401 && !url.startsWith('/api/auth/')) this.onUnauthorized();
    if (!r.ok) {
      const body = await r.json().catch(() => ({}));
      const detail = Array.isArray(body.detail) ? body.detail.map((d: { msg: string }) => d.msg).join('; ') : body.detail;
      throw new Error(detail ?? `${r.status} ${r.statusText}`);
    }
    return r.json();
  }

  authStatus = () => this.json<AuthStatus>('/api/auth/status');
  login = (username: string, password: string, register = false) =>
    this.json<AuthUser>(`/api/auth/${register ? 'register' : 'login'}`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ username, password }),
    });
  logout = () => this.json('/api/auth/logout', { method: 'POST' });

  sessions = () => this.json<SessionInfo[]>('/api/sessions');
  messages = (sid: string) => this.json<Msg[]>(`/api/sessions/${sid}/messages`);
  clear = (sid: string) => this.json(`/api/sessions/${sid}/messages`, { method: 'DELETE' });
  bots = () => this.json<Bot[]>('/api/bots');
  bot = (name: string) => this.json<BotDetail>(`/api/bots/${name}`);
  run = (name: string) => this.json<RunInfo>(`/api/bots/${name}/run`, { method: 'POST' });
  patchBot = (name: string, patch: { verbosity?: Verbosity }) =>
    this.json<Bot>(`/api/bots/${name}`, {
      method: 'PATCH',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(patch),
    });
  memory = (name: string) => this.json<MemoryItem[]>(`/api/bots/${name}/memory`);
  forget = (name: string, key?: string) =>
    this.json(`/api/bots/${name}/memory${key ? `?key=${encodeURIComponent(key)}` : ''}`, { method: 'DELETE' });
  deleteBot = (name: string) => this.json(`/api/bots/${name}`, { method: 'DELETE' });
  tools = (refresh = false) =>
    this.json<{ gateway: { url: string }[]; tools: ToolInfo[] }>(`/api/tools?refresh=${refresh}`);
  models = () => this.json<{ builder: string; default: string; models: string[]; error: string | null }>('/api/models');

  trace = (id: string) => this.json<TraceDetail>(`/api/traces/${id}`);
  sessionTraces = (sid: string) => this.json<TraceInfo[]>(`/api/sessions/${encodeURIComponent(sid)}/traces`);

  admin = {
    traces: (q: Record<string, string> = {}) => this.json<TraceInfo[]>(`/api/admin/traces?${new URLSearchParams(q)}`),
    overview: () => this.json<AdminOverview>('/api/admin/overview'),
    users: () => this.json<AdminUser[]>('/api/admin/users'),
    setRole: (id: string, role: 'admin' | null) =>
      this.json(`/api/admin/users/${id}`, {
        method: 'PATCH',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ role }),
      }),
    runs: (q: Record<string, string> = {}) => this.json<RunInfo[]>(`/api/admin/runs?${new URLSearchParams(q)}`),
    run: (id: string) => this.json<{ run: RunInfo; messages: Msg[]; events: AuditEvent[] }>(`/api/admin/runs/${id}`),
    audit: (q: Record<string, string> = {}) => this.json<AuditEvent[]>(`/api/admin/audit?${new URLSearchParams(q)}`),
  };

  /** POST a chat message and yield the SSE events of the agent turn. */
  async *chat(sid: string, message: string): AsyncGenerator<StreamEvent> {
    const r = await fetch(`/api/sessions/${sid}/chat`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ message }),
    });
    if (r.status === 401) this.onUnauthorized();
    if (!r.ok || !r.body) {
      const body = await r.json().catch(() => ({}));
      yield { type: 'error', error: body.detail ?? `${r.status} ${r.statusText}` };
      return;
    }
    const reader = r.body.pipeThrough(new TextDecoderStream()).getReader();
    let buf = '';
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += value.replace(/\r\n/g, '\n');
      let idx;
      while ((idx = buf.indexOf('\n\n')) >= 0) {
        const block = buf.slice(0, idx);
        buf = buf.slice(idx + 2);
        const data = block
          .split('\n')
          .filter((l) => l.startsWith('data:'))
          .map((l) => l.slice(5).trimStart())
          .join('\n');
        if (data) yield JSON.parse(data) as StreamEvent;
      }
    }
  }

  /** Global CloudEvents stream (bot created, run started/progress/finished). */
  events(onEvent: (e: CloudEvent) => void): EventSource {
    const es = new EventSource('/api/events');
    es.onmessage = (m) => onEvent(JSON.parse(m.data));
    return es;
  }
}
