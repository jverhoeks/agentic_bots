import json

from app import memory


def test_tools_and_prompt_section():
    tools = {t.__name__: t for t in [f.function for f in memory.toolset("membot").tools.values()]}
    assert tools["memory_save"]("reported_ids", "[1, 2, 3]") == "Saved 'reported_ids'."
    assert tools["memory_save"]("_last_run", "x").startswith("Invalid key")
    assert tools["memory_get"]("reported_ids") == "[1, 2, 3]"

    memory.record_run("membot", "run1", "Digest body", [{"name": "hn_top", "args": {}, "status": "ok"}])
    section = memory.prompt_section("membot")
    assert "reported_ids" in section and "Digest body" in section and "hn_top" in section
    assert json.loads(memory.get("membot", memory.LAST_RUN))["run_id"] == "run1"

    assert tools["memory_delete"]("_last_run") == "Platform keys cannot be deleted."
    assert tools["memory_delete"]("reported_ids") == "Deleted 'reported_ids'."
    assert memory.delete("membot") == 1  # only _last_run left
    assert memory.prompt_section("membot").count("No saved notes yet.") == 1
