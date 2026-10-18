import { Injectable, computed, effect, inject, signal } from '@angular/core';
import { Api, AuthUser, Bot, CloudEvent, Msg, SessionInfo, ToolCall } from './api';

export interface Streaming {
  text: string;
  tools: ToolCall[];
}

export type View = { kind: 'session'; id: string } | { kind: 'tools' } | { kind: 'connections' } | { kind: 'admin' };

const TUTORIAL_KEY = 'tutorial';

/** Which tutorial is open (null: the picker) and its step. */
export interface TutorialState { id: string | null; step: number }

function savedTutorial(): TutorialState | null {
  try {
    return JSON.parse(localStorage.getItem(TUTORIAL_KEY) ?? 'null');
  } catch {
    return null;
  }
}

@Injectable({ providedIn: 'root' })
export class Store {
  private api = inject(Api);
  private es?: EventSource;

  /** undefined while checking the cookie, null when logged out. */
  readonly user = signal<AuthUser | null | undefined>(undefined);
  readonly canRegister = signal(false);
  readonly sso = signal<string | null>(null);
  readonly sessions = signal<SessionInfo[]>([]);
  readonly bots = signal<Bot[]>([]);
  readonly view = signal<View>({ kind: 'session', id: 'main' });
  readonly messages = signal<Record<string, Msg[]>>({});
  readonly streaming = signal<Record<string, Streaming | undefined>>({});
  /** Live tool activity of background runs, per bot. */
  readonly runActivity = signal<Record<string, ToolCall[]>>({});
  readonly error = signal<string | null>(null);
  /** Heads-up banner, e.g. "bot X updated itself"; click opens the bot. */
  readonly notice = signal<{ text: string; sessionId: string } | null>(null);
  /** Tutorial mode: the open tutorial and step, or null when off (kept across reloads). */
  readonly tutorial = signal<TutorialState | null>(savedTutorial());
  /** The trace shown in the trace overlay (from a chat message or the admin page). */
  readonly traceId = signal<string | null>(null);
  /** External trace UI (TRACE_UI_URL, optional), known once an admin has loaded the admin overview. */
  readonly traceUi = signal<string | null>(null);

  /** Each user has their own main session (`main-<user>`). */
  readonly mainId = computed(() => this.sessions().find((s) => s.kind === 'main')?.id ?? 'main');
  readonly currentSession = computed(() => {
    const v = this.view();
    return v.kind === 'session' ? this.sessions().find((s) => s.id === v.id) : undefined;
  });
  readonly currentBot = computed(() => {
    const s = this.currentSession();
    return s?.bot_name ? this.bots().find((b) => b.name === s.bot_name) : undefined;
  });

  constructor() {
    effect(() => {
      const t = this.tutorial();
      if (t === null) localStorage.removeItem(TUTORIAL_KEY);
      else localStorage.setItem(TUTORIAL_KEY, JSON.stringify(t));
    });
  }

  async init() {
    this.api.onUnauthorized = () => this.loggedOut();
    try {
      const status = await this.api.authStatus();
      this.canRegister.set(status.can_register);
      this.sso.set(status.oidc);
      this.user.set(status.user);
      if (status.user) await this.start();
    } catch (e) {
      this.user.set(null);
      this.error.set(`Backend unreachable: ${(e as Error).message}`);
    }
  }

  async login(username: string, password: string, register: boolean) {
    this.user.set(await this.api.login(username, password, register));
    this.canRegister.set(false);
    await this.start();
  }

  async logout() {
    await this.api.logout().catch(() => undefined);
    this.loggedOut();
    this.canRegister.set((await this.api.authStatus().catch(() => null))?.can_register ?? false);
  }

  private async start() {
    await this.refresh();
    this.es?.close();
    this.es = this.api.events((e) => this.onEvent(e));
    const hash = location.hash.slice(1);
    const known = this.sessions().some((s) => s.id === hash);
    const page = hash === 'tools' || hash === 'connections' || (hash === 'admin' && this.user()?.role === 'admin') ? hash : null;
    await this.open(page ? { kind: page } : { kind: 'session', id: known ? hash : this.mainId() });
  }

  private loggedOut() {
    this.es?.close();
    this.es = undefined;
    this.user.set(null);
    this.sessions.set([]);
    this.bots.set([]);
    this.messages.set({});
    this.error.set(null);
    this.traceId.set(null);
    this.notice.set(null);
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
    history.replaceState(null, '', '#' + (view.kind === 'session' ? view.id : view.kind));
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
    await this.open({ kind: 'session', id: this.mainId() });
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
    const notice = (e.data as { notice?: Msg } | null)?.notice;
    if (type === 'bot.updated' && notice) {
      const sid = `bot-${bot}`;
      if (this.messages()[sid]) this.append(sid, notice);
      this.notice.set({ text: notice.content, sessionId: sid });
    }
    if (type === 'run.finished') {
      this.runActivity.update((a) => ({ ...a, [bot]: [] }));
      await this.loadMessages(`bot-${bot}`);
    }
    await this.refresh();
  }
}
