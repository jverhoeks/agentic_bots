import { Component, OnDestroy, OnInit, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { AdminOverview, AdminUser, Api, AuditEvent, Msg, RunInfo, TraceInfo } from './api';
import { MarkdownPipe } from './markdown.pipe';
import { Store } from './store';
import { ago } from './util';

type Tab = 'activity' | 'traces' | 'runs' | 'users';

/** Admins only: everything that happens across users (audit log, runs, users), with links to the traces. */
@Component({
  selector: 'app-admin-page',
  imports: [FormsModule, MarkdownPipe],
  template: `
    <header class="session-header">
      <div class="grow">
        <h1>Admin</h1>
        <p>Everything that happens: logins, chat turns, runs, tool, MCP and A2A calls, guardrail hits and bot changes, for all users.</p>
        @if (overview(); as o) {
          <div class="meta">
            <span class="pill">{{ o.users }} users</span>
            <span class="pill">{{ o.bots }} bots</span>
            <span class="pill">runs 24h: {{ o.runs_24h['ok'] ?? 0 }} ok · {{ o.runs_24h['failed'] ?? 0 }} failed · {{ o.runs_24h['running'] ?? 0 }} running</span>
            <span class="pill" [class.err]="o.guardrails_24h > 0">guardrail hits 24h: {{ o.guardrails_24h }}</span>
            <span class="pill">traces 24h: {{ o.traces_24h['chat'] ?? 0 }} chat · {{ o.traces_24h['run'] ?? 0 }} run ·
              {{ o.traces_24h['a2a'] ?? 0 }} a2a · {{ o.traces_24h['gateway'] ?? 0 }} gateway</span>
            @if (o.links.traces) { <a class="pill" [href]="o.links.traces" target="_blank">Trace UI ↗</a> }
            <a class="pill" [href]="o.links.gateway" target="_blank">Gateway UI ↗</a>
          </div>
        }
      </div>
      <div class="actions">
        @for (t of tabs; track t) {
          <button [class.primary]="tab() === t" (click)="show(t)">{{ t }}</button>
        }
      </div>
    </header>

    <div class="admin">
      @if (error()) { <div class="banner" (click)="error.set(null)">{{ error() }}</div> }

      @if (tab() === 'activity') {
        <div class="filters">
          <select [(ngModel)]="kind" (ngModelChange)="loadAudit()">
            <option value="">all events</option>
            @for (k of kinds; track k) { <option [value]="k">{{ k }}*</option> }
          </select>
          <input placeholder="user" [(ngModel)]="user" (change)="loadAudit()" />
          <input placeholder="bot" [(ngModel)]="bot" (change)="loadAudit()" />
          <label class="check"><input type="checkbox" [(ngModel)]="live" (ngModelChange)="poll()" /> live</label>
        </div>
        <table class="runs">
          <tr><th>when</th><th>event</th><th>user</th><th>by</th><th>bot</th><th>detail</th><th></th></tr>
          @for (e of events(); track e.id) {
            <tr [class.guard]="e.kind.startsWith('guardrail.')" [class.err]="e.kind === 'auth.login_failed'">
              <td [title]="e.ts">{{ ago(e.ts) }}</td>
              <td><code>{{ e.kind }}</code></td>
              <td>{{ e.user }}</td>
              <td>{{ e.actor !== e.user ? e.actor : '' }}</td>
              <td>{{ e.bot }}</td>
              <td class="detail" [title]="json(e.detail)">{{ summary(e) }}</td>
              <td class="links">
                @if (e.run_id) { <button class="link" (click)="openRun(e.run_id)">run</button> }
                @if (e.trace_id) { <button class="link" (click)="store.traceId.set(e.trace_id)">trace</button> }
              </td>
            </tr>
          } @empty { <tr><td colspan="7" class="hint">No events.</td></tr> }
        </table>
        @if (events().length && events().length % 200 === 0) {
          <button class="more" (click)="loadAudit(true)">Older</button>
        }
      }

      @if (tab() === 'traces') {
        <div class="filters">
          <select [(ngModel)]="traceKind" (ngModelChange)="loadTraces()">
            <option value="">all traces</option>
            @for (k of traceKinds; track k) { <option [value]="k">{{ k }}</option> }
          </select>
          <input placeholder="user" [(ngModel)]="user" (change)="loadTraces()" />
          <input placeholder="session" [(ngModel)]="session" (change)="loadTraces()" />
          <input placeholder="bot" [(ngModel)]="bot" (change)="loadTraces()" />
        </div>
        <table class="runs">
          <tr><th>started</th><th>trace</th><th>user</th><th>session</th><th>bot</th><th>took</th><th>LLM</th><th>tools</th><th>tokens in/out</th><th>spans</th></tr>
          @for (t of traces(); track t.trace_id) {
            <tr class="clickable" [class.err]="t.errors > 0" (click)="store.traceId.set(t.trace_id)">
              <td [title]="t.started_at">{{ ago(t.started_at) }}</td>
              <td><code>{{ t.kind }}</code> {{ t.name }}</td>
              <td>{{ t.user }}{{ t.actor && t.actor !== t.user ? ' (by ' + t.actor + ')' : '' }}</td>
              <td>{{ t.session_id }}</td>
              <td>{{ t.bot }}</td>
              <td>{{ ms(t.duration_ms) }}</td>
              <td>{{ t.llm_calls }}</td>
              <td>{{ t.tool_calls }}</td>
              <td>{{ t.input_tokens }} / {{ t.output_tokens }}</td>
              <td [title]="t.services.join(', ')">{{ t.spans }}{{ t.services.includes('agentgateway') ? ' ⇄' : '' }}</td>
            </tr>
          } @empty { <tr><td colspan="10" class="hint">No traces.</td></tr> }
        </table>
        @if (traces().length && traces().length % 100 === 0) {
          <button class="more" (click)="loadTraces(true)">Older</button>
        }
      }

      @if (tab() === 'runs') {
        <table class="runs">
          <tr><th>started</th><th>bot</th><th>user</th><th>started by</th><th>status</th><th>tokens in/out</th><th>tools</th><th></th></tr>
          @for (r of runs(); track r.id) {
            <tr class="clickable" (click)="openRun(r.id)">
              <td [title]="r.started_at">{{ ago(r.started_at) }}</td>
              <td>{{ r.bot_name }}</td>
              <td>{{ r.owner ?? 'shared' }}</td>
              <td>{{ r.actor ?? r.trigger }}</td>
              <td><span class="dot" [class]="r.status"></span> {{ r.status }}</td>
              <td>{{ r.usage.input_tokens ?? '' }} / {{ r.usage.output_tokens ?? '' }}</td>
              <td>{{ r.usage.tool_calls ?? '' }}</td>
              <td class="links">@if (r.trace_id) { <button class="link" (click)="$event.stopPropagation(); store.traceId.set(r.trace_id)">trace</button> }</td>
            </tr>
          } @empty { <tr><td colspan="8" class="hint">No runs.</td></tr> }
        </table>
      }

      @if (tab() === 'users') {
        <table class="runs">
          <tr><th>user</th><th>name</th><th>role</th><th>bots</th><th>last seen</th><th>created</th><th></th></tr>
          @for (u of users(); track u.id) {
            <tr>
              <td><code>{{ u.id }}</code></td>
              <td>{{ u.name }}</td>
              <td>{{ u.role ?? 'user' }}</td>
              <td>{{ u.bots }}</td>
              <td>{{ ago(u.last_seen) }}</td>
              <td>{{ ago(u.created_at) }}</td>
              <td>
                @if (u.id !== store.user()?.id) {
                  <button (click)="toggleAdmin(u)">{{ u.role === 'admin' ? 'Revoke admin' : 'Make admin' }}</button>
                }
              </td>
            </tr>
          }
        </table>
      }

      @if (run(); as d) {
        <section class="run-detail">
          <header>
            <h2>Run {{ d.run.id }} · {{ d.run.bot_name }} · {{ d.run.status }}</h2>
            <span class="grow"></span>
            @if (d.run.trace_id) { <button (click)="store.traceId.set(d.run.trace_id)">Trace</button> }
            <button (click)="run.set(null)">Close</button>
          </header>
          <p class="hint">user {{ d.run.owner ?? 'shared' }} · started by {{ d.run.actor ?? d.run.trigger }} ·
            {{ d.run.started_at }} · {{ d.run.usage.requests ?? 0 }} model requests</p>
          @if (d.run.error) { <p class="err">{{ d.run.error }}</p> }
          <h3>Timeline</h3>
          <table class="runs">
            @for (e of d.events; track e.id) {
              <tr [class.guard]="e.kind.startsWith('guardrail.')">
                <td>{{ e.ts.slice(11, 19) }}</td><td><code>{{ e.kind }}</code></td>
                <td class="detail wrap">{{ json(e.detail) }}</td>
              </tr>
            }
          </table>
          @for (m of d.messages; track m.id) {
            <h3>{{ m.role }}</h3>
            <div class="bubble md" [innerHTML]="m.content | markdown"></div>
          }
        </section>
      }
    </div>
  `,
})
export class AdminPage implements OnInit, OnDestroy {
  private api = inject(Api);
  protected store = inject(Store);
  protected ago = ago;
  protected tabs: Tab[] = ['activity', 'traces', 'runs', 'users'];
  protected traceKinds = ['chat', 'run', 'a2a', 'gateway', 'other'];
  protected kinds = ['auth.', 'run.', 'tool.', 'guardrail.', 'bot.', 'a2a.', 'admin.'];

  tab = signal<Tab>('activity');
  overview = signal<AdminOverview | null>(null);
  events = signal<AuditEvent[]>([]);
  runs = signal<RunInfo[]>([]);
  traces = signal<TraceInfo[]>([]);
  users = signal<AdminUser[]>([]);
  run = signal<{ run: RunInfo; messages: Msg[]; events: AuditEvent[] } | null>(null);
  error = signal<string | null>(null);
  kind = '';
  user = '';
  bot = '';
  session = '';
  traceKind = '';
  live = true;
  private timer?: ReturnType<typeof setInterval>;

  ngOnInit() {
    this.api.admin.overview().then((o) => (this.overview.set(o), this.store.traceUi.set(o.links.traces)), (e) => this.error.set(e.message));
    this.loadAudit();
    this.poll();
  }

  ngOnDestroy() {
    clearInterval(this.timer);
  }

  poll() {
    clearInterval(this.timer);
    if (this.live)
      this.timer = setInterval(() => (this.tab() === 'activity' ? this.loadAudit() : this.tab() === 'traces' && this.loadTraces()), 5000);
  }

  async show(t: Tab) {
    this.tab.set(t);
    await this.guard(async () => {
      if (t === 'activity') await this.loadAudit();
      if (t === 'traces') await this.loadTraces();
      if (t === 'runs') this.runs.set(await this.api.admin.runs());
      if (t === 'users') this.users.set(await this.api.admin.users());
    });
  }

  async loadAudit(older = false) {
    const q: Record<string, string> = {};
    if (this.kind) q['kind'] = this.kind;
    if (this.user) q['user'] = this.user.trim();
    if (this.bot) q['bot'] = this.bot.trim();
    const current = this.events();
    if (older && current.length) q['before'] = String(current[current.length - 1].id);
    await this.guard(async () => {
      const page = await this.api.admin.audit(q);
      this.events.set(older ? [...current, ...page] : page);
    });
  }

  async loadTraces(older = false) {
    const q: Record<string, string> = {};
    if (this.traceKind) q['kind'] = this.traceKind;
    if (this.user) q['user'] = this.user.trim();
    if (this.session) q['session_id'] = this.session.trim();
    if (this.bot) q['bot'] = this.bot.trim();
    const current = this.traces();
    if (older && current.length) q['before'] = current[current.length - 1].started_at;
    await this.guard(async () => {
      const page = await this.api.admin.traces(q);
      this.traces.set(older ? [...current, ...page] : page);
    });
  }

  ms(v: number) {
    return v >= 1000 ? `${(v / 1000).toFixed(1)}s` : `${Math.round(v)}ms`;
  }

  async openRun(id: string) {
    await this.guard(async () => this.run.set(await this.api.admin.run(id)));
  }

  async toggleAdmin(u: AdminUser) {
    await this.guard(async () => {
      await this.api.admin.setRole(u.id, u.role === 'admin' ? null : 'admin');
      this.users.set(await this.api.admin.users());
    });
  }

  json(v: unknown) {
    return JSON.stringify(v);
  }

  summary(e: AuditEvent): string {
    const d = e.detail ?? {};
    if (e.kind === 'tool.call') return `${d['tool']} → ${d['status']}`;
    if (e.kind === 'run.finished') return `${d['status']}${d['error'] ? ': ' + d['error'] : ''}`;
    if (e.kind === 'run.started') return `${d['trigger']} on ${d['model']}`;
    const s = JSON.stringify(d);
    return s === '{}' ? '' : s.length > 120 ? s.slice(0, 120) + '…' : s;
  }

  private async guard(fn: () => Promise<void>) {
    try {
      await fn();
      this.error.set(null);
    } catch (e) {
      this.error.set((e as Error).message);
    }
  }
}
