"""Bot-to-bot delegation: runs of the target bot, chain/loop protection, task polling."""

import json

import pytest
from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from sqlmodel import select

from app import memory, spec
from app.agents import a2a, bot_runner
from app.db import db
from app.models import Message, Run


def make_bot(name: str, tools: list[str]) -> spec.BotSpec:
    return spec.save_bot(spec.BotSpec(name=name, title=name.title(), description=f"{name} bot.",
                                      instructions="# Goal\nAnswer requests from other bots.", model="test-model",
                                      tools=tools))


def scripted(calls: dict[str, list]):
    """Streaming FunctionModel per bot: call the next scripted tool, or answer with the last tool result."""
    def model_for(name: str):
        async def stream(messages, info):
            last = messages[-1].parts[-1]
            if last.part_kind == "tool-return":
                yield f"{name} got: {last.content}"
            elif script := calls.get(name):
                tool, args = script.pop(0)
                yield {0: DeltaToolCall(name=tool, json_args=json.dumps(args))}
            else:
                yield f"answer from {name}"
        return FunctionModel(stream_function=stream)
    return model_for


@pytest.fixture
def bots():
    names = ["a2a-alpha", "a2a-beta"]
    for n in names:
        make_bot(n, ["a2a"])
    yield names
    for n in names:
        spec.delete_bot(n)
        memory.delete(n)


async def test_send_message_runs_target_and_returns_answer(bots, monkeypatch):
    alpha, beta = bots
    calls = {alpha: [("a2a_send_message", {"agent": beta, "message": "summarise X"})]}
    model_for = scripted(calls)
    monkeypatch.setattr(bot_runner, "make_model", lambda _m: None)
    monkeypatch.setattr(bot_runner, "Agent", _agent_factory(model_for))

    run = bot_runner.start_run(spec.get_bot(alpha))
    await bot_runner.wait(run.id)

    with db() as s:
        b_run = s.exec(select(Run).where(Run.bot_name == beta)).one()
        b_msgs = s.exec(select(Message).where(Message.session_id == b_run.session_id)).all()
        a_out = s.exec(select(Message).where(Message.run_id == run.id)).one().content
    assert b_run.trigger == f"a2a:{alpha}" and b_run.status == "ok"
    assert b_msgs[0].role == "user" and "summarise X" in b_msgs[0].content  # request visible in beta's session
    assert "answer from a2a-beta" in a_out and "completed" in a_out
    assert memory.get(beta, memory.LAST_RUN) is None  # a2a side requests keep beta's own last result
    assert memory.get(alpha, memory.LAST_RUN)


async def test_loop_back_to_caller_is_refused(bots, monkeypatch):
    alpha, beta = bots
    calls = {
        alpha: [("a2a_send_message", {"agent": beta, "message": "ask alpha"})],
        beta: [("a2a_send_message", {"agent": alpha, "message": "loop"})],
    }
    monkeypatch.setattr(bot_runner, "make_model", lambda _m: None)
    monkeypatch.setattr(bot_runner, "Agent", _agent_factory(scripted(calls)))

    run = bot_runner.start_run(spec.get_bot(alpha))
    await bot_runner.wait(run.id)

    with db() as s:
        b_run = s.exec(select(Run).where(Run.bot_name == beta).order_by(Run.started_at.desc())).first()
        b_out = s.exec(select(Message).where(Message.run_id == b_run.id)).one().content
        loops = s.exec(select(Run).where(Run.bot_name == alpha, Run.trigger == f"a2a:{beta}")).all()
    assert "Refused" in b_out and "would loop" in b_out
    assert loops == []  # beta never started a run of alpha


async def test_self_call_unknown_bot_and_foreign_task(bots):
    alpha, beta = bots
    tools = a2a.toolset(alpha).tools
    assert "Refused" in await tools["a2a_send_message"].function(alpha, "hi")
    assert "No bot named" in await tools["a2a_start_task"].function("nope-bot", "hi")
    assert "No task" in await tools["a2a_get_task"].function("does-not-exist")
    assert [b["name"] for b in await tools["a2a_list_agents"].function()].count(alpha) == 0


async def test_get_last_result_reads_peer_memory(bots):
    alpha, beta = bots
    memory.record_run(beta, "r-1", "beta's digest", [])
    res = await a2a.toolset(alpha).tools["a2a_get_last_result"].function(beta)
    assert res["agent"] == beta and res["output"] == "beta's digest"


def test_a2a_in_catalog_and_toolsets_gated():
    names = [t["name"] for t in a2a.catalog()]
    assert "a2a_send_message" in names
    plain = make_bot("a2a-plain", [])
    try:
        assert not any(getattr(ts, "id", None) == "a2a" for ts in bot_runner._toolsets(plain))
        assert len(bot_runner._toolsets(plain.model_copy(update={"tools": ["a2a_get_last_result"]}))) == 2
    finally:
        spec.delete_bot("a2a-plain")


def test_verbosity_round_trip_and_prompt():
    b = make_bot("verbose-bot", []).model_copy(update={"verbosity": "verbose"})
    try:
        assert spec.BotSpec.from_skill_md(b.to_skill_md()).verbosity == "verbose"
        assert spec.BotSpec.from_skill_md(make_bot("verbose-bot", []).to_skill_md()).verbosity == "short"
    finally:
        spec.delete_bot("verbose-bot")


def _agent_factory(model_for):
    """Replace pydantic-ai Agent in bot_runner so each bot gets its scripted model."""
    from pydantic_ai import Agent

    def factory(_model, *, name, **kw):
        return Agent(model_for(name), name=name, **kw)

    return factory
