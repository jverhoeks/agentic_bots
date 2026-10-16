import { Component, OnInit, computed, inject } from '@angular/core';
import { BotPanel } from './bot-panel';
import { Chat } from './chat';
import { Store } from './store';
import { ToolsPage } from './tools-page';
import { ago } from './util';

@Component({
  selector: 'app-root',
  imports: [Chat, BotPanel, ToolsPage],
  template: `
    <aside class="sidebar">
      <div class="brand"><span class="logo">◆</span> Agentic Bots</div>

      <button class="nav" [class.active]="isActive('main')" (click)="store.open({ kind: 'session', id: 'main' })">
        <span class="nav-icon">✦</span>
        <span class="nav-text"><b>Main</b><small>Create and manage bots</small></span>
      </button>

      <div class="section">Bots</div>
      @for (bot of store.bots(); track bot.name) {
        <div class="bot-row">
          <button class="nav" [class.active]="isActive(bot.session_id)"
                  (click)="store.open({ kind: 'session', id: bot.session_id })">
            <span class="dot" [class]="bot.last_run?.status ?? 'idle'"></span>
            <span class="nav-text">
              <b>{{ bot.title }}</b>
              <small>{{ bot.last_run ? (bot.last_run.status === 'running' ? 'running…' : 'ran ' + ago(bot.last_run.started_at)) : 'never run' }}</small>
            </span>
          </button>
          <button class="run-btn" [disabled]="bot.last_run?.status === 'running'"
                  [title]="'Run ' + bot.title + ' now'" (click)="store.runBot(bot.name)">▶</button>
        </div>
      } @empty {
        <p class="hint">No bots yet. Ask Main to create one.</p>
      }

      <div class="spacer"></div>
      <button class="nav" [class.active]="store.view().kind === 'tools'" (click)="store.open({ kind: 'tools' })">
        <span class="nav-icon">⚙</span><span class="nav-text"><b>Tools</b><small>MCP gateway catalog</small></span>
      </button>
    </aside>

    <main class="content">
      @if (store.error(); as err) {
        <div class="banner" (click)="store.error.set(null)">{{ err }}</div>
      }
      @if (store.view().kind === 'tools') {
        <app-tools-page />
      } @else if (sessionId(); as sid) {
        @if (store.currentBot(); as bot) {
          <app-bot-panel [bot]="bot" />
        } @else {
          <header class="session-header">
            <div>
              <h1>Main</h1>
              <p>Describe a recurring task and I'll turn it into a bot. Try: <em>"create a bot to get the daily AI news from hackernews"</em></p>
            </div>
          </header>
        }
        <app-chat [sessionId]="sid" [placeholder]="placeholder()" />
      }
    </main>
  `,
})
export class App implements OnInit {
  protected store = inject(Store);
  protected ago = ago;

  protected sessionId = computed(() => {
    const v = this.store.view();
    return v.kind === 'session' ? v.id : null;
  });
  protected placeholder = computed(() =>
    this.store.currentBot() ? 'Ask a follow-up about the results…' : 'Describe the bot you want…',
  );

  isActive(id: string) {
    const v = this.store.view();
    return v.kind === 'session' && v.id === id;
  }

  ngOnInit() {
    this.store.init();
  }
}
