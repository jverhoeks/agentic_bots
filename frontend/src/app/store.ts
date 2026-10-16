import { Injectable, computed, inject, signal } from '@angular/core';
import { Api, Bot, CloudEvent, Msg, SessionInfo, ToolCall } from './api';

export interface Streaming {
  text: string;
  tools: ToolCall[];
}

export type View = { kind: 'session'; id: string } | { kind: 'tools' };

@Injectable({ providedIn: 'root' })
export class Store {
  private api = inject(Api);

  readonly sessions = signal<SessionInfo[]>([]);
  readonly bots = signal<Bot[]>([]);
  readonly view = signal<View>({ kind: 'session', id: 'main' });
  readonly messages = signal<Record<string, Msg[]>>({});
  readonly streaming = signal<Record<string, Streaming | undefined>>({});
  /** Live tool activity of background runs, per bot. */
  readonly runActivity = signal<Record<string, ToolCall[]>>({});
  readonly error = signal<string | null>(null);

  readonly currentSession = computed(() => {
    const v = this.view();
    return v.kind === 'session' ? this.sessions().find((s) => s.id === v.id) : undefined;
  });
  readonly currentBot = computed(() => {
    const s = this.currentSession();
    return s?.bot_name ? this.bots().find((b) => b.name === s.bot_name) : undefined;
  });

  async init() {
    await this.refresh();
    this.api.events((e) => this.onEvent(e));
    const hash = location.hash.slice(1);
    const known = this.sessions().some((s) => s.id === hash);
    await this.open(hash === 'tools' ? { kind: 'tools' } : { kind: 'session', id: known ? hash : 'main' });
  }

  async refresh() {
    try {
      const [sessions, bots] = await Promise.all([this.api.sessions(), this.api.bots()]);
      this.sessions.set(sessions);
      this.bots.set(bots);
      this.error.set(null);
    } catch (e) {
      this.error.set(`Backend unreachable: ${(e as Error).message}`);
    }
  }

  async open(view: View) {
    this.view.set(view);
    history.replaceState(null, '', '#' + (view.kind === 'tools' ? 'tools' : view.id));
    if (view.kind === 'session') await this.loadMessages(view.id);
  }

  async loadMessages(sid: string) {
    try {
      const msgs = await this.api.messages(sid);
      this.messages.update((m) => ({ ...m, [sid]: msgs }));
    } catch (e) {
      this.error.set((e as Error).message);
    }
  }

  private append(sid: string, msg: Msg) {
    this.messages.update((m) => ({ ...m, [sid]: [...(m[sid] ?? []), msg] }));
  }

  private setStreaming(sid: string, s: Streaming | undefined) {
    this.streaming.update((m) => ({ ...m, [sid]: s }));
  }

  async send(sid: string, text: string) {
    this.append(sid, { role: 'user', content: text, tool_calls: [] });
    let st: Streaming = { text: '', tools: [] };
    this.setStreaming(sid, st);
    for await (const ev of this.api.chat(sid, text)) {
      if (ev.type === 'text_delta') st = { ...st, text: st.text + ev.text };
      else if (ev.type === 'tool_call') st = { ...st, tools: [...st.tools, ev], text: st.text ? st.text + '\n\n' : '' };
      else if (ev.type === 'tool_result') st = { ...st, tools: st.tools.map((t) => (t.id === ev.id ? { ...t, ...ev } : t)) };
      else if (ev.type === 'done') {
        this.append(sid, ev.message);
        break;
      } else if (ev.type === 'error') {
        this.append(sid, ev.message ?? { role: 'error', content: ev.error, tool_calls: [] });
        break;
      }
      this.setStreaming(sid, st);
    }
    this.setStreaming(sid, undefined);
  }

  async runBot(name: string) {
    try {
      await this.api.run(name);
      this.runActivity.update((a) => ({ ...a, [name]: [] }));
      await this.refresh();
    } catch (e) {
      this.error.set((e as Error).message);
    }
  }

  async deleteBot(name: string) {
    await this.api.deleteBot(name);
    await this.refresh();
    await this.open({ kind: 'session', id: 'main' });
  }

  async clear(sid: string) {
    await this.api.clear(sid);
    await this.loadMessages(sid);
  }

  private async onEvent(e: CloudEvent) {
    const type = e.type.replace('com.agenticbots.', '');
    const bot = e.subject ?? '';
    if (type === 'run.progress') {
      const call = e.data as ToolCall & { type: string };
      this.runActivity.update((a) => {
        const list = a[bot] ?? [];
        const exists = list.some((t) => t.id === call.id);
        return { ...a, [bot]: exists ? list.map((t) => (t.id === call.id ? { ...t, ...call } : t)) : [...list, call] };
      });
      return;
    }
    if (type === 'run.finished') {
      this.runActivity.update((a) => ({ ...a, [bot]: [] }));
      await this.loadMessages(`bot-${bot}`);
    }
    await this.refresh();
  }
}
