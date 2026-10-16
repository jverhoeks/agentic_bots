import { Component, computed, effect, inject, input, signal } from '@angular/core';
import { Api, Bot, BotDetail } from './api';
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
          <span class="pill">trigger: {{ bot().trigger }}</span>
          @for (t of bot().tools; track t) { <span class="pill tool">{{ t }}</span> }
        </div>
      </div>
      <div class="actions">
        <button class="primary" [disabled]="running()" (click)="store.runBot(bot().name)">
          {{ running() ? 'Running…' : '▶ Run now' }}
        </button>
        <button (click)="tab.set(tab() === 'spec' ? null : 'spec')">SKILL.md</button>
        <button (click)="tab.set(tab() === 'runs' ? null : 'runs')">Runs</button>
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
  `,
})
export class BotPanel {
  protected store = inject(Store);
  private api = inject(Api);
  protected ago = ago;

  bot = input.required<Bot>();
  tab = signal<'spec' | 'runs' | null>(null);
  detail = signal<BotDetail | null>(null);

  running = computed(() => this.bot().last_run?.status === 'running');
  activity = computed(() => this.store.runActivity()[this.bot().name] ?? []);

  constructor() {
    // reload details whenever the bot (or its last run) changes
    effect(() => {
      const b = this.bot();
      b.last_run?.status;
      this.api.bot(b.name).then((d) => this.detail.set(d));
    });
  }

  remove() {
    if (confirm(`Delete bot "${this.bot().title}" and its conversation?`)) this.store.deleteBot(this.bot().name);
  }
}
