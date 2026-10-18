import { Component, computed, inject } from '@angular/core';
import { MarkdownPipe } from './markdown.pipe';
import { Store } from './store';

/** The bot the tutorial builds. Small on purpose: a run costs a few LLM calls, not a full digest. */
export const TUTORIAL_BOT = 'tutorial-hn';
/** The team tutorial: two specialists and a chief that delegates to them over A2A. */
const TEAM = { hn: 'team-hn-scout', web: 'team-web-scout', chief: 'team-chief' };

interface Step {
  title: string;
  /** Markdown. */
  body: string;
  action?: { label: string; run: () => void };
  /** Shown as ✓ once true; the user still clicks Next. */
  done?: () => boolean;
}

interface Guide {
  id: string;
  title: string;
  summary: string;
  bots: string[];
  steps: Step[];
}

/** Tutorial mode: a guide card over the app. A picker, then the steps of one tutorial. */
@Component({
  selector: 'app-tutorial',
  imports: [MarkdownPipe],
  template: `
    @if (picking()) {
      <aside class="tutorial">
        <header>
          <span class="tutorial-count">Tutorials</span>
          <span class="grow"></span>
          <button class="ghost" title="Close" (click)="store.tutorial.set(null)">✕</button>
        </header>
        @for (g of guides; track g.id) {
          <div class="tutorial-pick">
            <h3>{{ g.title }}</h3>
            <p>{{ g.summary }}</p>
            <button class="primary" (click)="start(g.id)">Start ({{ g.steps.length }} steps)</button>
          </div>
        }
      </aside>
    } @else if (step(); as s) {
      <aside class="tutorial">
        <header>
          <span class="tutorial-count">{{ guide().title }} {{ index() + 1 }}/{{ steps().length }}</span>
          <span class="grow"></span>
          <button class="ghost" title="Leave the tutorial (you can restart it from the sidebar)" (click)="store.tutorial.set(null)">✕</button>
        </header>
        <h3>@if (s.done?.()) { <span class="ok">✓</span> } {{ s.title }}</h3>
        <div class="md" [innerHTML]="s.body | markdown"></div>
        <footer>
          @if (index() === 0) {
            <button title="All tutorials" (click)="store.tutorial.set({ id: null, step: 0 })">All tutorials</button>
          } @else {
            <button (click)="go(-1)">Back</button>
          }
          <span class="grow"></span>
          @if (s.action; as a) {
            <button [class.primary]="!s.done?.()" [disabled]="busy()" (click)="a.run()">{{ a.label }}</button>
          }
          @if (index() < steps().length - 1) {
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

  private botOf = (name: string) => this.store.bots().find((b) => b.name === name);
  private sessionOf = (name: string) => this.botOf(name)?.session_id ?? `bot-${name}`;
  private messagesOf = (name: string) => this.store.messages()[this.sessionOf(name)] ?? [];
  private bot = computed(() => this.botOf(TUTORIAL_BOT));
  private botMessages = computed(() => this.messagesOf(TUTORIAL_BOT));
  /** Waiting for an answer in Main or a tutorial bot's chat, or for a run of one. */
  protected busy = computed(() => {
    const st = this.store.streaming();
    return !!st[this.store.mainId()] || this.guide().bots.some((b) => !!st[this.sessionOf(b)] || this.botOf(b)?.last_run?.status === 'running');
  });

  private open = (name: string) => this.store.open({ kind: 'session', id: this.sessionOf(name) });
  private openBot = () => this.open(TUTORIAL_BOT);
  private sayTo = async (name: string, text: string) => {
    await this.open(name);
    await this.store.send(this.sessionOf(name), text);
  };
  private say = (text: string) => this.sayTo(TUTORIAL_BOT, text);
  private askMain = async (text: string) => {
    await this.store.open({ kind: 'session', id: this.store.mainId() });
    await this.store.send(this.store.mainId(), text);
  };
  private viewing = (name: string) => this.store.view().kind === 'session' && this.store.currentBot()?.name === name;
  private ran = (name: string) => ['ok', 'failed'].includes(this.botOf(name)?.last_run?.status ?? '');

  private readonly basics: Step[] = [
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
        run: () =>
          this.askMain(
            `Create a bot named "${TUTORIAL_BOT}" (title "Tutorial: AI on Hacker News") that lists the 5 most relevant ` +
            'AI stories on Hacker News from the last 24 hours, one line each with a link. No comment threads, keep it short. Do not run it yet.'),
      },
      done: () => !!this.bot(),
    },
    {
      title: 'Meet your bot',
      body: `The bot is in the sidebar now. Its header shows the **model**, the **tools** it was granted (the gateway
lets it call only these) and the **length** of its answers. **SKILL.md** shows the full definition: an open
standard file in \`bots/${TUTORIAL_BOT}/\`, which you can also edit by hand.`,
      action: { label: 'Open the bot', run: this.openBot },
      done: () => this.viewing(TUTORIAL_BOT),
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
      done: () => this.ran(TUTORIAL_BOT),
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

  private readonly team: Step[] = [
    {
      title: 'Bots that work together',
      body: `A bot can hand work to other bots with the **a2a** tool (Agent-to-Agent). The usual pattern is a
**chief**: it splits a job, gives each part to a **specialist**, and combines what comes back.

You build a small team here: two scouts, \`${TEAM.hn}\` and \`${TEAM.web}\`, and \`${TEAM.chief}\`, which turns their
findings into one briefing. It only uses Hacker News and web search, so you need no accounts.`,
    },
    {
      title: 'Build the first specialist',
      body: `A specialist does one thing well with only the tools it needs. You can run and test it on its own,
and reuse it in more than one team.`,
      action: {
        label: 'Ask Main',
        run: () =>
          this.askMain(
            `Create a bot named "${TEAM.hn}" (title "Team: HN scout") that lists the 3 most relevant AI stories on ` +
              'Hacker News from the last 24 hours, one line each with a link and the points. Keep it short. Do not run it yet.',
          ),
      },
      done: () => !!this.botOf(TEAM.hn),
    },
    {
      title: 'Build the second specialist',
      body: `This scout searches the wider web. It also handles a specific question, so the chief can send it
follow-up work later.`,
      action: {
        label: 'Ask Main',
        run: () =>
          this.askMain(
            `Create a bot named "${TEAM.web}" (title "Team: web scout") that searches the web for the 3 most important ` +
              'AI news items of the last 24 hours that are not from Hacker News, one line each with a link and the source. ' +
              'When it is asked about a specific story or topic instead, it researches that and answers with sources. ' +
              'Keep it short. Do not run it yet.',
          ),
      },
      done: () => !!this.botOf(TEAM.web),
    },
    {
      title: 'Build the chief',
      body: `The chief gets only the **a2a** tools. With them it can:
- \`a2a_list_agents\`: see your bots;
- \`a2a_start_task\` + \`a2a_get_task\`: start several bots at once and collect their results;
- \`a2a_send_message\`: ask one bot and wait for the answer;
- \`a2a_get_last_result\`: read a bot's latest result without starting a new run.

The instructions are a numbered list of who to ask, and how to combine the answers.`,
      action: {
        label: 'Ask Main',
        run: () =>
          this.askMain(
            `Create a bot named "${TEAM.chief}" (title "Team: chief of staff") that writes my AI morning briefing by ` +
              `delegating, with only the a2a tools. Steps: 1) start tasks for ${TEAM.hn} and ${TEAM.web} at the same time ` +
              'with a2a_start_task; 2) collect both results with a2a_get_task; 3) merge them: drop duplicates and keep the ' +
              '5 items that matter most, one line each with a link and why it matters; 4) end with one question worth ' +
              'digging into. It does no research itself. Do not run it yet.',
          ),
      },
      done: () => !!this.botOf(TEAM.chief),
    },
    {
      title: 'Run the chief',
      body: `Watch the sidebar: the scouts' dots start blinking while the chief waits for them. Both run at the
same time. Each request from the chief is a normal run of that scout, started by \`a2a:${TEAM.chief}\`.`,
      action: {
        label: '▶ Run the chief',
        run: async () => {
          await this.open(TEAM.chief);
          await this.store.runBot(TEAM.chief);
        },
      },
      done: () => this.ran(TEAM.chief),
    },
    {
      title: 'See what a specialist did',
      body: `The chief's request and the scout's answer are in the scout's own chat, so you can check exactly what
each specialist did and ask it something yourself.`,
      action: { label: 'Open the HN scout', run: () => this.open(TEAM.hn) },
      done: () => this.viewing(TEAM.hn),
    },
    {
      title: 'Delegate a follow-up',
      body: `In its chat the chief delegates too. Here it uses \`a2a_send_message\` to ask the web scout and waits
for the answer. Under the answer, **trace** shows the a2a calls and how long each one took.`,
      action: {
        label: 'Dig into item 1',
        run: () => this.sayTo(TEAM.chief, `Ask ${TEAM.web} to dig deeper into item 1 of the briefing, and tell me what it found.`),
      },
      done: () => this.messagesOf(TEAM.chief).some((m) => m.role === 'user' && !m.run_id),
    },
    {
      title: 'Teams worth building',
      body: `- **Chief of staff.** A PR bot (GitHub), an inbox bot (Gmail, read-only) and a calendar bot, combined into
  "what needs me today". Connect the apps on the **Connections** page first.
- **Research desk.** A researcher finds sources, a fact-checker opens each one and flags weak claims, and the
  chief writes up only what holds.
- **Release manager.** One bot lists merged PRs, another lists open blockers. The chief drafts release notes
  and a go/no-go list.
- **Competitor watch.** One scout per competitor. Each week the chief compares launches, pricing and hiring.
- **Helpdesk router.** A front bot reads the question, passes it to the right specialist (IT, HR, finance
  docs) and passes the answer back.
- **Second opinion.** Two bots on different models answer the same question; the chief points out where
  they disagree.`,
    },
    {
      title: 'Good to know',
      body: `- **Two levels deep.** The chief can ask a specialist, and that one can ask one more bot. Loops and
  self-calls are refused.
- **Only your own bots.** A chief cannot reach another user's bots.
- **Specialists do not change themselves** when another bot calls them. That only happens from their own
  chat or runs.
- **Cheaper mornings.** Schedule the specialists a bit earlier (for example 07:30) and let the chief use
  \`a2a_get_last_result\` at 08:00, instead of starting new runs.
- **Outside agents.** Agents listed in \`config/a2a-agents.json\` can be called with \`a2a_remote_send_message\`.`,
    },
    {
      title: 'Done',
      body: `You built a team: specialists that each do one thing, and a chief that delegates and combines. Now ask
Main for a team of your own.

The tutorial bots are just bots: keep them, or delete them.`,
      action: {
        label: 'Delete the team bots',
        run: async () => {
          const mine = Object.values(TEAM).filter((b) => this.botOf(b));
          if (!mine.length || !confirm(`Delete ${mine.join(', ')} and their conversations?`)) return;
          for (const b of mine) await this.store.deleteBot(b);
        },
      },
      done: () => !Object.values(TEAM).some((b) => this.botOf(b)),
    },
  ];

  readonly guides: Guide[] = [
    {
      id: 'basics',
      title: 'Your first bot',
      summary: 'Build, run, follow up, change and schedule one bot. About 5 minutes.',
      bots: [TUTORIAL_BOT],
      steps: this.basics,
    },
    {
      id: 'team',
      title: 'A team of bots',
      summary: 'A chief of staff that hands work to two specialist bots over A2A and merges their results. Plus ideas for other teams.',
      bots: Object.values(TEAM),
      steps: this.team,
    },
  ];

  protected picking = computed(() => this.store.tutorial() !== null && !this.guides.some((g) => g.id === this.store.tutorial()?.id));
  protected guide = computed(() => this.guides.find((g) => g.id === this.store.tutorial()?.id) ?? this.guides[0]);
  protected steps = computed(() => this.guide().steps);
  protected index = computed(() => Math.min(this.store.tutorial()?.step ?? 0, this.steps().length - 1));
  protected step = computed(() => (this.store.tutorial() === null || this.picking() ? null : this.steps()[this.index()]));

  start(id: string) {
    this.store.tutorial.set({ id, step: 0 });
  }

  go(delta: number) {
    this.store.tutorial.set({ id: this.guide().id, step: Math.max(0, Math.min(this.steps().length - 1, this.index() + delta)) });
  }
}
