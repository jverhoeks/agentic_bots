import { Component, input, signal } from '@angular/core';
import { ToolCall } from './api';
import { argsSummary } from './util';

@Component({
  selector: 'app-tool-chip',
  template: `
    <div class="chip" [class]="call().status" (click)="open.set(!open())">
      <span class="chip-status">{{ call().status === 'running' ? '◌' : call().status === 'ok' ? '✓' : '✕' }}</span>
      <code>{{ call().name }}</code>
      <span class="chip-args">{{ summary() }}</span>
    </div>
    @if (open() && call().preview) {
      <pre class="chip-preview">{{ call().preview }}</pre>
    }
  `,
})
export class ToolChip {
  call = input.required<ToolCall>();
  open = signal(false);
  summary = () => argsSummary(this.call().args);
}
