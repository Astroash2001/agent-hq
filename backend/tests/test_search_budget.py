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

    async def _exec(tool_name, arguments, agent_id=None, task_id=None, granted=None):
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

    async def _exec(tool_name, arguments, agent_id=None, task_id=None, granted=None):
        return {"success": True, "results": []}

    monkeypatch.setattr(ba.tool_registry, "execute", _exec)

    # One shared counter, so each search differs: an identical repeat is
    # refused by the duplicate-call guard before the search cap is reached.
    greedy = _greedy_model([])

    async def _capture(self, messages, tool_schemas, task):
        seen.append([m for m in messages if m.get("role") == "tool"])
        return await greedy(self, messages, tool_schemas, task)

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
                "tool_calls": [{"name": "read_file",
                                "arguments": {"filepath": f"x{len(turns)}"}}],
                "prompt_tokens": 1, "completion_tokens": 1}

    monkeypatch.setattr(ba.BaseAgent, "_call_llm", _fake)
    executed = []

    async def _exec(tool_name, arguments, agent_id=None, task_id=None, granted=None):
        executed.append(tool_name)
        return {"success": True}

    monkeypatch.setattr(ba.tool_registry, "execute", _exec)
    await researcher.run(Task(id="t3", description="read things"))
    assert executed.count("read_file") == 4


@pytest.mark.asyncio
async def test_a_page_that_will_not_load_is_told_to_try_another_source(researcher, monkeypatch):
    """
    Observed: a research step met three HTTP 403s, wrote "could not be priced"
    for each, and finished on two facts. A failed fetch buys back the search
    that replaces it.
    """
    monkeypatch.setattr(settings, "search_max_calls", 3)
    turns = []

    async def _fake(self, messages, tool_schemas, task):
        turns.append(1)
        if len(turns) > 3:
            return {"content": "done", "thought": "", "tool_calls": [],
                    "prompt_tokens": 1, "completion_tokens": 1}
        # One search, then a fetch that fails, on each turn.
        name = "web_search" if len(turns) % 2 else "fetch_url"
        args = ({"query": f"q{len(turns)}"} if name == "web_search"
                else {"url": f"https://example.com/{len(turns)}"})
        return {"content": "", "thought": "", "tool_calls": [{"name": name, "arguments": args}],
                "prompt_tokens": 1, "completion_tokens": 1}

    monkeypatch.setattr(ba.BaseAgent, "_call_llm", _fake)
    seen = []

    async def _exec(tool_name, arguments, agent_id=None, task_id=None, granted=None):
        if tool_name == "fetch_url":
            return {"success": False, "error": "Server returned HTTP 403."}
        return {"success": True, "results": []}

    monkeypatch.setattr(ba.tool_registry, "execute", _exec)

    async def _capture(self, messages, tool_schemas, task):
        seen.extend(m for m in messages if m.get("role") == "tool")
        return await _fake(self, messages, tool_schemas, task)

    monkeypatch.setattr(ba.BaseAgent, "_call_llm", _capture)
    researcher.tools = ["web_search", "fetch_url"]
    await researcher.run(Task(id="t6", description="price it"))

    advice = [m for m in seen if "different site" in str(m.get("content"))]
    assert advice, "a failed page must say to try another source"
    assert "HTTP 403" in str(advice[0]["content"]), "the real error is kept"


@pytest.mark.asyncio
async def test_an_identical_observing_call_is_not_run_twice(researcher, monkeypatch):
    """
    Observed: the same 30,000-row query issued ten times in one step, each
    re-sending the whole history, waiting for a row cap that never lifts.
    """
    turns = []

    async def _fake(self, messages, tool_schemas, task):
        turns.append(1)
        if len(turns) > 4:
            return {"content": "done", "thought": "", "tool_calls": [],
                    "prompt_tokens": 1, "completion_tokens": 1}
        return {"content": "", "thought": "again",
                "tool_calls": [{"name": "read_file", "arguments": {"filepath": "same.csv"}}],
                "prompt_tokens": 1, "completion_tokens": 1}

    monkeypatch.setattr(ba.BaseAgent, "_call_llm", _fake)
    executed = []

    async def _exec(tool_name, arguments, agent_id=None, task_id=None, granted=None):
        executed.append(tool_name)
        return {"success": True}

    monkeypatch.setattr(ba.tool_registry, "execute", _exec)
    await researcher.run(Task(id="t4", description="read it"))
    assert executed.count("read_file") == 1, "a repeat must be answered, not re-run"


def test_a_step_leaves_the_steps_behind_it_something_to_spend():
    """
    Observed: a 3-step mission where two research steps spent 413,788 of the
    400,000 between them, so the step that was to write the itinerary was
    failed by the budget gate without ever calling its model. The mission
    produced research and no deliverable.
    """
    import step_guard

    # Nothing waiting: the step may use the whole budget, as before.
    assert step_guard.ceiling_for_step(400_000, 0, 60_000, 0.5) == 400_000

    # One step waiting: its share is held back.
    assert step_guard.ceiling_for_step(400_000, 1, 60_000, 0.5) == 340_000
    assert step_guard.ceiling_for_step(400_000, 2, 60_000, 0.5) == 280_000

    # A wide plan cannot reserve the budget away from the step that is running:
    # 8 steps waiting is 480,000 of reserve, capped at half.
    assert step_guard.ceiling_for_step(400_000, 8, 60_000, 0.5) == 200_000

    # Degenerate inputs are not a division by zero or a negative ceiling.
    assert step_guard.ceiling_for_step(0, 3, 60_000, 0.5) == 0
    assert step_guard.ceiling_for_step(400_000, 3, 0, 0.5) == 400_000
    assert step_guard.ceiling_for_step(100, 3, 60_000, 1.0) >= 1


@pytest.mark.asyncio
async def test_a_running_step_stops_when_the_mission_budget_is_spent(researcher, monkeypatch):
    """
    The budget used to be checked only between steps: a mission reported 756K of
    400K and a running agent carried on to 1.1M. Out of budget, an agent gets
    one closing turn with write_file only, then stops.
    """
    import step_guard

    turns = []
    offered = []

    async def _fake(self, messages, tool_schemas, task):
        turns.append(1)
        offered.append({s["function"]["name"] for s in tool_schemas})
        return {"content": "partial findings", "thought": "",
                "tool_calls": [{"name": "web_search", "arguments": {"query": f"q{len(turns)}"}}],
                "prompt_tokens": 1, "completion_tokens": 1}

    monkeypatch.setattr(ba.BaseAgent, "_call_llm", _fake)
    executed = []

    async def _exec(tool_name, arguments, agent_id=None, task_id=None, granted=None):
        executed.append(tool_name)
        return {"success": True, "results": []}

    monkeypatch.setattr(ba.tool_registry, "execute", _exec)
    spent = {"tokens": 0}
    token = step_guard.bind_budget(lambda: (spent["tokens"], 100))
    try:
        # Two turns inside the budget, then the mission runs out.
        original = _fake

        async def _spending(self, messages, tool_schemas, task):
            result = await original(self, messages, tool_schemas, task)
            if len(turns) == 2:
                spent["tokens"] = 150
            return result

        monkeypatch.setattr(ba.BaseAgent, "_call_llm", _spending)
        answer = await researcher.run(Task(id="t5", description="research"),
                                      extra_tools=["write_file"], produces="notes.md")
    finally:
        step_guard.reset_budget(token)

    assert len(turns) == 3, "two normal turns, one closing turn, then stop"
    assert offered[-1] == {"write_file"}, "the closing turn offers only write_file"
    assert executed.count("web_search") == 2, "nothing but write_file runs after the budget"
    assert answer == "partial findings"
