import json

from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel

from app.agents.runtime import execute
from app.db import db
from app.models import Message, Session


async def collect(gen):
    return [ev async for ev in gen]


async def test_execute_streams_and_persists():
    with db() as s:
        s.add(Session(id="t1", kind="bot", bot_name="x", title="X"))
        s.commit()

    def lookup(topic: str) -> str:
        """Look something up."""
        return f"facts about {topic}"

    agent = Agent(TestModel(), tools=[lookup])
    evs = await collect(execute(agent, "t1", "Execute your task now.", show_prompt=False, run_id="r1"))
    kinds = [e["type"] for e in evs]
    assert "tool_call" in kinds and "tool_result" in kinds and kinds[-1] == "done"

    with db() as s:
        msgs = s.query(Message).filter(Message.session_id == "t1").all()
        sess = s.get(Session, "t1")
    assert [m.role for m in msgs] == ["assistant"]  # synthetic run prompt is hidden
    assert msgs[0].run_id == "r1" and json.loads(msgs[0].tool_calls_json)[0]["name"] == "lookup"
    assert "Execute your task now." in sess.history_json  # but the model saw it

    # follow-up continues the same history
    evs = await collect(execute(agent, "t1", "tell me more"))
    assert evs[-1]["type"] == "done"
    with db() as s:
        assert [m.role for m in s.query(Message).filter(Message.session_id == "t1").all()] == [
            "assistant", "user", "assistant"]


async def test_execute_reports_errors():
    with db() as s:
        s.add(Session(id="t2", kind="bot", bot_name="y", title="Y"))
        s.commit()

    def boom() -> str:
        """Always fails."""
        raise RuntimeError("kaput")

    agent = Agent(TestModel(), tools=[boom])
    evs = await collect(execute(agent, "t2", "go"))
    assert evs[-1]["type"] == "error" and "kaput" in evs[-1]["error"]
