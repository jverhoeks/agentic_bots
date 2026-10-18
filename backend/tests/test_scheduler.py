"""Scheduled runs: `metadata.schedule` (cron) in SKILL.md, started by the backend's scheduler loop."""

from datetime import datetime, timezone

import pytest

from app import scheduler, spec
from app.agents import bot_runner

BOT = "cron-bot"


def _bot(**kw) -> spec.BotSpec:
    return spec.BotSpec(name=BOT, title="Cron", description="d", instructions="# Goal\nA digest every weekday morning.",
                        model="test-model", owner="alice", **kw)


def test_schedule_round_trips_and_sets_the_trigger():
    b = _bot(schedule="0  7 * * 1-5")
    assert b.schedule == "0 7 * * 1-5" and b.trigger == "schedule"
    md = b.to_skill_md()
    assert "schedule: 0 7 * * 1-5" in md and "trigger: schedule" in md
    assert spec.BotSpec.from_skill_md(md).schedule == "0 7 * * 1-5"
    assert _bot(schedule="").schedule is None and _bot().trigger == "manual"
    for bad in ("every morning", "0 7 * *", "0 7 * * * *", "61 7 * * *"):
        with pytest.raises(ValueError):
            _bot(schedule=bad)


def test_due_only_in_the_window():
    t = lambda h, m: datetime(2026, 10, 19, h, m, tzinfo=timezone.utc)  # noqa: E731  a Monday
    assert scheduler.due("0 7 * * 1-5", t(6, 59), t(7, 0))
    assert not scheduler.due("0 7 * * 1-5", t(7, 0), t(7, 0).replace(second=30))
    assert not scheduler.due("0 7 * * 6", t(6, 59), t(7, 1))  # Saturdays only


def test_tick_starts_due_bots_and_skips_busy_ones(monkeypatch):
    spec.save_bot(_bot(schedule="*/5 * * * *"))
    started = []

    def start_run(bot, trigger="manual", prompt=None, actor=None):
        if started:
            raise RuntimeError("busy")
        started.append((bot.name, trigger, actor))

    monkeypatch.setattr(bot_runner, "start_run", start_run)
    try:
        since, now = datetime(2026, 10, 19, 7, 4, 50, tzinfo=timezone.utc), datetime(2026, 10, 19, 7, 5, 20, tzinfo=timezone.utc)
        assert BOT in scheduler.tick(since, now)
        assert started == [(BOT, "schedule", "schedule")]
        assert BOT not in scheduler.tick(since, now)  # still busy: this slot is skipped
        assert BOT not in scheduler.tick(now, now.replace(minute=6))
    finally:
        spec.delete_bot(BOT)


async def test_scheduled_run_is_a_real_run_of_the_owner(monkeypatch):
    from pydantic_ai import Agent
    from pydantic_ai.models.test import TestModel

    from app import telemetry, tracestore
    from app.db import db
    from app.models import Run
    from sqlmodel import select

    monkeypatch.setattr(bot_runner, "build_agent", lambda b, editable=False, via="bot-chat": Agent(TestModel(custom_output_text="digest")))
    spec.save_bot(_bot(schedule="*/5 * * * *"))
    bot_runner.ensure_session(spec.get_bot(BOT))
    try:
        since, now = datetime(2026, 10, 19, 7, 4, 50, tzinfo=timezone.utc), datetime(2026, 10, 19, 7, 5, 20, tzinfo=timezone.utc)
        assert scheduler.tick(since, now) == [BOT]
        with db() as s:
            run = s.exec(select(Run).where(Run.bot_name == BOT).order_by(Run.started_at.desc())).first()
        await bot_runner.wait(run.id)
        telemetry.flush()
        with db() as s:
            run = s.get(Run, run.id)
        assert run.status == "ok" and run.trigger == "schedule" and run.actor == "schedule"
        t = tracestore.get(run.trace_id)
        assert t.kind == "run" and t.user == "alice" and t.bot == BOT
    finally:
        spec.delete_bot(BOT)
