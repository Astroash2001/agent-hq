"""
Whose tokens a mission counts.

Three accountings existed before this and none of them saw a whole mission.
`BaseAgent.token_usage` counts an agent's turns and is a lifetime figure.
`Orchestrator._step_telemetry` counts what each step's agent reported. Neither
sees `planner.py`, `critic.py` or `synthesis.py`, which call `llm.acompletion`
directly and belong to no agent and no step -- so one planner call per mission,
one reviewer call per step and the synthesis pass were real spend that the cost
panel, `mission.json` and the budget gate all reported as zero. `debate.py` was
worse than missing: it added to an agent's lifetime counter but never to that
agent's `last_run`, so it appeared in one figure and not the other.

The ledger is the third place, and the tests below pin the properties that make
it worth having: it records whoever called, it stays scoped to one mission, and
the gate reads it as well as the roster so neither blind spot can hide spend.
"""
import asyncio
import types

import pytest

import llm
import token_ledger
from event_bus import event_bus
from orchestrator import Orchestrator
from schemas import DAGNode, Event, EventType, TokenUsage


@pytest.fixture(autouse=True)
def clean_ledger():
    token_ledger.reset()
    yield
    token_ledger.reset()


def _response(prompt, completion):
    """A provider response carrying nothing but a usage block."""
    return types.SimpleNamespace(
        usage=types.SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion),
        choices=[types.SimpleNamespace(
            message=types.SimpleNamespace(content="{}", tool_calls=None))],
    )


@pytest.fixture
def fake_provider(monkeypatch):
    """Answer every call locally, with a usage block we choose."""
    import litellm

    calls = []

    async def _acompletion(**kwargs):
        calls.append(kwargs)
        return _response(100, 25)

    monkeypatch.setattr(litellm, "acompletion", _acompletion)
    return calls


# --- the recording point ----------------------------------------------------

@pytest.mark.asyncio
async def test_a_call_inside_a_mission_is_charged_to_it(fake_provider):
    token_ledger.begin("m1")
    with token_ledger.mission("m1"):
        await llm.acompletion([{"role": "user", "content": "hi"}])

    assert token_ledger.total_tokens("m1") == 125


@pytest.mark.asyncio
async def test_a_call_outside_any_mission_is_charged_to_none(fake_provider):
    token_ledger.begin("m1")
    await llm.acompletion([{"role": "user", "content": "hi"}])

    # Not an error, and not charged to whichever mission happened to run last:
    # a probe script or a unit test calling the planner is nobody's bill.
    assert token_ledger.total_tokens("m1") == 0


@pytest.mark.asyncio
async def test_the_planner_the_critic_and_the_reviewer_are_counted(fake_provider):
    """The whole point: calls that belong to no agent and no step."""
    token_ledger.begin("m2")
    with token_ledger.mission("m2"):
        for label in ("planner", "critic", "synthesis", "debate"):
            with token_ledger.source(label):
                await llm.acompletion([{"role": "user", "content": label}])

    totals = token_ledger.totals("m2")
    assert totals["total_tokens"] == 4 * 125
    assert totals["by_source"] == {
        "planner": 125, "critic": 125, "synthesis": 125, "debate": 125,
    }
    # None of it belongs to a step, so all of it is overhead.
    assert token_ledger.overhead_tokens("m2") == 500


@pytest.mark.asyncio
async def test_overhead_excludes_the_agents_own_work(fake_provider):
    token_ledger.begin("m3")
    with token_ledger.mission("m3"):
        with token_ledger.source("agent:researcher"):
            await llm.acompletion([{"role": "user", "content": "work"}])
        with token_ledger.source("critic"):
            await llm.acompletion([{"role": "user", "content": "review"}])

    assert token_ledger.total_tokens("m3") == 250
    assert token_ledger.overhead_tokens("m3") == 125


# --- scoping ----------------------------------------------------------------

@pytest.mark.asyncio
async def test_two_missions_in_one_process_do_not_mix(fake_provider):
    token_ledger.begin("a")
    token_ledger.begin("b")

    async def run(task_id, times):
        with token_ledger.mission(task_id):
            for _ in range(times):
                await llm.acompletion([{"role": "user", "content": task_id}])

    # Concurrently, because the context is what keeps them apart: each task
    # copies the context it was created in.
    await asyncio.gather(run("a", 1), run("b", 3))

    assert token_ledger.total_tokens("a") == 125
    assert token_ledger.total_tokens("b") == 375


@pytest.mark.asyncio
async def test_reopening_a_mission_starts_from_zero(fake_provider):
    """A resumed mission re-baselines: its budget governs the work still to do."""
    token_ledger.begin("m4")
    with token_ledger.mission("m4"):
        await llm.acompletion([{"role": "user", "content": "first run"}])
    assert token_ledger.total_tokens("m4") == 125

    token_ledger.begin("m4")
    assert token_ledger.total_tokens("m4") == 0


def test_a_mission_nobody_opened_reads_as_zero():
    assert token_ledger.total_tokens("never_seen") == 0
    assert token_ledger.overhead_tokens("never_seen") == 0
    assert token_ledger.totals("never_seen")["total_tokens"] == 0


def test_a_simulated_turn_is_recorded_and_flagged():
    """
    The simulator never reaches `llm.acompletion`, so `base_agent` records it
    here instead -- flagged, because the counts are invented.
    """
    token_ledger.begin("m5")
    token_ledger.record(
        prompt_tokens=90, completion_tokens=10, cost_usd=0.0,
        model="simulator", estimated=True,
        task_id="m5", source_name="agent:coder")

    totals = token_ledger.totals("m5")
    assert totals["total_tokens"] == 100
    assert totals["estimated"] is True


def test_accounting_never_raises_at_the_caller():
    """A ledger that can break a mission is worse than one that is wrong."""
    token_ledger.begin("m6")
    token_ledger.record(prompt_tokens=None, completion_tokens="nonsense",
                        task_id="m6")          # must not raise
    assert token_ledger.total_tokens("m6") >= 0


# --- what the gate and the HUD read -----------------------------------------

class _Agent:
    def __init__(self, agent_id, spent=0):
        self.id = agent_id
        self.name = agent_id.title()
        self.token_usage = TokenUsage(prompt_tokens=spent, total_tokens=spent)


def test_the_gate_reads_the_larger_of_the_two_accountings():
    """
    Neither reading contains the other: the roster's delta sees agent turns
    and no overhead, the ledger sees every real call but not a simulated turn
    it was not told about. Under-reading spend is the one direction a budget
    gate must not fail in.
    """
    orch = Orchestrator()
    orch.agents = {"researcher": _Agent("researcher", spent=400)}
    orch.boss_agent = None
    orch._token_baseline["m7"] = 0

    token_ledger.begin("m7")
    token_ledger.record(prompt_tokens=100, completion_tokens=0,
                        task_id="m7", source_name="planner")
    # Roster 400 vs ledger 100.
    assert orch._mission_tokens_spent("m7") == 400

    token_ledger.record(prompt_tokens=900, completion_tokens=0,
                        task_id="m7", source_name="critic")
    # Ledger 1000 vs roster 400: the overhead the roster cannot see.
    assert orch._mission_tokens_spent("m7") == 1000


@pytest.mark.asyncio
async def test_dag_update_carries_the_whole_bill_and_the_overhead():
    orch = Orchestrator()
    orch.agents = {}
    orch.boss_agent = None
    orch.current_dag = [DAGNode(id="s1", label="research",
                                agent_id="researcher", status="completed")]
    orch._step_telemetry["m8"] = {
        "s1": {"step_id": "s1", "agent_id": "researcher",
               "prompt_tokens": 800, "completion_tokens": 200,
               "cost_usd": 0.01, "duration_sec": 4.0, "model": "openai/gpt-5-mini"},
    }
    orch._token_baseline["m8"] = 0
    token_ledger.begin("m8")
    token_ledger.record(prompt_tokens=1000, completion_tokens=0,
                        task_id="m8", source_name="agent:researcher")
    token_ledger.record(prompt_tokens=300, completion_tokens=0,
                        task_id="m8", source_name="critic")

    seen = []

    async def _listen(event: Event):
        seen.append(event)

    event_bus.subscribe(EventType.DAG_UPDATE, _listen)
    try:
        await orch._emit_dag("m8", "llm")
        await asyncio.sleep(0.01)
    finally:
        event_bus.unsubscribe(EventType.DAG_UPDATE, _listen)

    payload = seen[0].data
    # The per-step breakdown accounts for the step and nothing else...
    assert payload["mission_tokens"] == 1000
    # ...while the figure the HUD shows includes the reviewer.
    assert payload["mission_tokens_total"] == 1300
    assert payload["overhead_tokens"] == 300
