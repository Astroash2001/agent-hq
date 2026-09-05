"""
The token budget, and whose tokens it counts.

`BaseAgent.token_usage` is a lifetime counter -- it accumulates across every
mission the process has served and nothing resets it. The budget gate in
`_execute_plan` used to read the sum of those counters directly, which made the
budget tighten as a server stayed up: mission one had the whole allowance,
mission ten had whatever mission one through nine left it, and a long-lived
process would eventually refuse a mission that a freshly booted one would run
without complaint. The gate is the same code either way, so the only test that
can tell the two apart is one that puts prior spend on the roster before the
mission starts.

The second test is the one that stops the fix from going too far the other way:
scoping the counter must not turn the gate off.
"""
import asyncio
import os
import shutil
import tempfile

import pytest

import runstore
from config import settings
from orchestrator import Orchestrator
from schemas import Event, EventType, Plan, PlanStep, TokenUsage
from event_bus import event_bus
from workspace import MissionWorkspace


@pytest.fixture
def scratch(monkeypatch):
    """A private run store and mission directory for one test."""
    root = tempfile.mkdtemp(prefix="agenthq_budget_")
    monkeypatch.setattr(settings, "missions_path", os.path.join(root, "missions"))
    monkeypatch.setattr(settings, "persist_missions", True)
    monkeypatch.setattr(settings, "memory_enabled", False)
    monkeypatch.setattr(settings, "runstore_enabled", True)
    monkeypatch.setattr(settings, "parallel_enabled", False)
    store = runstore.reset_store(os.path.join(root, "runs.db"))
    yield root, store
    runstore.reset_store(settings.runstore_path)
    shutil.rmtree(root, ignore_errors=True)


class _Agent:
    """A specialist that burns a fixed number of tokens per subtask."""

    def __init__(self, agent_id, name, burn=0):
        self.id = agent_id
        self.name = name
        self.tools = ["write_file"]
        self.burn = burn
        self.calls = []
        self.last_run = {}
        self.workspace = None
        self.token_usage = TokenUsage()

    async def run(self, task, context="", hints="", goal="", extra_tools=None):
        self.calls.append(task.description)
        await asyncio.sleep(0.01)
        # What a real agent does turn by turn: add to the lifetime counter.
        self.token_usage.prompt_tokens += self.burn
        self.token_usage.total_tokens += self.burn
        await self.workspace.put(name=task.assigned_to + "_out.md",
                                 content="# Work\n" + ("body " * 60),
                                 author_id=self.id, author_name=self.name)
        self.last_run = {"duration_sec": 0.01, "tokens": self.burn, "tools_used": [],
                         "steps": 1, "outcome": "success", "estimated": True,
                         "cost_usd": 0.0, "model": "", "prompt_tokens": self.burn,
                         "completion_tokens": 0, "retries": 0, "fell_back": False}
        return "done"

    async def update_status(self, *a, **k):
        pass

    async def speak(self, *a, **k):
        pass


def _wire(orch, monkeypatch, agents):
    monkeypatch.setattr(orch, "boss_agent", None)
    monkeypatch.setattr(orch, "_agent_by_id",
                        lambda aid: agents.get(aid) or next(iter(agents.values())))
    monkeypatch.setattr(orch, "agents", agents)


def _linear_plan(n=3):
    return Plan(goal="g", source="llm", steps=[
        PlanStep(id=f"s{i}", agent_id="researcher", description=f"step {i}",
                 dependencies=[f"s{i-1}"] if i > 1 else [],
                 produces=f"s{i}.md")
        for i in range(1, n + 1)])


async def _run(orch, monkeypatch, task_id, burn, budget, prior=0, steps=3):
    """One mission, with `prior` tokens already on the roster before it starts."""
    monkeypatch.setattr(settings, "max_tokens_budget_per_task", budget)
    agent = _Agent("researcher", "Alice", burn=burn)
    agent.token_usage.total_tokens = prior
    agents = {"researcher": agent}
    _wire(orch, monkeypatch, agents)

    workspace = MissionWorkspace(task_id)
    agent.workspace = workspace
    orch._slots = asyncio.Semaphore(1)
    orch._peak_active_steps = 0
    orch._active_steps = 0
    # What `execute_task` records before planning starts.
    orch._token_baseline[task_id] = orch._tokens_spent()

    plan = _linear_plan(steps)
    results = await orch._execute_plan(plan, workspace, task_id, "g")
    return plan, results, agent


@pytest.mark.asyncio
async def test_a_long_lived_process_does_not_starve_a_fresh_mission(scratch, monkeypatch):
    """
    The regression. The roster is carrying ten times the budget from missions
    already served; this mission spends almost nothing and must still run.

    Reading the lifetime counters, every step here fails before it is dispatched
    with "token budget exhausted" -- on a mission that burned 30 tokens.
    """
    orch = Orchestrator()
    plan, results, agent = await _run(
        orch, monkeypatch, "m_prior", burn=10, budget=1000, prior=10_000)

    assert [s.status for s in plan.steps] == ["completed"] * 3
    assert len(results) == 3
    assert len(agent.calls) == 3, "every step was dispatched"
    assert orch._mission_tokens_spent("m_prior") == 30
    assert agent.token_usage.total_tokens == 10_030, "the lifetime counter still climbs"


@pytest.mark.asyncio
async def test_the_gate_still_fires_on_this_missions_own_spend(scratch, monkeypatch):
    """
    Scoping the counter must not disarm it. No prior spend at all, but this
    mission burns past its budget on its second step, and the third must never
    be dispatched.
    """
    seen = []

    async def _listen(event: Event):
        seen.append(event.data.get("error", ""))

    event_bus.subscribe(EventType.TASK_FAILED, _listen)
    try:
        orch = Orchestrator()
        plan, _, agent = await _run(
            orch, monkeypatch, "m_spend", burn=4000, budget=6000, prior=0)
        await asyncio.sleep(0.01)
    finally:
        event_bus.unsubscribe(EventType.TASK_FAILED, _listen)

    assert [s.status for s in plan.steps] == ["completed", "completed", "failed"]
    assert len(agent.calls) == 2, "the third step was never dispatched"
    assert any("budget exhausted" in e for e in seen), seen


@pytest.mark.asyncio
async def test_prior_spend_and_own_spend_add_up_the_way_the_gate_reads_them(
        scratch, monkeypatch):
    """
    Both halves at once: the roster is over budget on lifetime tokens, and this
    mission also burns past its own budget. The gate must fire on the second
    reason and not the first -- so the first two steps run before it does,
    rather than none of them.
    """
    orch = Orchestrator()
    plan, _, agent = await _run(
        orch, monkeypatch, "m_both", burn=4000, budget=6000, prior=50_000)

    assert [s.status for s in plan.steps] == ["completed", "completed", "failed"]
    assert orch._mission_tokens_spent("m_both") == 8000
    assert orch._tokens_spent() == 58_000


def test_a_mission_with_no_baseline_is_charged_the_lifetime_total():
    """
    The conservative fallback. A caller that drives `_execute_plan` without
    registering the mission has no baseline to subtract, and guessing zero would
    hand it an unbounded budget. Charging the lifetime total is the reading that
    fails closed.
    """
    orch = Orchestrator()
    for agent in orch.agents.values():
        agent.token_usage.total_tokens = 700
    lifetime = orch._tokens_spent()

    assert lifetime >= 700
    assert orch._mission_tokens_spent("never_registered") == lifetime


def test_the_mission_figure_never_reads_negative():
    """
    Nothing resets an agent's counter mid-mission today. If something ever did,
    a negative subtraction would read as spare budget and fail open, which is
    the one direction a budget must not fail in.
    """
    orch = Orchestrator()
    orch._token_baseline["m_reset"] = 5_000
    for agent in orch.agents.values():
        agent.token_usage.total_tokens = 0

    assert orch._mission_tokens_spent("m_reset") == 0
