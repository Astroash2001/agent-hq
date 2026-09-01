"""
Search budget.

An agent given a working search tool will keep using it: every result suggests
another query worth trying. Observed on one subtask -- eighteen searches, each
carried in the message history and re-sent on every later turn, and a single
3-step mission costing 144,156 tokens against a 150,000 cap.

The cap is enforced in the loop rather than asked for in the prompt, because a
prompt is advisory and this is a budget. These tests drive the loop with a
stubbed model that searches forever, so nothing here costs anything.
"""
import json

import pytest

import base_agent as ba
from config import settings
from schemas import AgentManifest, Task


@pytest.fixture
def researcher(monkeypatch):
    monkeypatch.setattr(settings, "search_max_calls", 3)
    monkeypatch.setattr(settings, "max_agent_steps", 6)
    monkeypatch.setattr(settings, "spatial_enabled", False)   # no walking in a unit test
    return ba.BaseAgent(AgentManifest(
        id="researcher", name="Alice", role="Research",
        avatar_emoji="R", system_prompt="Research things.",
        tools=["web_search"],
    ))


def _greedy_model(calls):
    """A model that only ever wants to search again."""
    async def _fake(self, messages, tool_schemas, task):
        calls.append(1)
        return {
            "content": "",
            "thought": "searching",
            "tool_calls": [{"name": "web_search", "arguments": {"query": f"q{len(calls)}"}}],
            "prompt_tokens": 10, "completion_tokens": 5,
        }
    return _fake


@pytest.mark.asyncio
async def test_searches_are_capped(researcher, monkeypatch):
    turns = []
    monkeypatch.setattr(ba.BaseAgent, "_call_llm", _greedy_model(turns))

    executed = []

    async def _exec(tool_name, arguments, agent_id=None, task_id=None):
        executed.append(tool_name)
        return {"success": True, "results": []}

    monkeypatch.setattr(ba.tool_registry, "execute", _exec)

    await researcher.run(Task(id="t", description="research everything"))

    # The model asked on every one of its turns; only the first 3 ran.
    assert len(executed) == 3, f"cap not enforced, {len(executed)} searches ran"
    assert len(turns) > 3, "the loop should keep going, just without searching"


@pytest.mark.asyncio
async def test_the_agent_is_told_why_it_was_refused(researcher, monkeypatch):
    monkeypatch.setattr(ba.BaseAgent, "_call_llm", _greedy_model([]))

    seen = []

    async def _exec(tool_name, arguments, agent_id=None, task_id=None):
        return {"success": True, "results": []}

    monkeypatch.setattr(ba.tool_registry, "execute", _exec)

    original = ba.BaseAgent._call_llm

    async def _capture(self, messages, tool_schemas, task):
        seen.append([m for m in messages if m.get("role") == "tool"])
        return await _greedy_model([])(self, messages, tool_schemas, task)

    monkeypatch.setattr(ba.BaseAgent, "_call_llm", _capture)
    await researcher.run(Task(id="t2", description="research everything"))

    refusals = [
        json.loads(m["content"]).get("error", "")
        for turn in seen for m in turn
        if m.get("content", "").startswith("{")
        and "Search limit reached" in m.get("content", "")
    ]
    assert refusals, "the agent was never told the search budget ran out"
    assert "Write your deliverable now" in refusals[0]


@pytest.mark.asyncio
async def test_other_tools_are_not_capped(researcher, monkeypatch):
    """The budget is on search, not on working."""
    turns = []

    async def _fake(self, messages, tool_schemas, task):
        turns.append(1)
        if len(turns) > 4:
            return {"content": "done", "thought": "", "tool_calls": [],
                    "prompt_tokens": 1, "completion_tokens": 1}
        return {"content": "", "thought": "reading",
                "tool_calls": [{"name": "read_file", "arguments": {"filepath": "x"}}],
                "prompt_tokens": 1, "completion_tokens": 1}

    monkeypatch.setattr(ba.BaseAgent, "_call_llm", _fake)
    executed = []

    async def _exec(tool_name, arguments, agent_id=None, task_id=None):
        executed.append(tool_name)
        return {"success": True}

    monkeypatch.setattr(ba.tool_registry, "execute", _exec)
    await researcher.run(Task(id="t3", description="read things"))
    assert executed.count("read_file") == 4
