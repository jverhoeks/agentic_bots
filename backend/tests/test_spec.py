import pytest
from pydantic import ValidationError

from app import spec
from pathlib import Path


def make(**kw):
    base = dict(name="daily-ai-news", title="Daily AI News", description="AI digest.",
                instructions="# Goal\nSummarise the AI news of the day.", model="test-model",
                tools=["hackernews_hn_top_stories", "web_fetch_url"])
    return spec.BotSpec(**{**base, **kw})


def test_skill_md_round_trip():
    b = make()
    text = b.to_skill_md()
    assert text.startswith("---\nname: daily-ai-news\n")
    assert "allowed-tools: hackernews_hn_top_stories web_fetch_url" in text
    assert spec.BotSpec.from_skill_md(text) == b


def test_sample_bot_parses():
    b = spec.BotSpec.from_skill_md((Path(__file__).parent / "fixtures" / "daily-ai-news.SKILL.md").read_text())
    assert b.name == "daily-ai-news" and "hackernews_hn_top_stories" in b.tools


@pytest.mark.parametrize("name", ["Daily", "a", "bad_name", "-x-", "x--y"])
def test_invalid_names(name):
    with pytest.raises(ValidationError):
        make(name=name)


def test_save_list_delete():
    spec.save_bot(make(name="tmp-bot"))
    assert spec.get_bot("tmp-bot").created_at
    assert "tmp-bot" in [b.name for b in spec.list_bots()]
    assert spec.delete_bot("tmp-bot")
    assert spec.get_bot("tmp-bot") is None


def test_path_traversal_rejected():
    with pytest.raises(ValueError):
        spec.get_bot("../etc")
