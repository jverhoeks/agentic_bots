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
  role: 'user' | 'assistant' | 'error';
  content: string;
  tool_calls: ToolCall[];
  run_id?: string | null;
  created_at?: string;
}

export interface SessionInfo {
  id: string;
  kind: 'main' | 'bot';
  bot_name: string | null;
  title: string;
}

export interface RunInfo {
  id: string;
  bot_name: string;
  status: 'running' | 'ok' | 'failed';
  trigger: string;
  started_at: string;
  finished_at: string | null;
  usage: { requests?: number; tool_calls?: number; input_tokens?: number; output_tokens?: number };
  error: string | null;
}

export interface Bot {
  name: string;
  title: string;
  description: string;
  model: string;
  tools: string[];
  trigger: string;
  max_steps: number;
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
  private async json<T>(url: string, init?: RequestInit): Promise<T> {
    const r = await fetch(url, init);
    if (!r.ok) {
      const body = await r.json().catch(() => ({}));
      throw new Error(body.detail ?? `${r.status} ${r.statusText}`);
    }
    return r.json();
  }

  sessions = () => this.json<SessionInfo[]>('/api/sessions');
  messages = (sid: string) => this.json<Msg[]>(`/api/sessions/${sid}/messages`);
  clear = (sid: string) => this.json(`/api/sessions/${sid}/messages`, { method: 'DELETE' });
  bots = () => this.json<Bot[]>('/api/bots');
  bot = (name: string) => this.json<BotDetail>(`/api/bots/${name}`);
  run = (name: string) => this.json<RunInfo>(`/api/bots/${name}/run`, { method: 'POST' });
  deleteBot = (name: string) => this.json(`/api/bots/${name}`, { method: 'DELETE' });
  tools = (refresh = false) =>
    this.json<{ gateway: { url: string }[]; tools: ToolInfo[] }>(`/api/tools?refresh=${refresh}`);
  models = () => this.json<{ builder: string; default: string; models: string[]; error: string | null }>('/api/models');

  /** POST a chat message and yield the SSE events of the agent turn. */
  async *chat(sid: string, message: string): AsyncGenerator<StreamEvent> {
    const r = await fetch(`/api/sessions/${sid}/chat`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ message }),
    });
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
