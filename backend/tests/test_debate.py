"""
The debate round: one turn each, concurrently, and off means off.

The cost of this feature is the risk. A meeting used to be one model call; it
is now one per attendee plus the chair's loop, and the two ways that goes wrong
are an attendee quietly running a ReAct loop of its own, and the turns running
one after another so a four-person meeting takes four times as long as it needs
to. Both are asserted here.

The third claim is the one that keeps the feature honest: with
`debate_enabled=False`, `_run_meeting` builds exactly the context string it
built before this existed.
"""
import asyncio
import time

import pytest

import debate
import llm
from config import settings
from event_bus import event_bus
from schemas import AgentManifest, Event, EventType


class _Msg:
    def __init__(self, content):
        self.content = content


class _Choice:
    def __init__(self, content):
        self.message = _Msg(content)


class _Resp:
    def __init__(self, content):
        self.choices = [_Choice(content)]


def _agent(agent_id, name, role="Specialist"):
    from base_agent import BaseAgent
    return BaseAgent(AgentManifest(
        id=agent_id, name=name, role=role,
        system_prompt=f"You are {name}, {role}.",
        tools=["read_file"],
    ))


@pytest.fixture
def keyed(monkeypatch):
    """conftest clears every credential; the debate falls back to simulated
    turns without one. A dummy key puts it on the real path, and every call
    itself is stubbed."""
    monkeypatch.setattr(settings, "gemini_api_key", "test-key")
    monkeypatch.setattr(settings, "debate_enabled", True)
    monkeypatch.setattr(settings, "debate_rounds", 1)
    return True


# ------------------------------------------------------------------ the turns

@pytest.mark.asyncio
async def test_each_attendee_gets_exactly_one_call_and_no_tools(keyed, monkeypatch):
    calls = []

    async def _fake(messages, **kwargs):
        calls.append({"messages": messages, "kwargs": kwargs})
        return llm.LLMResult(response=_Resp("The number in table 2 is unsourced."),
                             model="gemini/gemini-2.5-flash-lite",
                             prompt_tokens=200, completion_tokens=30,
                             cost_usd=0.0002, attempts=1, fell_back=False)

    monkeypatch.setattr(llm, "acompletion", _fake)
    attendees = [_agent("coder", "Bob"), _agent("creative", "Eve"),
                 _agent("researcher", "Alice")]

    positions = await debate.hold(attendees, goal="Ship it",
                                  step_description="Agree the launch plan",
                                  room_context="draft.md: ...", step_id="s3")

    assert len(positions) == 3
    assert len(calls) == 3, "one call per attendee, no more"
    for call in calls:
        # No tools passed means no tool loop is even possible.
        assert "tools" not in call["kwargs"] or not call["kwargs"]["tools"]
        assert call["kwargs"]["max_tokens"] == settings.debate_max_tokens
        assert len(call["messages"]) == 2      # system + one user turn
    assert {p.agent_name for p in positions} == {"Bob", "Eve", "Alice"}


@pytest.mark.asyncio
async def test_the_turns_run_concurrently(keyed, monkeypatch):
    """
    Attendee turns are independent by construction, so four of them should cost
    one call's wall clock, not four.
    """
    delay = 0.25

    async def _slow(messages, **kwargs):
        await asyncio.sleep(delay)
        return llm.LLMResult(response=_Resp("Objection."), model="m",
                             prompt_tokens=10, completion_tokens=5,
                             cost_usd=0.0, attempts=1, fell_back=False)

    monkeypatch.setattr(llm, "acompletion", _slow)
    monkeypatch.setattr(settings, "meeting_capacity", 4)
    attendees = [_agent(f"a{i}", f"Agent{i}") for i in range(3)]

    started = time.perf_counter()
    positions = await debate.hold(attendees, goal="g", step_description="d")
    elapsed = time.perf_counter() - started

    assert len(positions) == 3
    assert elapsed < delay * 2, f"turns serialised: {elapsed:.2f}s for 3 x {delay}s"


@pytest.mark.asyncio
async def test_the_guest_list_is_capped_by_the_seats(keyed, monkeypatch):
    """`meeting_capacity` counts the chair, so a capacity of 4 debates 3."""
    calls = []

    async def _fake(messages, **kwargs):
        calls.append(1)
        return llm.LLMResult(response=_Resp("Objection."), model="m",
                             prompt_tokens=10, completion_tokens=5,
                             cost_usd=0.0, attempts=1, fell_back=False)

    monkeypatch.setattr(llm, "acompletion", _fake)
    monkeypatch.setattr(settings, "meeting_capacity", 4)

    positions = await debate.hold([_agent(f"a{i}", f"Agent{i}") for i in range(6)],
                                  goal="g", step_description="d")
    assert len(calls) == 3
    assert len(positions) == 3


@pytest.mark.asyncio
async def test_one_failed_turn_does_not_take_the_meeting_down(keyed, monkeypatch):
    async def _flaky(messages, **kwargs):
        if "Bob" in messages[0]["content"]:
            raise RuntimeError("provider said no")
        return llm.LLMResult(response=_Resp("Objection."), model="m",
                             prompt_tokens=10, completion_tokens=5,
                             cost_usd=0.0, attempts=1, fell_back=False)

    monkeypatch.setattr(llm, "acompletion", _flaky)
    positions = await debate.hold([_agent("coder", "Bob"), _agent("creative", "Eve")],
                                  goal="g", step_description="d")
    assert [p.agent_name for p in positions] == ["Eve"]


@pytest.mark.asyncio
async def test_a_turn_is_billed_to_the_attendee(keyed, monkeypatch):
    """The price of the argument has to land in the same gauge as the work."""
    async def _fake(messages, **kwargs):
        return llm.LLMResult(response=_Resp("Objection."), model="m",
                             prompt_tokens=200, completion_tokens=40,
                             cost_usd=0.0003, attempts=1, fell_back=False)

    monkeypatch.setattr(llm, "acompletion", _fake)
    bob = _agent("coder", "Bob")
    assert bob.token_usage.total_tokens == 0

    await debate.hold([bob], goal="g", step_description="d")

    assert bob.token_usage.total_tokens == 240
    assert bob.token_usage.estimated_cost_usd == pytest.approx(0.0003)


@pytest.mark.asyncio
async def test_every_position_is_announced_to_the_office(keyed, monkeypatch):
    async def _fake(messages, **kwargs):
        return llm.LLMResult(response=_Resp("Table 2 is unsourced."), model="m",
                             prompt_tokens=10, completion_tokens=5,
                             cost_usd=0.0, attempts=1, fell_back=False)

    monkeypatch.setattr(llm, "acompletion", _fake)
    seen = []

    async def _listen(event: Event):
        seen.append(event)

    event_bus.subscribe(EventType.AGENT_ARGUED, _listen)
    try:
        await debate.hold([_agent("coder", "Bob")], goal="g",
                          step_description="d", step_id="s2")
        await asyncio.sleep(0)
    finally:
        event_bus.unsubscribe(EventType.AGENT_ARGUED, _listen)

    assert len(seen) == 1
    payload = seen[0].data
    assert payload["agent_name"] == "Bob"
    assert payload["position"] == "Table 2 is unsourced."
    assert payload["step_id"] == "s2"
    assert payload["simulated"] is False


# ------------------------------------------------------------- the off switch

@pytest.mark.asyncio
async def test_disabled_debate_makes_no_calls_and_states_no_positions(monkeypatch):
    async def _boom(messages, **kwargs):
        raise AssertionError("a disabled debate must not call a model")

    monkeypatch.setattr(llm, "acompletion", _boom)
    monkeypatch.setattr(settings, "debate_enabled", False)

    assert await debate.hold([_agent("coder", "Bob")], goal="g",
                             step_description="d") == []
    assert debate.render([]) == ""


@pytest.mark.asyncio
async def test_a_disabled_debate_leaves_the_chair_context_unchanged(monkeypatch):
    """
    The context the chair is handed with debate off must be exactly the string
    `_run_meeting` built before the round existed: brief, then the room's
    artifacts, and nothing else.
    """
    brief, room_context = "You are chairing a meeting.", "draft.md: ..."
    monkeypatch.setattr(settings, "debate_enabled", False)

    positions = await debate.hold([_agent("coder", "Bob")], goal="g",
                                  step_description="d", room_context=room_context)
    floor = debate.render(positions)
    rebuilt = "\n\n".join(part for part in (brief, room_context, floor) if part)

    assert rebuilt == f"{brief}\n\n{room_context}"


# ------------------------------------------------------------- the no-key path

# ---------------------------------------------------------- the whole meeting

@pytest.mark.asyncio
async def test_a_meeting_traces_every_attendee_and_hands_the_chair_their_words(
        keyed, monkeypatch):
    """
    The acceptance case: run a meeting step and check the trace holds one span
    per attendee under the step, and that the objections actually reach the
    chair's context -- a decision cannot reference an objection the chair was
    never shown.
    """
    import tracing
    from orchestrator import Orchestrator
    from schemas import PlanStep, Task
    from workspace import MissionWorkspace

    async def _fake(messages, **kwargs):
        speaker = "Bob" if "Bob" in messages[0]["content"] else "Eve"
        return llm.LLMResult(response=_Resp(f"{speaker} objects: table 2 is unsourced."),
                             model="m", prompt_tokens=100, completion_tokens=20,
                             cost_usd=0.0001, attempts=1, fell_back=False)

    monkeypatch.setattr(llm, "acompletion", _fake)
    monkeypatch.setattr(settings, "meeting_capacity", 4)

    orch = Orchestrator()
    for agent_id, name in (("coder", "Bob"), ("creative", "Eve")):
        agent = orch.agents[agent_id]
        agent.name = name
        # The stub above tells the two turns apart by the persona it is given,
        # which is the attendee's own manifest prompt.
        agent.manifest.system_prompt = f"You are {name}."
    chair = orch.boss_agent
    seen_context = {}

    async def _chair_run(task, context="", hints="", goal="", extra_tools=None):
        seen_context["context"] = context
        return "Recorded: table 2 needs a source before we ship."

    monkeypatch.setattr(chair, "run", _chair_run)

    step = PlanStep(id="s4", agent_id=chair.id, description="Agree the launch plan",
                    kind="meeting", attendees=["coder", "creative"])
    workspace = MissionWorkspace("m_debate")
    await workspace.put("draft.md", "a draft", "coder", "Bob", "s1")

    trace = tracing.start_mission("m_debate", None)
    try:
        async with tracing.span("step", "s4"):
            out = await orch._run_meeting(
                step, chair, Task(id="sub", description=step.description),
                workspace, context="", hints="", goal="Ship it", task_id="t9")
    finally:
        tracing.end_mission()

    assert "Recorded" in out
    debates = [s for s in trace.spans if s.kind == "debate"]
    assert sorted(s.name for s in debates) == ["coder:r1", "creative:r1"]
    step_span = next(s for s in trace.spans if s.kind == "step")
    assert all(s.parent_id == step_span.span_id for s in debates)

    # The chair saw both objections, and was told not to drop them silently.
    assert "Bob objects" in seen_context["context"]
    assert "Eve objects" in seen_context["context"]
    assert "do not quietly drop" in seen_context["context"]


@pytest.mark.asyncio
async def test_without_a_credential_the_turns_are_simulated_and_say_so(monkeypatch):
    """
    conftest clears the keys, so this is the path the offline demo takes. The
    room still argues -- it is the most legible thing the office does -- but
    every position is tagged rather than passed off as a model's opinion.
    """
    async def _boom(messages, **kwargs):
        raise AssertionError("no credential is configured; nothing should be called")

    monkeypatch.setattr(llm, "acompletion", _boom)
    monkeypatch.setattr(settings, "debate_enabled", True)

    positions = await debate.hold([_agent("coder", "Bob")], goal="g",
                                  step_description="Agree the launch plan")

    assert len(positions) == 1
    assert positions[0].simulated is True
    assert "simulated" in positions[0].text
    assert "Bob" in debate.render(positions)
