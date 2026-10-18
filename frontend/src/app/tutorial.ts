import { Component, computed, inject } from '@angular/core';
import { MarkdownPipe } from './markdown.pipe';
import { Store } from './store';

/** The bot the tutorial builds. Small on purpose: a run costs a few LLM calls, not a full digest. */
export const TUTORIAL_BOT = 'tutorial-hn';

interface Step {
  title: string;
  /** Markdown. */
  body: string;
  action?: { label: string; run: () => void };
  /** Shown as ✓ once true; the user still clicks Next. */
  done?: () => boolean;
}

/** Tutorial mode: a guide card over the app that walks through building, running and changing one bot. */
@Component({
  selector: 'app-tutorial',
  imports: [MarkdownPipe],
  template: `
    @if (step(); as s) {
      <aside class="tutorial">
        <header>
          <span class="tutorial-count">Tutorial {{ index() + 1 }}/{{ steps.length }}</span>
          <span class="grow"></span>
          <button class="ghost" title="Leave the tutorial (you can restart it from the sidebar)" (click)="store.tutorial.set(null)">✕</button>
        </header>
        <h3>@if (s.done?.()) { <span class="ok">✓</span> } {{ s.title }}</h3>
        <div class="md" [innerHTML]="s.body | markdown"></div>
        <footer>
          <button [disabled]="index() === 0" (click)="go(-1)">Back</button>
          <span class="grow"></span>
          @if (s.action; as a) {
            <button [class.primary]="!s.done?.()" [disabled]="busy()" (click)="a.run()">{{ a.label }}</button>
          }
          @if (index() < steps.length - 1) {
            <button [class.primary]="!s.action || s.done?.()" (click)="go(1)">Next</button>
          } @else {
            <button class="primary" (click)="store.tutorial.set(null)">Finish</button>
          }
        </footer>
      </aside>
    }
  `,
})
export class Tutorial {
  protected store = inject(Store);

  private bot = computed(() => this.store.bots().find((b) => b.name === TUTORIAL_BOT));
  private botSession = computed(() => this.bot()?.session_id ?? `bot-${TUTORIAL_BOT}`);
  private botMessages = computed(() => this.store.messages()[this.botSession()] ?? []);
  /** Waiting for an answer in Main or the bot chat, or for a run. */
  protected busy = computed(() => {
    const st = this.store.streaming();
    return !!st[this.store.mainId()] || !!st[this.botSession()] || this.bot()?.last_run?.status === 'running';
  });

  private openBot = () => this.store.open({ kind: 'session', id: this.botSession() });
  private say = async (text: string) => {
    await this.openBot();
    await this.store.send(this.botSession(), text);
  };

  readonly steps: Step[] = [
    {
      title: 'How it works',
      body: `You describe a recurring task in plain language. **Main** (the builder agent) turns it into a **bot**:
a \`SKILL.md\` file with instructions, a model and the tools it may use. Each bot has its own chat, where it runs
its task and answers follow-up questions.

This tutorial builds a small bot, \`${TUTORIAL_BOT}\`: the top AI stories on Hacker News. Each step has a button
that does the step for you; you can also type it yourself.`,
    },
    {
      title: 'Ask Main for a bot',
      body: `Main reads the tool catalog of the MCP gateway, picks only the tools the task needs, writes the
instructions and saves the bot. Watch the tool calls (\`list_tools\`, \`create_bot\`) appear in the chat.`,
      action: {
        label: 'Ask Main',
        run: async () => {
          await this.store.open({ kind: 'session', id: this.store.mainId() });
          await this.store.send(this.store.mainId(),
            `Create a bot named "${TUTORIAL_BOT}" (title "Tutorial: AI on Hacker News") that lists the 5 most relevant ` +
            'AI stories on Hacker News from the last 24 hours, one line each with a link. No comment threads, keep it short. Do not run it yet.');
        },
      },
      done: () => !!this.bot(),
    },
    {
      title: 'Meet your bot',
      body: `The bot is in the sidebar now. Its header shows the **model**, the **tools** it was granted (the gateway
lets it call only these) and the **length** of its answers. **SKILL.md** shows the full definition: an open
standard file in \`bots/${TUTORIAL_BOT}/\`, which you can also edit by hand.`,
      action: { label: 'Open the bot', run: this.openBot },
      done: () => this.store.view().kind === 'session' && this.store.currentBot()?.name === TUTORIAL_BOT,
    },
    {
      title: 'Run it',
      body: `A run executes the task in the background. The tool calls show live under the header, and the result
lands in the bot's chat. The dot in the sidebar turns green (or red) when it is done.`,
      action: {
        label: '▶ Run now',
        run: async () => {
          await this.openBot();
          await this.store.runBot(TUTORIAL_BOT);
        },
      },
      done: () => ['ok', 'failed'].includes(this.bot()?.last_run?.status ?? ''),
    },
    {
      title: 'Ask a follow-up',
      body: `The bot's chat keeps its history, so you can ask about the results. It can use its tools again, for
example to open a story.`,
      action: { label: 'Ask about story 1', run: () => this.say('Tell me more about the first story.') },
      done: () => this.botMessages().some((m) => m.role === 'user' && !m.run_id),
    },
    {
      title: 'Change it from its chat',
      body: `Ask the bot to change, in its own chat. It updates its own \`SKILL.md\` (checked like the builder does,
and logged in the audit log). Bots may also improve themselves during a run, for example when a source keeps failing.
Either way you get a 🔔 notice in the bot's chat and a banner, saying what changed.`,
      action: { label: 'Make it shorter', run: () => this.say('From now on, list 3 stories and start with a one-line TL;DR.') },
      done: () => this.botMessages().some((m) => m.role === 'notice'),
    },
    {
      title: 'Put it on a schedule',
      body: `A cron schedule makes the bot run by itself. The header then shows the schedule and the next run. To
send results somewhere (Slack, e-mail), a bot needs a tool that can deliver; granting it is your approval.`,
      action: { label: 'Every weekday at 07:00', run: () => this.say('Run every weekday at 07:00.') },
      done: () => !!this.bot()?.schedule,
    },
    {
      title: 'Look under the hood',
      body: `- **trace** under an answer shows every LLM call, tool call and gateway request of that answer, with tokens and timings.
- **Runs** in the bot header lists every run with its token use.
- **Tools** in the sidebar is the gateway's catalog: everything bots can be given.`,
      action: { label: 'Open Tools', run: () => this.store.open({ kind: 'tools' }) },
    },
    {
      title: 'Done',
      body: `That is the whole loop: describe → bot → run → follow up → refine → schedule. Now ask Main for a bot of your own.

The tutorial bot is just a bot: keep it, or delete it (which also stops its schedule).`,
      action: {
        label: 'Delete the tutorial bot',
        run: async () => {
          if (this.bot() && confirm(`Delete "${TUTORIAL_BOT}" and its conversation?`)) await this.store.deleteBot(TUTORIAL_BOT);
        },
      },
      done: () => !this.bot(),
    },
  ];

  protected index = computed(() => Math.min(this.store.tutorial() ?? 0, this.steps.length - 1));
  protected step = computed(() => (this.store.tutorial() === null ? null : this.steps[this.index()]));

  go(delta: number) {
    this.store.tutorial.set(Math.max(0, Math.min(this.steps.length - 1, this.index() + delta)));
  }
}
