import { Component, computed, effect, inject, input, signal } from '@angular/core';
import { Api, Bot, BotDetail, MemoryItem, Verbosity } from './api';
import { Store } from './store';
import { ToolChip } from './tool-chip';
import { ago } from './util';

@Component({
  selector: 'app-bot-panel',
  imports: [ToolChip],
  template: `
    <header class="session-header">
      <div class="grow">
        <h1>{{ bot().title }}</h1>
        <p>{{ bot().description }}</p>
        <div class="meta">
          <span class="pill">model: {{ bot().model }}</span>
          @if (bot().schedule; as cron) {
            <span class="pill" [title]="'next run ' + (bot().next_run ?? '')">schedule: <code>{{ cron }}</code>
              @if (bot().next_run; as next) { · next {{ when(next) }} }</span>
          } @else {
            <span class="pill">trigger: {{ bot().trigger }}</span>
          }
          <label class="pill" title="How much the bot writes in reports and answers">
            length:
            <select [value]="bot().verbosity" (change)="setVerbosity($any($event.target).value)">
              <option value="short">short</option>
              <option value="medium">medium</option>
              <option value="verbose">verbose</option>
            </select>
          </label>
          @for (t of bot().tools; track t) { <span class="pill tool">{{ t }}</span> }
        </div>
      </div>
      <div class="actions">
        <button class="primary" [disabled]="running()" (click)="store.runBot(bot().name)">
          {{ running() ? 'Running…' : '▶ Run now' }}
        </button>
        <button (click)="tab.set(tab() === 'spec' ? null : 'spec')">SKILL.md</button>
        <button (click)="tab.set(tab() === 'runs' ? null : 'runs')">Runs</button>
        <button (click)="toggleMemory()">Memory</button>
        <button class="ghost" title="Clear conversation" (click)="store.clear(bot().session_id)">Clear</button>
        <button class="ghost danger" title="Delete bot" (click)="remove()">Delete</button>
      </div>
    </header>

    @if (activity().length) {
      <div class="activity">
        <span class="activity-label">Run in progress</span>
        @for (t of activity(); track t.id) { <app-tool-chip [call]="t" /> }
      </div>
    }

    @if (tab() === 'spec' && detail(); as d) {
      <pre class="drawer">{{ d.skill_md }}</pre>
    }
    @if (tab() === 'runs' && detail(); as d) {
      <div class="drawer">
        <table class="runs">
          <tr><th>Run</th><th>Status</th><th>Started</th><th>Requests</th><th>Tool calls</th><th>Tokens in/out</th></tr>
          @for (r of d.runs; track r.id) {
            <tr>
              <td><code>{{ r.id }}</code></td>
              <td><span class="dot" [class]="r.status"></span> {{ r.status }}</td>
              <td>{{ ago(r.started_at) }}</td>
              <td>{{ r.usage.requests ?? '' }}</td>
              <td>{{ r.usage.tool_calls ?? '' }}</td>
              <td>{{ r.usage.input_tokens ?? '' }} / {{ r.usage.output_tokens ?? '' }}</td>
            </tr>
            @if (r.error) { <tr><td colspan="6" class="err">{{ r.error }}</td></tr> }
          } @empty {
            <tr><td colspan="6">No runs yet.</td></tr>
          }
        </table>
      </div>
    }
    @if (tab() === 'memory') {
      <div class="drawer">
        <table class="runs memory">
          <tr><th>Key</th><th>Value</th><th>Updated</th><th></th></tr>
          @for (m of memory(); track m.key) {
            <tr>
              <td><code>{{ m.key }}</code></td>
              <td><pre class="mem-value">{{ m.value }}</pre></td>
              <td>{{ ago(m.updated_at) }}</td>
              <td><button class="ghost" title="Forget this" (click)="forget(m.key)">Forget</button></td>
            </tr>
          } @empty {
            <tr><td colspan="4">Nothing remembered yet.</td></tr>
          }
        </table>
        @if (memory().length) {
          <button class="ghost danger" (click)="forget()">Forget everything</button>
        }
      </div>
    }
  `,
})
export class BotPanel {
  protected store = inject(Store);
  private api = inject(Api);
  protected ago = ago;

  bot = input.required<Bot>();
  tab = signal<'spec' | 'runs' | 'memory' | null>(null);
  memory = signal<MemoryItem[]>([]);
  detail = signal<BotDetail | null>(null);

  running = computed(() => this.bot().last_run?.status === 'running');
  activity = computed(() => this.store.runActivity()[this.bot().name] ?? []);

  constructor() {
    // reload details whenever the bot (or its last run) changes
    effect(() => {
      const b = this.bot();
      b.last_run?.status;
      this.api.bot(b.name).then((d) => this.detail.set(d));
      if (this.tab() === 'memory') this.loadMemory();
    });
  }

  async setVerbosity(v: Verbosity) {
    try {
      await this.api.patchBot(this.bot().name, { verbosity: v });
      await this.store.refresh();
    } catch (e) {
      this.store.error.set(String(e));
    }
  }

  toggleMemory() {
    this.tab.set(this.tab() === 'memory' ? null : 'memory');
    if (this.tab() === 'memory') this.loadMemory();
  }

  private loadMemory() {
    this.api.memory(this.bot().name).then((m) => this.memory.set(m));
  }

  async forget(key?: string) {
    if (!key && !confirm(`Forget everything "${this.bot().title}" remembers?`)) return;
    await this.api.forget(this.bot().name, key);
    this.loadMemory();
  }

  when(iso: string) {
    return new Date(iso).toLocaleString([], { weekday: 'short', day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });
  }

  remove() {
    if (confirm(`Delete bot "${this.bot().title}" and its conversation?`)) this.store.deleteBot(this.bot().name);
  }
}
