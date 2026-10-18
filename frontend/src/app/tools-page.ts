import { Component, OnInit, computed, inject, signal } from '@angular/core';
import { Api, ToolInfo } from './api';

@Component({
  selector: 'app-tools-page',
  template: `
    <header class="session-header">
      <div class="grow">
        <h1>Tools</h1>
        <p>Everything bots can use, served by the MCP gateway
          @if (gateway()) { at <code>{{ gateway() }}</code> }.
          Add connectors in <code>gateway/config.yaml</code>.</p>
        @if (models(); as m) {
          <div class="meta">
            <span class="pill">builder: {{ m.builder }}</span>
            <span class="pill">default bot model: {{ m.default }}</span>
            @if (m.error) { <span class="pill err">models: {{ m.error }}</span> }
            @else { <span class="pill">{{ m.models.length }} models</span> }
          </div>
        }
      </div>
      <div class="actions"><button (click)="load(true)">Refresh</button></div>
    </header>
    <div class="tools">
      @if (error()) { <div class="banner">{{ error() }}</div> }
      @for (group of groups(); track group[0]) {
        <section>
          <h2>{{ group[0] }}</h2>
          @for (t of group[1]; track t.name) {
            <div class="tool-card">
              <div class="tool-title"><code>{{ t.name }}</code>
                @if (t.read_only) { <span class="pill">read-only</span> }
              </div>
              <p>{{ t.description }}</p>
              <div class="params">
                @for (p of params(t); track p) { <span class="pill tool">{{ p }}</span> }
              </div>
            </div>
          }
        </section>
      }
    </div>
  `,
})
export class ToolsPage implements OnInit {
  private api = inject(Api);
  tools = signal<ToolInfo[]>([]);
  gateway = signal<string | null>(null);
  error = signal<string | null>(null);
  models = signal<{ builder: string; default: string; models: string[]; error: string | null } | null>(null);

  groups = computed(() => {
    const g = new Map<string, ToolInfo[]>();
    for (const t of this.tools()) g.set(t.connector, [...(g.get(t.connector) ?? []), t]);
    return [...g.entries()];
  });

  ngOnInit() {
    this.load();
    this.api.models().then((m) => this.models.set(m));
  }

  async load(refresh = false) {
    try {
      const r = await this.api.tools(refresh);
      this.tools.set(r.tools);
      this.gateway.set(r.gateway[0]?.url ?? null);
      this.error.set(null);
    } catch (e) {
      this.error.set((e as Error).message);
    }
  }

  params(t: ToolInfo) {
    return Object.entries(t.input_schema?.properties ?? {}).map(
      ([k, v]) => `${k}${v.default !== undefined ? '=' + JSON.stringify(v.default) : ''}`,
    );
  }
}
