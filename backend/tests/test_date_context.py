"""
Date injection.

A model's sense of "now" is its training cutoff. Asked what happened in Indian
news today, one returned a brief headed "Today" whose sources were dated up to
three weeks earlier -- it had no way to tell, because nothing in the
conversation said what today was.

These pin the fact reaching the prompt. What the model then does with it -- how
recent a request needs, whether to filter at all -- is deliberately not tested
here, because it is deliberately not the harness's decision.
"""
import datetime

import pytest

import base_agent as ba
import planner as pl
from config import date_context
from schemas import AgentManifest, Task


def test_date_context_states_today():
    today = datetime.datetime.now()
    note = date_context()
    assert str(today.year) in note
    assert today.strftime("%B") in note        # month name
    assert today.strftime("%A") in note        # weekday
    assert note.startswith("Today's date is")


def test_it_is_a_fact_not_an_instruction():
    """
    The harness supplies the date and stops there.

    Steering the model here -- "search for recent sources", "filter to today" --
    would put the recency decision in Python, which is exactly where it does
    not belong: it depends on what the user actually asked for.
    """
    note = date_context().lower()
    for directive in ("search", "filter", "recent", "must", "should", "use this"):
        assert directive not in note, f"date_context() is steering the model: {directive!r}"


@pytest.mark.asyncio
async def test_agent_system_prompt_leads_with_the_date(monkeypatch):
    seen = {}

    async def _capture(self, messages, tool_schemas, task):
        seen["system"] = messages[0]["content"]
        return {"content": "done", "thought": "", "tool_calls": [],
                "prompt_tokens": 1, "completion_tokens": 1}

    monkeypatch.setattr(ba.BaseAgent, "_call_llm", _capture)
    monkeypatch.setattr(ba.settings, "spatial_enabled", False)

    agent = ba.BaseAgent(AgentManifest(
        id="researcher", name="Alice", role="Research",
        avatar_emoji="R", system_prompt="You research things.", tools=[],
    ))
    await agent.run(Task(id="t", description="anything"))

    assert seen["system"].startswith("Today's date is")
    # and the agent's own prompt survives underneath it
    assert "You research things." in seen["system"]


@pytest.mark.asyncio
async def test_planner_system_prompt_carries_the_date(monkeypatch):
    seen = {}

    async def _fake(messages, **kw):
        seen["system"] = messages[0]["content"]
        raise RuntimeError("stop here -- the prompt is what we are checking")

    monkeypatch.setattr(pl.llm, "acompletion", _fake)
    monkeypatch.setattr(pl.settings, "llm_strict", False)
    # conftest clears every credential to keep the suite offline, and the
    # planner skips the model entirely when none is set. A dummy key puts it
    # back on the path we are inspecting; the call itself is stubbed above.
    monkeypatch.setattr(pl.settings, "gemini_api_key", "test-key")

    await pl.Planner().plan("do a thing", [{"id": "researcher", "name": "Alice",
                                            "role": "Research", "tools": []}])

    assert seen["system"].startswith("Today's date is")
    # the %-formatted template still rendered correctly beneath it
    assert "planning module of AgentHQ" in seen["system"]
    assert "%d" not in seen["system"], "template placeholders were not substituted"
