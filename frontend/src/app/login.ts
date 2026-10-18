import { Component, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { Store } from './store';

/** Basic username/password login. The first account registers itself (registration then closes). */
@Component({
  selector: 'app-login',
  imports: [FormsModule],
  template: `
    <form class="login" (ngSubmit)="submit()">
      <div class="brand"><span class="logo">◆</span> Agentic Bots</div>
      <p class="hint">{{ register() ? 'Create your account. Your bots and connections are yours alone.' : 'Log in to see your bots.' }}</p>
      <label>Username
        <input name="username" [(ngModel)]="username" autocomplete="username" required
               pattern="[a-z0-9]+(-[a-z0-9]+)*" placeholder="e.g. jacob" />
      </label>
      <label>Password
        <input name="password" type="password" [(ngModel)]="password" required minlength="8"
               [autocomplete]="register() ? 'new-password' : 'current-password'" />
      </label>
      @if (error(); as err) {
        <p class="err">{{ err }}</p>
      }
      <button class="primary" type="submit" [disabled]="busy()">{{ register() ? 'Create account' : 'Log in' }}</button>
      @if (store.sso(); as sso) {
        <a class="sso" href="/api/auth/oidc/login">Log in with {{ sso }}</a>
      }
      @if (store.canRegister()) {
        <button type="button" class="link" (click)="register.set(!register())">
          {{ register() ? 'I have an account' : 'Create an account' }}
        </button>
      }
    </form>
  `,
})
export class Login {
  protected store = inject(Store);
  protected username = '';
  protected password = '';
  protected register = signal(this.store.canRegister());
  // the SSO callback redirects back with ?sso_error=… when the login failed
  protected error = signal<string | null>(new URLSearchParams(location.search).get('sso_error'));
  protected busy = signal(false);

  async submit() {
    this.busy.set(true);
    this.error.set(null);
    try {
      await this.store.login(this.username.trim().toLowerCase(), this.password, this.register());
    } catch (e) {
      this.error.set((e as Error).message);
    } finally {
      this.busy.set(false);
    }
  }
}
