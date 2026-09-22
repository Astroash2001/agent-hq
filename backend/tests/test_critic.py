"""
The critic, and the bounded repair loop it feeds.

The cap is the feature. An unbounded repair loop is how an agent system spends a
whole mission's budget on one step, so these check that a rejected step is
re-dispatched exactly as many times as the budget allows and then completes
anyway -- rather than hanging, looping, or failing the mission.
"""
import asyncio
import json
import os
import shutil
import tempfile

import pytest

import contracts
import critic
from config import settings
from event_bus import event_bus
from memory import AgentMemory
from schemas import Episode, Event, EventType, PlanStep
from workspace import MissionWorkspace


@pytest.fixture
def keyed(monkeypatch):
    """conftest clears every credential to keep the suite offline. The critic
    skips entirely without one, so a dummy key puts it on the path under test;
    the call itself is always stubbed."""
    monkeypatch.setattr(settings, "gemini_api_key", "test-key")
    monkeypatch.setattr(settings, "critic_enabled", True)
    return True


def _reply(payload):
    """A stubbed llm.acompletion returning one JSON judgement."""
    class _Msg:
        content = json.dumps(payload) if isinstance(payload, dict) else payload

    class _Choice:
        message = _Msg()

    class _Resp:
        choices = [_Choice()]

    async def _fake(messages, **kwargs):
        import llm
        return llm.LLMResult(response=_Resp(), model="gemini/gemini-2.5-flash-lite",
                             prompt_tokens=100, completion_tokens=20,
                             cost_usd=0.0001, attempts=1, fell_back=False)
    return _fake


# ------------------------------------------------------------------- gating

@pytest.mark.asyncio
async def test_disabled_critic_is_a_clean_no_op(monkeypatch):
    monkeypatch.setattr(settings, "critic_enabled", False)
    got = await critic.critique("goal", "step", "a fine artifact")
    assert got.skipped is True
    assert got.verdict == "accept", "a skip must never read as a rejection"


@pytest.mark.asyncio
async def test_no_credential_means_no_judgement(monkeypatch):
    """There is no offline judge. The simulator grading itself would look like
    a measurement while being nothing of the kind."""
    monkeypatch.setattr(settings, "critic_enabled", True)
    got = await critic.critique("goal", "step", "a fine artifact")
    assert got.skipped is True
    assert "no model" in got.reason_skipped


@pytest.mark.asyncio
async def test_an_empty_artifact_is_not_worth_a_call(keyed, monkeypatch):
    called = []

    async def _fake(messages, **kwargs):
        called.append(1)

    monkeypatch.setattr(critic.llm, "acompletion", _fake)
    got = await critic.critique("goal", "step", "   ")
    assert got.skipped is True and called == []


def test_the_judge_uses_the_cheap_model(monkeypatch):
    monkeypatch.setattr(settings, "critic_model", "")
    monkeypatch.setattr(settings, "fallback_model", "gemini/gemini-2.5-flash-lite")
    assert critic.model_name() == "gemini/gemini-2.5-flash-lite"
    monkeypatch.setattr(settings, "critic_model", "groq/llama-3.1-8b-instant")
    assert critic.model_name() == "groq/llama-3.1-8b-instant"


# ------------------------------------------------------------------ parsing

@pytest.mark.asyncio
async def test_a_revise_verdict_carries_actionable_guidance(keyed, monkeypatch):
    monkeypatch.setattr(critic.llm, "acompletion", _reply({
        "verdict": "revise", "score": 0.4,
        "reasons": ["No figures are given for either chemistry"],
        "guidance": "Add the Wh/kg and cycle-life figures for both chemistries.",
    }))
    got = await critic.critique("compare batteries", "research them", "some text")
    assert got.skipped is False
    assert got.verdict == "revise"
    assert got.score == 0.4
    assert "Wh/kg" in got.guidance


@pytest.mark.asyncio
async def test_the_trace_records_why_the_critic_decided(keyed, monkeypatch):
    """
    The span held only the verdict. A correct extraction rejected twice at 0.72
    could not be diagnosed, because the reasons were never written anywhere.
    """
    import tracing

    recorded = {}
    real_span = tracing.span

    class _Recorder:
        def set(self, **attrs):
            recorded.update(attrs)

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _span(kind, name, **attrs):
        if kind == "critic":
            yield _Recorder()
        else:
            async with real_span(kind, name, **attrs) as s:
                yield s

    monkeypatch.setattr(critic.tracing, "span", _span)
    monkeypatch.setattr(critic.llm, "acompletion", _reply({
        "verdict": "revise", "score": 0.72,
        "reasons": ["Query 3 did not use position = 'Forward' as instructed"],
        "guidance": "Use the instructed filter."}))

    await critic.critique("goal", "step", "an artifact")
    assert recorded["verdict"] == "revise"
    assert recorded["score"] == 0.72
    assert "Forward" in recorded["reasons"]


def test_the_judge_is_told_a_corrected_instruction_is_correct_work():
    prompt = critic.CRITIC_SYSTEM_PROMPT
    assert "planner before anyone looked at the data" in prompt
    assert "accept it" in prompt


def test_the_planner_is_told_not_to_guess_queries_or_values():
    from planner import PLANNER_SYSTEM_PROMPT
    assert "do not write SQL, column names, category values" in PLANNER_SYSTEM_PROMPT


@pytest.mark.asyncio
async def test_an_unreadable_reply_lets_the_work_through(keyed, monkeypatch):
    """A misparse must not spend a repair attempt."""
    monkeypatch.setattr(critic.llm, "acompletion", _reply("I think it's fine, honestly"))
    got = await critic.critique("g", "s", "artifact")
    assert got.skipped is True
    assert got.verdict == "accept"


@pytest.mark.asyncio
async def test_a_failed_call_never_takes_the_mission_down(keyed, monkeypatch):
    async def _boom(messages, **kwargs):
        raise RuntimeError("provider on fire")

    monkeypatch.setattr(critic.llm, "acompletion", _boom)
    got = await critic.critique("g", "s", "artifact")
    assert got.skipped is True
    assert "RuntimeError" in got.reason_skipped


@pytest.mark.asyncio
async def test_a_nonsense_verdict_defaults_to_accept(keyed, monkeypatch):
    monkeypatch.setattr(critic.llm, "acompletion", _reply({
        "verdict": "maybe?", "score": 5.0, "reasons": "just one string"}))
    got = await critic.critique("g", "s", "artifact")
    assert got.verdict == "accept"
    assert got.score == 1.0, "an out-of-range score is clamped"
    assert got.reasons == ["just one string"]


@pytest.mark.asyncio
async def test_a_revise_with_no_guidance_is_built_from_the_reasons(keyed, monkeypatch):
    monkeypatch.setattr(critic.llm, "acompletion", _reply({
        "verdict": "revise", "score": 0.2, "reasons": ["It cites nothing"]}))
    got = await critic.critique("g", "s", "artifact")
    assert got.verdict == "revise"
    assert "cites nothing" in got.guidance


# --------------------------------------------------------- the repair loop

@pytest.fixture
def missions(monkeypatch):
    root = tempfile.mkdtemp(prefix="agenthq_critic_")
    monkeypatch.setattr(settings, "missions_path", os.path.join(root, "missions"))
    monkeypatch.setattr(settings, "persist_missions", True)
    yield root
    shutil.rmtree(root, ignore_errors=True)


class _StubAgent:
    """A specialist that writes whatever the script says, one entry per call."""

    def __init__(self, outputs, name="Alice", agent_id="researcher"):
        self.outputs = list(outputs)
        self.id = agent_id
        self.name = name
        self.tools = ["write_file"]
        self.calls = []
        self.last_run = {}
        self.workspace = None
        self.step_id = ""

    async def run(self, task, context="", hints="", goal="", extra_tools=None):
        self.calls.append(task.description)
        body = self.outputs[min(len(self.calls) - 1, len(self.outputs) - 1)]
        await self.workspace.put(name="research.md", content=body,
                                 author_id=self.id, author_name=self.name,
                                 step_id=self.step_id)
        # A real model answered, so the repair loop is allowed to re-dispatch.
        self.last_run = {"duration_sec": 0.1, "tokens": 100, "tools_used": [],
                         "steps": 1, "outcome": "success", "estimated": False,
                         "cost_usd": 0.001, "model": "gemini/gemini-2.5-flash",
                         "prompt_tokens": 80, "completion_tokens": 20,
                         "retries": 0, "fell_back": False}
        return f"wrote research.md ({len(body)} chars)"

    async def update_status(self, *a, **k):
        pass

    async def speak(self, *a, **k):
        pass


async def _run_one_step(orch, agent, monkeypatch, workspace, critiques=None):
    """Drive a single step through the real _run_step, with a scripted agent."""
    step = PlanStep(id="s1", agent_id="researcher", description="Research it",
                    produces="research.md")
    agent.workspace = workspace
    agent.step_id = step.id
    monkeypatch.setattr(orch, "_agent_by_id", lambda _id: agent)
    monkeypatch.setattr(orch, "boss_agent", None)
    monkeypatch.setattr(settings, "memory_enabled", False)

    if critiques is not None:
        seq = list(critiques)

        async def _critique(goal, desc, text, **_kw):
            return seq.pop(0) if seq else critic.Critique(skipped=True)
        monkeypatch.setattr(critic, "critique", _critique)
    else:
        async def _skip(goal, desc, text, **_kw):
            return critic.Critique(skipped=True, reason_skipped="test")
        monkeypatch.setattr(critic, "critique", _skip)

    events = []

    async def tap(event: Event):
        events.append(event.data)

    event_bus.subscribe(EventType.STEP_CRITIQUED, tap)
    try:
        result = await orch._run_step(step, workspace, "t_repair", "the goal")
        await asyncio.sleep(0.05)
    finally:
        event_bus.unsubscribe(EventType.STEP_CRITIQUED, tap)
    return result, events


@pytest.mark.asyncio
async def test_a_contract_failure_triggers_exactly_one_repair(missions, monkeypatch):
    from orchestrator import Orchestrator

    orch = Orchestrator()
    monkeypatch.setattr(settings, "max_repair_attempts", 1)
    monkeypatch.setattr(settings, "contracts_enabled", True)

    # First reply is the stub; the second is real work.
    agent = _StubAgent(["Task completed after 4 execution steps.",
                        "# Findings\n\n" + ("real detail. " * 40)])
    workspace = MissionWorkspace("m_repair")
    result, events = await _run_one_step(orch, agent, monkeypatch, workspace)

    assert len(agent.calls) == 2, "the stub should have been sent back once"
    assert result["attempts"] == 2
    assert "rejected" in agent.calls[1] or "Fix all of the following" in agent.calls[1]
    assert workspace.get("research.md").content.startswith("# Findings")
    assert events and events[0]["verdict"] == "revise"
    assert events[0]["source"] == "contract"
    assert events[0]["repairing"] is True


@pytest.mark.asyncio
async def test_repair_stops_at_the_cap_and_the_step_still_completes(missions, monkeypatch):
    """The step must finish rather than hang or fail the mission -- with the
    rejection visible, so a capped repair does not read as an accept."""
    from orchestrator import Orchestrator

    orch = Orchestrator()
    monkeypatch.setattr(settings, "max_repair_attempts", 1)

    agent = _StubAgent(["Task completed after 4 execution steps."])  # never improves
    workspace = MissionWorkspace("m_cap")
    result, events = await _run_one_step(orch, agent, monkeypatch, workspace)

    assert len(agent.calls) == 2, "1 original + 1 repair, and no more"
    assert result["attempts"] == 2
    assert result["verdict"] == "revise"
    assert events[-1]["repairing"] is False, "the last rejection had no budget left"


@pytest.mark.asyncio
async def test_zero_repairs_configured_means_one_dispatch(missions, monkeypatch):
    from orchestrator import Orchestrator

    orch = Orchestrator()
    monkeypatch.setattr(settings, "max_repair_attempts", 0)

    agent = _StubAgent(["Task completed after 4 execution steps."])
    workspace = MissionWorkspace("m_zero")
    result, events = await _run_one_step(orch, agent, monkeypatch, workspace)

    assert len(agent.calls) == 1
    assert events[0]["repairing"] is False


@pytest.mark.asyncio
async def test_a_critic_revise_triggers_exactly_one_redispatch(missions, monkeypatch):
    from orchestrator import Orchestrator

    orch = Orchestrator()
    monkeypatch.setattr(settings, "max_repair_attempts", 1)

    good = "# Findings\n\n" + ("real detail. " * 40)
    agent = _StubAgent([good, good + "\n\nNow with figures."])
    workspace = MissionWorkspace("m_critic")

    critiques = [
        critic.Critique(verdict="revise", score=0.3, reasons=["No figures"],
                        guidance="Add the Wh/kg figures."),
        critic.Critique(verdict="accept", score=0.9),
    ]
    result, events = await _run_one_step(orch, agent, monkeypatch, workspace,
                                         critiques=critiques)

    assert len(agent.calls) == 2
    assert "Wh/kg" in agent.calls[1], "the guidance must reach the agent"
    assert result["quality"] == 0.9
    assert result["verdict"] == "accept"
    assert [e["source"] for e in events] == ["critic", "critic"]


@pytest.mark.asyncio
async def test_a_clean_artifact_is_dispatched_once(missions, monkeypatch):
    from orchestrator import Orchestrator

    orch = Orchestrator()
    agent = _StubAgent(["# Findings\n\n" + ("real detail. " * 40)])
    workspace = MissionWorkspace("m_clean")
    critiques = [critic.Critique(verdict="accept", score=0.95)]
    result, events = await _run_one_step(orch, agent, monkeypatch, workspace,
                                         critiques=critiques)

    assert len(agent.calls) == 1
    assert result["attempts"] == 1
    assert result["quality"] == 0.95
    assert len(events) == 1 and events[0]["verdict"] == "accept"


@pytest.mark.asyncio
async def test_a_repair_that_rewrites_in_place_is_not_discarded(missions, monkeypatch):
    """_best_artifact compares names. A repair rewrites the same filename, so
    without a content comparison the fixed version looks like nothing new."""
    from orchestrator import Orchestrator

    orch = Orchestrator()
    monkeypatch.setattr(settings, "max_repair_attempts", 1)
    fixed = "# Findings\n\n" + ("the corrected work. " * 40)
    agent = _StubAgent(["Task completed after 2 execution steps.", fixed])
    workspace = MissionWorkspace("m_inplace")
    await _run_one_step(orch, agent, monkeypatch, workspace)

    assert workspace.get("research.md").content == fixed


@pytest.mark.asyncio
async def test_repair_cost_accumulates_across_attempts(missions, monkeypatch):
    """A repair is real spend on the same step. A telemetry row that overwrote
    would report the cost of quality as the cost of the last try only."""
    from orchestrator import Orchestrator

    orch = Orchestrator()
    monkeypatch.setattr(settings, "max_repair_attempts", 1)
    agent = _StubAgent(["Task completed after 2 execution steps."])
    workspace = MissionWorkspace("m_cost")
    await _run_one_step(orch, agent, monkeypatch, workspace)

    row = orch._step_telemetry["t_repair"]["s1"]
    assert row["attempts"] == 2
    assert row["cost_usd"] == pytest.approx(0.002)      # 2 x 0.001
    assert row["prompt_tokens"] == 160                  # 2 x 80


@pytest.mark.asyncio
async def test_a_simulated_step_is_judged_but_not_re_dispatched(missions, monkeypatch):
    """Re-running the built-in simulator returns byte-identical output, so a
    repair spends the budget to receive the same artifact back."""
    from orchestrator import Orchestrator

    orch = Orchestrator()
    monkeypatch.setattr(settings, "max_repair_attempts", 1)

    agent = _StubAgent(["Task completed after 4 execution steps."])
    original_run = agent.run

    async def _simulated(task, **kwargs):
        out = await original_run(task, **kwargs)
        agent.last_run["estimated"] = True
        return out
    agent.run = _simulated

    workspace = MissionWorkspace("m_sim")
    result, events = await _run_one_step(orch, agent, monkeypatch, workspace)

    assert len(agent.calls) == 1, "no point re-running a deterministic simulator"
    assert events and events[0]["verdict"] == "revise", "still judged, and said so"
    assert events[0]["repairing"] is False


# ------------------------------------------------------------------ memory

def test_an_old_memory_store_without_quality_still_loads(monkeypatch):
    """data/memory.json predates this field. Refusing to load it would throw
    away every episode the office has ever recorded."""
    root = tempfile.mkdtemp(prefix="agenthq_mem_")
    path = os.path.join(root, "memory.json")
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"episodes": [{
                "id": "ep_old", "agent_id": "researcher", "agent_name": "Alice",
                "goal": "g", "subtask": "s", "outcome": "success",
                "summary": "did the thing", "duration_sec": 1.0,
                "total_tokens": 100, "tools_used": [], "artifact": "a.md",
                "tokens": ["thing"], "timestamp": "2026-01-01T00:00:00",
            }]}, fh)

        store = AgentMemory(path)
        assert len(store.episodes) == 1
        assert store.episodes[0].quality is None, "unscored is not perfect"
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_a_recorded_quality_round_trips(monkeypatch):
    root = tempfile.mkdtemp(prefix="agenthq_mem2_")
    path = os.path.join(root, "memory.json")
    try:
        store = AgentMemory(path)
        store.record(agent_id="researcher", agent_name="Alice", goal="g",
                     subtask="s", outcome="success", quality=0.85)
        assert AgentMemory(path).episodes[0].quality == 0.85

        store.record(agent_id="researcher", agent_name="Alice", goal="g2",
                     subtask="s2", outcome="success")
        assert AgentMemory(path).episodes[1].quality is None
    finally:
        shutil.rmtree(root, ignore_errors=True)


@pytest.mark.asyncio
async def test_both_attempts_appear_in_the_trace(missions, monkeypatch):
    """The cost of quality has to be visible: a repair is a second dispatch,
    and the trace is where that shows up."""
    import tracing
    from orchestrator import Orchestrator

    orch = Orchestrator()
    monkeypatch.setattr(settings, "max_repair_attempts", 1)
    monkeypatch.setattr(settings, "tracing_enabled", True)

    agent = _StubAgent(["Task completed after 4 execution steps.",
                        "# Findings\n\n" + ("real detail. " * 40)])
    workspace = MissionWorkspace("m_trace_repair")
    trace = tracing.start_mission("m_trace_repair", workspace.dir)
    try:
        await _run_one_step(orch, agent, monkeypatch, workspace)
    finally:
        tracing.end_mission()

    attempts = [s for s in trace.spans if s.kind == "attempt"]
    assert len(attempts) == 2
    assert [s.attrs["attempt"] for s in attempts] == [1, 2]
    assert [s.attrs["repair"] for s in attempts] == [False, True]
    # And both hang off the one step span, not off each other.
    step_span = next(s for s in trace.spans if s.kind == "step")
    assert all(s.parent_id == step_span.span_id for s in attempts)
