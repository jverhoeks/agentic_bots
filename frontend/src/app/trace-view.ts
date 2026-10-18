import { Component, computed, effect, inject, signal } from '@angular/core';
import { Api, TraceDetail, TraceSpan } from './api';
import { Store } from './store';

interface Row {
  span: TraceSpan;
  depth: number;
  left: number; // % of the trace
  width: number;
}

/** Waterfall of one trace from the app database: chat turn or run → LLM calls → tools → gateway. */
@Component({
  selector: 'app-trace-view',
  template: `
    <div class="overlay" (click)="close()">
      <section class="trace-view" (click)="$event.stopPropagation()">
        <header>
          @if (detail(); as d) {
            <h2>{{ title(d) }}</h2>
          } @else {
            <h2>Trace</h2>
          }
          <span class="grow"></span>
          @if (external(); as url) { <a [href]="url" target="_blank">Trace UI ↗</a> }
          <button (click)="close()">Close</button>
        </header>
        @if (error()) { <p class="err">{{ error() }}</p> }
        @if (detail(); as d) {
          <div class="meta">
            <span class="pill">{{ d.trace.kind }}</span>
            @if (d.trace.user) { <span class="pill">user {{ d.trace.user }}</span> }
            @if (d.trace.session_id) { <span class="pill">session {{ d.trace.session_id }}</span> }
            @if (d.trace.actor && d.trace.actor !== d.trace.user) { <span class="pill">by {{ d.trace.actor }}</span> }
            <span class="pill">{{ ms(d.trace.duration_ms) }}</span>
            <span class="pill">{{ d.trace.llm_calls }} LLM calls · {{ d.trace.input_tokens }}/{{ d.trace.output_tokens }} tokens</span>
            <span class="pill">{{ d.trace.tool_calls }} tool calls</span>
            <span class="pill" [class.err]="d.trace.errors > 0">{{ d.trace.errors }} errors</span>
            <span class="pill">{{ d.trace.services.join(' + ') }}</span>
            @if (d.trace.parent_trace_id) {
              <button class="link" (click)="store.traceId.set(d.trace.parent_trace_id)">started from ↑</button>
            }
          </div>
          <div class="waterfall">
            @for (r of rows(); track r.span.span_id) {
              <div class="span-row" [class.open]="open() === r.span.span_id" [class.err]="r.span.status === 'error'"
                   (click)="toggle(r.span.span_id)">
                <div class="span-name" [style.padding-left.px]="8 + r.depth * 14" [title]="r.span.name">
                  <span class="svc" [class]="svcClass(r.span.service)"></span>{{ label(r.span) }}
                </div>
                <div class="span-bar">
                  <div class="bar" [class]="svcClass(r.span.service)" [style.left.%]="r.left" [style.width.%]="r.width"></div>
                  <span class="dur">{{ ms(r.span.duration_ms) }}</span>
                </div>
              </div>
              @if (open() === r.span.span_id) {
                <div class="span-attrs">
                  <div class="hint">{{ r.span.name }} · {{ r.span.service }} · {{ r.span.kind }} · {{ r.span.start }}
                    @if (r.span.status_message) { · <span class="err">{{ r.span.status_message }}</span> }</div>
                  @for (a of attrs(r.span); track a[0]) {
                    <div class="attr"><code>{{ a[0] }}</code>
                      @if (a[1].length > 120 || a[1].includes('\\n')) { <pre>{{ a[1] }}</pre> } @else { <span>{{ a[1] }}</span> }
                    </div>
                  }
                  @for (e of r.span.events; track $index) {
                    <div class="attr"><code>event {{ e.name }}</code><pre>{{ json(e.attrs) }}</pre></div>
                  }
                </div>
              }
            }
          </div>
        } @else if (!error()) {
          <p class="hint">Loading…</p>
        }
      </section>
    </div>
  `,
})
export class TraceView {
  private api = inject(Api);
  protected store = inject(Store);
  detail = signal<TraceDetail | null>(null);
  error = signal<string | null>(null);
  open = signal<string | null>(null);

  external = computed(() => {
    const id = this.store.traceId();
    const base = this.store.traceUi();
    return id && base ? `${base}/trace/${id}` : null;
  });

  rows = computed<Row[]>(() => {
    const d = this.detail();
    if (!d || !d.spans.length) return [];
    const t = (s: TraceSpan) => new Date(s.start).getTime();
    const t0 = Math.min(...d.spans.map(t));
    const t1 = Math.max(...d.spans.map((s) => t(s) + s.duration_ms));
    const total = Math.max(t1 - t0, 1);
    const ids = new Set(d.spans.map((s) => s.span_id));
    const kids = new Map<string | null, TraceSpan[]>();
    for (const s of d.spans) {
      const parent = s.parent_id && ids.has(s.parent_id) ? s.parent_id : null; // orphans become roots
      kids.set(parent, [...(kids.get(parent) ?? []), s]);
    }
    const out: Row[] = [];
    const walk = (parent: string | null, depth: number) => {
      for (const s of (kids.get(parent) ?? []).sort((a, b) => t(a) - t(b))) {
        out.push({ span: s, depth, left: ((t(s) - t0) / total) * 100, width: Math.max((s.duration_ms / total) * 100, 0.4) });
        walk(s.span_id, depth + 1);
      }
    };
    walk(null, 0);
    return out;
  });

  constructor() {
    effect(() => {
      const id = this.store.traceId();
      this.detail.set(null);
      this.error.set(null);
      this.open.set(null);
      if (id) this.api.trace(id).then((d) => this.detail.set(d), (e) => this.error.set((e as Error).message));
    });
  }

  close() {
    this.store.traceId.set(null);
  }

  toggle(id: string) {
    this.open.set(this.open() === id ? null : id);
  }

  title(d: TraceDetail) {
    const t = d.trace;
    return `${t.name || t.kind}${t.bot ? ' · ' + t.bot : ''} · ${new Date(t.started_at).toLocaleString()}`;
  }

  label(s: TraceSpan): string {
    const a = s.attrs;
    const op = a['gen_ai.operation.name'];
    if (op === 'execute_tool') return `🔧 ${a['gen_ai.tool.name'] ?? s.name.replace('execute_tool ', '')}`;
    if (op === 'chat') return `🧠 ${a['gen_ai.request.model'] ?? s.name}`;
    if (s.service === 'agentgateway') return `⇄ gateway ${s.name}`;
    return s.name;
  }

  attrs(s: TraceSpan): [string, string][] {
    return Object.entries(s.attrs)
      .filter(([k]) => k !== 'logfire.json_schema' && k !== 'logfire.msg')
      .map(([k, v]) => [k, typeof v === 'string' ? this.pretty(v) : JSON.stringify(v, null, 2)]);
  }

  svcClass(service: string) {
    return service === 'agentgateway' ? 'gw' : 'be';
  }

  ms(v: number) {
    return v >= 1000 ? `${(v / 1000).toFixed(1)}s` : `${Math.round(v)}ms`;
  }

  json(v: unknown) {
    return JSON.stringify(v, null, 2);
  }

  private pretty(v: string) {
    if (!/^[[{]/.test(v)) return v;
    try {
      return JSON.stringify(JSON.parse(v), null, 2);
    } catch {
      return v;
    }
  }
}
