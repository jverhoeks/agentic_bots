"""Scheduled runs: a bot with `metadata.schedule` (cron, SCHEDULE_TIMEZONE) runs by itself.

A loop in the backend checks every 30 seconds which schedules fell due since the last check. Runs missed while the
backend was down are not caught up, and a bot that is still busy skips that slot. One backend process only: with
more replicas each would start the run (then move this to a job queue or Temporal).
"""

import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from croniter import croniter

from app import spec
from app.config import get_settings

log = logging.getLogger("app.scheduler")
TICK = 30


def due(schedule: str, since: datetime, now: datetime) -> bool:
    """Did `schedule` fire in (since, now]?"""
    return croniter(schedule, since).get_next(datetime) <= now


def next_run(schedule: str | None) -> str | None:
    if not schedule:
        return None
    return croniter(schedule, datetime.now(ZoneInfo(get_settings().schedule_timezone))).get_next(datetime).isoformat()


def tick(since: datetime, now: datetime) -> list[str]:
    """Start the runs that fell due; returns the bot names started."""
    from app.agents import bot_runner

    started = []
    for bot in spec.list_bots():
        if not bot.schedule or not due(bot.schedule, since, now):
            continue
        try:
            bot_runner.start_run(bot, trigger="schedule", actor="schedule")
            started.append(bot.name)
        except RuntimeError as e:  # still busy with the previous turn
            log.warning("skipping scheduled run of %s: %s", bot.name, e)
        except Exception:  # noqa: BLE001  one broken bot must not stop the others
            log.exception("scheduled run of %s failed to start", bot.name)
    return started


async def loop() -> None:
    zone = ZoneInfo(get_settings().schedule_timezone)
    since = datetime.now(zone)
    while True:
        await asyncio.sleep(TICK)
        now = datetime.now(zone)
        try:
            tick(since, now)
        except Exception:  # noqa: BLE001
            log.exception("scheduler tick failed")
        since = now
