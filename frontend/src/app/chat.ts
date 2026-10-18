import { Component, ElementRef, computed, effect, inject, input, signal, viewChild } from '@angular/core';
import { MarkdownPipe } from './markdown.pipe';
import { Store } from './store';
import { ToolChip } from './tool-chip';

@Component({
  selector: 'app-chat',
  imports: [MarkdownPipe, ToolChip],
  template: `
    <div class="messages" #scroller>
      @for (m of messages(); track $index) {
        <div class="msg" [class]="m.role">
          @if (m.run_id) {
            <div class="run-tag">Run {{ m.run_id }} · {{ m.created_at ? time(m.created_at) : '' }}</div>
          }
          @if (m.tool_calls.length) {
            <div class="chips">
              @for (t of m.tool_calls; track t.id) { <app-tool-chip [call]="t" /> }
            </div>
          }
          @if (m.role === 'notice') {
            <div class="bubble md notice" [innerHTML]="m.content | markdown"></div>
          } @else if (m.role === 'user') {
            <div class="bubble">{{ m.content }}</div>
          } @else {
            <div class="bubble md" [innerHTML]="m.content | markdown"></div>
            @if (m.trace_id) {
              <button class="link trace-link" (click)="store.traceId.set(m.trace_id)" title="LLM calls, tools and gateway requests of this answer">trace</button>
            }
          }
        </div>
      } @empty {
        @if (!streaming()) { <div class="empty">No messages yet.</div> }
      }
      @if (streaming(); as st) {
        <div class="msg assistant">
          @if (st.tools.length) {
            <div class="chips">
              @for (t of st.tools; track t.id) { <app-tool-chip [call]="t" /> }
            </div>
          }
          <div class="bubble md">
            @if (st.text) { <div [innerHTML]="st.text | markdown"></div> }
            <span class="typing"><i></i><i></i><i></i></span>
          </div>
        </div>
      }
    </div>

    <form class="composer" (submit)="submit($event)">
      <textarea #box rows="1" [placeholder]="placeholder()" [value]="draft()" [disabled]="busy()"
                (input)="draft.set(box.value); autosize(box)" (keydown.enter)="enter($event)"></textarea>
      <button type="submit" [disabled]="busy() || !draft().trim()">Send</button>
    </form>
  `,
})
export class Chat {
  protected store = inject(Store);
  sessionId = input.required<string>();
  placeholder = input('Message…');

  draft = signal('');
  private scroller = viewChild<ElementRef<HTMLElement>>('scroller');

  messages = computed(() => this.store.messages()[this.sessionId()] ?? []);
  streaming = computed(() => this.store.streaming()[this.sessionId()]);
  busy = computed(() => !!this.streaming());

  constructor() {
    effect(() => {
      this.messages();
      this.streaming();
      const el = this.scroller()?.nativeElement;
      if (el) queueMicrotask(() => (el.scrollTop = el.scrollHeight));
    });
  }

  time(iso: string) {
    return new Date(iso).toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' });
  }

  enter(e: Event) {
    const ke = e as KeyboardEvent;
    if (!ke.shiftKey) this.submit(e);
  }

  autosize(el: HTMLTextAreaElement) {
    el.style.height = 'auto';
    el.style.height = Math.min(el.scrollHeight, 200) + 'px';
  }

  submit(e: Event) {
    e.preventDefault();
    const text = this.draft().trim();
    if (!text || this.busy()) return;
    this.draft.set('');
    this.store.send(this.sessionId(), text);
  }
}
