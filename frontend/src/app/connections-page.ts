import { Component, OnDestroy, OnInit, inject, signal } from '@angular/core';
import { Store } from './store';

interface Field { key: string; label: string; inputType: string; required: boolean; secret: boolean; placeholder?: string; description?: string }
interface AuthOption { type: 'oauth2' | 'api_key' | 'custom_credential'; label: string; description: string; ready?: boolean; fields?: Field[] }
interface Provider { service: string; name: string; categories: string[]; homepage: string; auth: AuthOption[] }
interface Connection { id: string; service: string; authType: string; account: string }

/**
 * The user's own SaaS connections (open-connector), through the apps broker. The broker is called
 * directly (`/connections/api`, proxied by the UI server), not through the backend: API keys typed
 * here go to the connector side only. The backend only hands out a short token naming the user.
 */
@Component({
  selector: 'app-connections-page',
  template: `
    <header class="session-header">
      <div class="grow">
        <h1>Connections</h1>
        <p>Your accounts in SaaS apps (Gmail, GitHub, Slack, …). Bots you grant the <code>apps</code> tool use them
          as you, and only yours. Keys and sign-ins go straight to the connector service; this app never sees them.</p>
      </div>
      <div class="actions">
        @if (store.user()?.role === 'admin') {
          <a class="pill" href="http://localhost:3010" target="_blank"
             title="Admins: set up OAuth clients (e.g. Google) and see all connections">Connector console ↗</a>
        }
        <button (click)="load()">Refresh</button>
      </div>
    </header>
    <div class="connections">
      @if (error()) { <div class="banner" (click)="error.set(null)">{{ error() }}</div> }

      <section>
        <h2>Connected</h2>
        @for (c of mine(); track c.id) {
          <div class="tool-card conn">
            <div class="grow"><b>{{ nameOf(c.service) }}</b> <code>{{ c.service }}</code>
              @if (c.account) { <span class="pill">{{ c.account }}</span> }
              <span class="pill">{{ authLabel(c.authType) }}</span>
            </div>
            <button [disabled]="busy()" (click)="disconnect(c)">Disconnect</button>
          </div>
        } @empty {
          <p class="hint">Nothing connected yet. Find an app below.</p>
        }
        @if (waiting(); as w) {
          <p class="hint">Waiting for the sign-in to {{ w }} to finish in the other window…
            <button class="link" (click)="stopWaiting()">stop waiting</button></p>
        }
      </section>

      <section>
        <h2>Add a connection</h2>
        <input class="search" placeholder="Search {{ total() || '' }} apps: gmail, github, slack, notion…" [value]="query()"
               (input)="search($any($event.target).value)" />
        @for (p of providers(); track p.service) {
          <div class="tool-card provider" [class.open]="selected() === p.service">
            <div class="tool-title" (click)="toggle(p)">
              <b>{{ p.name }}</b> <code>{{ p.service }}</code>
              @for (cat of p.categories; track cat) { <span class="pill">{{ cat }}</span> }
              @if (isConnected(p.service)) { <span class="pill ok">connected</span> }
            </div>
            @if (selected() === p.service) {
              @for (a of p.auth; track a.type) {
                <div class="auth-option">
                  @if (a.type === 'oauth2') {
                    @if (a.ready) {
                      <button class="primary" [disabled]="busy()" (click)="oauth(p)">Sign in with {{ p.name }}</button>
                    } @else {
                      <p class="hint">Sign-in with {{ p.name }} needs an OAuth client, which an admin sets up once in the
                        connector console.@if (p.auth.length > 1) { Or use another option below. }</p>
                    }
                  } @else {
                    <form (submit)="$event.preventDefault(); submit(p, a, $any($event.target))">
                      <b>{{ a.label }}</b>
                      @if (a.description) { <p class="hint">{{ a.description }}</p> }
                      @for (f of a.fields ?? []; track f.key) {
                        <label>{{ f.label }}
                          <input [name]="f.key" [type]="f.secret || f.inputType === 'password' ? 'password' : 'text'"
                                 [placeholder]="f.placeholder ?? ''" [required]="f.required" autocomplete="off" />
                        </label>
                      }
                      <button class="primary" type="submit" [disabled]="busy()">Connect</button>
                    </form>
                  }
                </div>
              }
              @if (p.homepage) { <a class="hint" [href]="p.homepage" target="_blank">{{ p.homepage }}</a> }
            }
          </div>
        } @empty {
          @if (query()) { <p class="hint">No app matches “{{ query() }}”.</p> }
        }
      </section>
    </div>
  `,
})
export class ConnectionsPage implements OnInit, OnDestroy {
  protected store = inject(Store);
  mine = signal<Connection[]>([]);
  providers = signal<Provider[]>([]);
  total = signal(0);
  query = signal('');
  selected = signal<string | null>(null);
  busy = signal(false);
  error = signal<string | null>(null);
  waiting = signal<string | null>(null);
  private names = new Map<string, string>();
  private token: { value: string; until: number } | null = null;
  private poll?: ReturnType<typeof setInterval>;
  private debounce?: ReturnType<typeof setTimeout>;

  ngOnInit() {
    this.load();
    this.search('');
  }

  ngOnDestroy() {
    this.stopWaiting();
    clearTimeout(this.debounce);
  }

  private async call<T>(method: string, path: string, body?: unknown): Promise<T> {
    if (!this.token || Date.now() > this.token.until) {
      const r = await fetch('/api/auth/connections-token', { method: 'POST' });
      if (!r.ok) throw new Error('not logged in');
      const t = await r.json();
      this.token = { value: t.token, until: Date.now() + (t.expires_in - 60) * 1000 };
    }
    const r = await fetch(`/connections/api${path}`, {
      method,
      headers: { authorization: `Bearer ${this.token.value}`, ...(body ? { 'content-type': 'application/json' } : {}) },
      body: body ? JSON.stringify(body) : undefined,
    });
    const data = await r.json().catch(() => ({ error: `connector service: HTTP ${r.status}` }));
    if (!r.ok) throw new Error(data.error ?? `HTTP ${r.status}`);
    return data as T;
  }

  async load() {
    try {
      this.mine.set((await this.call<{ connections: Connection[] }>('GET', '/connections')).connections);
    } catch (e) {
      this.error.set(`Connections: ${(e as Error).message}`);
    }
  }

  search(q: string) {
    this.query.set(q);
    clearTimeout(this.debounce);
    this.debounce = setTimeout(async () => {
      try {
        const d = await this.call<{ total: number; providers: Provider[] }>('GET', `/providers?limit=30&q=${encodeURIComponent(q)}`);
        for (const p of d.providers) this.names.set(p.service, p.name);
        this.providers.set(d.providers);
        if (!q) this.total.set(d.total);
      } catch (e) {
        this.error.set(`Apps: ${(e as Error).message}`);
      }
    }, q ? 250 : 0);
  }

  toggle(p: Provider) {
    this.selected.set(this.selected() === p.service ? null : p.service);
  }

  isConnected = (service: string) => this.mine().some((c) => c.service === service);
  nameOf = (service: string) => this.names.get(service) ?? service;
  authLabel = (t: string) => ({ oauth2: 'signed in', api_key: 'API key', custom_credential: 'credentials' })[t] ?? t;

  private async act(fn: () => Promise<void>) {
    this.busy.set(true);
    this.error.set(null);
    try {
      await fn();
    } catch (e) {
      this.error.set((e as Error).message);
    } finally {
      this.busy.set(false);
    }
  }

  submit(p: Provider, a: AuthOption, form: HTMLFormElement) {
    const values = Object.fromEntries((a.fields ?? []).map((f) => [f.key, (form.elements.namedItem(f.key) as HTMLInputElement).value]));
    return this.act(async () => {
      await this.call('POST', `/connections/${p.service}`, { authType: a.type, values });
      form.reset();
      this.selected.set(null);
      await this.load();
    });
  }

  oauth(p: Provider) {
    // open the window before the request, or the browser treats it as an unrequested popup
    const win = window.open('about:blank', 'connect', 'popup,width=560,height=720');
    return this.act(async () => {
      try {
        const { authorizationUrl } = await this.call<{ authorizationUrl: string }>('POST', `/connections/${p.service}`, { authType: 'oauth2' });
        if (win) win.location.href = authorizationUrl;
        else window.open(authorizationUrl, '_blank');
      } catch (e) {
        win?.close();
        throw e;
      }
      this.wait(p, win);
    });
  }

  private wait(p: Provider, win: Window | null) {
    this.stopWaiting();
    this.waiting.set(p.name);
    const until = Date.now() + 5 * 60_000;
    this.poll = setInterval(async () => {
      await this.load();
      if (this.isConnected(p.service) || Date.now() > until) {
        if (this.isConnected(p.service)) win?.close();
        this.stopWaiting();
      }
    }, 2000);
  }

  stopWaiting() {
    clearInterval(this.poll);
    this.waiting.set(null);
  }

  disconnect(c: Connection) {
    if (!confirm(`Disconnect ${this.nameOf(c.service)}? Bots can no longer use it as you.`)) return;
    return this.act(async () => {
      await this.call('DELETE', `/connections/${c.service}`);
      await this.load();
    });
  }
}
