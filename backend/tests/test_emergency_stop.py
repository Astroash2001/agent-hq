"""
The kill switch, and what it actually kills.

`cancel_all` used to flag the roster and nothing else. That aborts the request
in flight -- `BaseAgent.cancel` cancels `self._inflight` -- but `run()` clears
`_is_cancelled` at the top of every run, so the very next step the scheduler
dispatched came up with a clean flag and ran normally. HALT stopped one turn and
the mission carried on spending.

The test that can tell the difference is the one with a step still queued behind
the running one: a mission halted during step 1 of 3 must never dispatch step 2.
Asserting on the halted step alone passes either way.
"""
import asyncio
import os
import shutil
import tempfile

import pytest

import runstore
from config import settings
from event_bus import event_bus
from orchestrator import Orchestrator
from schemas import Event, EventType, Plan, PlanStep, TokenUsage
from workspace import MissionWorkspace


@pytest.fixture
def scratch(monkeypatch):
    root = tempfile.mkdtemp(prefix="agenthq_halt_")
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
    """
    A specialist that can be held mid-step, the way a real one is held inside a
    provider call. `started` fires when it is in the step; `release` lets it out.

    `returns_on_cancel` picks which of the two real races this stub models.
    False: the scheduler cancels the future before the agent unwinds. True: the
    agent gets there first and *returns* normally -- which is what a real
    `BaseAgent.run` does, because it catches the CancelledError from the aborted
    request and answers "Task halted by emergency stop." as an ordinary string.
    """

    def __init__(self, agent_id="researcher", name="Alice", hold=False,
                 returns_on_cancel=False):
        self.id = agent_id
        self.name = name
        self.tools = ["write_file"]
        self.hold = hold
        self.returns_on_cancel = returns_on_cancel
        self.calls = []
        self.last_run = {}
        self.workspace = None
        self.token_usage = TokenUsage()
        self.cancelled = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    def cancel(self):
        self.cancelled += 1
        # What BaseAgent.cancel does to the in-flight request.
        if self.returns_on_cancel:
            self.release.set()

    async def run(self, task, context="", hints="", goal="", extra_tools=None):
        self.calls.append(task.description)
        self.started.set()
        if self.hold:
            await self.release.wait()
        await asyncio.sleep(0.01)
        await self.workspace.put(name=task.assigned_to + "_out.md",
                                 content="# Work\n" + ("body " * 60),
                                 author_id=self.id, author_name=self.name)
        self.last_run = {"duration_sec": 0.01, "tokens": 10, "tools_used": [],
                         "steps": 1, "outcome": "success", "estimated": True,
                         "cost_usd": 0.0, "model": "", "prompt_tokens": 6,
                         "completion_tokens": 4, "retries": 0, "fell_back": False}
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


@pytest.mark.asyncio
async def test_halt_stops_the_mission_not_just_the_turn(scratch, monkeypatch):
    """
    Three steps in a chain. HALT while step 1 is in flight. Steps 2 and 3 must
    never be dispatched -- which is exactly what the old code did dispatch,
    because `run()` cleared the flag on entry.
    """
    orch = Orchestrator()
    agent = _Agent(hold=True)
    _wire(orch, monkeypatch, {"researcher": agent})

    workspace = MissionWorkspace("m_halt")
    agent.workspace = workspace
    orch._slots = asyncio.Semaphore(1)
    orch._peak_active_steps = 0
    orch._active_steps = 0
    orch.active_tasks["m_halt"] = object()

    plan = _linear_plan(3)
    runner = asyncio.ensure_future(
        orch._execute_plan(plan, workspace, "m_halt", "g"))

    await asyncio.wait_for(agent.started.wait(), timeout=2)
    orch.cancel_all()
    await asyncio.wait_for(runner, timeout=5)

    assert agent.cancelled == 1, "the roster was flagged"
    assert len(agent.calls) == 1, (
        f"dispatched {len(agent.calls)} steps after the halt; HALT must stop "
        f"the graph, not just the turn")
    assert [s.status for s in plan.steps[1:]] == ["failed", "failed"]


@pytest.mark.asyncio
async def test_the_halt_is_announced_once(scratch, monkeypatch):
    """
    Four independent steps, all in flight, all cancelled together. `asyncio.wait`
    returns on the first completion, so without a latch the loop announces the
    halt again for every straggler it comes back round to.
    """
    seen = []

    async def _listen(event: Event):
        seen.append(event.data.get("error", ""))

    event_bus.subscribe(EventType.TASK_FAILED, _listen)
    try:
        orch = Orchestrator()
        monkeypatch.setattr(settings, "parallel_enabled", True)
        monkeypatch.setattr(settings, "max_parallel_agents", 4)
        agents = {f"a{i}": _Agent(agent_id=f"a{i}", name=f"A{i}", hold=True)
                  for i in range(1, 5)}
        _wire(orch, monkeypatch, agents)

        workspace = MissionWorkspace("m_once")
        for a in agents.values():
            a.workspace = workspace
        orch._slots = asyncio.Semaphore(4)
        orch._peak_active_steps = 0
        orch._active_steps = 0
        orch.active_tasks["m_once"] = object()

        plan = Plan(goal="g", source="llm", steps=[
            PlanStep(id=f"s{i}", agent_id=f"a{i}", description=f"step {i}",
                     produces=f"s{i}.md")
            for i in range(1, 5)])
        runner = asyncio.ensure_future(
            orch._execute_plan(plan, workspace, "m_once", "g"))

        for a in agents.values():
            await asyncio.wait_for(a.started.wait(), timeout=2)
        orch.cancel_all()
        await asyncio.wait_for(runner, timeout=5)
        await asyncio.sleep(0.02)
    finally:
        event_bus.unsubscribe(EventType.TASK_FAILED, _listen)

    halts = [e for e in seen if "Halted by emergency stop" in e]
    assert len(halts) == 1, f"announced {len(halts)} times: {halts}"


@pytest.mark.asyncio
async def test_a_halted_step_is_recorded_failed_so_a_resume_reruns_it(
        scratch, monkeypatch):
    """
    A cancelled task's `.exception()` raises rather than returning, so the
    completion loop has to ask `.cancelled()` first. Getting that wrong turns a
    halt into an unhandled CancelledError out of the scheduler.

    Recorded `failed`, not `completed`: the step produced no artifact, and only
    `completed` steps are skipped on resume.
    """
    root, store = scratch
    orch = Orchestrator()
    agent = _Agent(hold=True)
    _wire(orch, monkeypatch, {"researcher": agent})

    workspace = MissionWorkspace("m_rec")
    agent.workspace = workspace
    orch._slots = asyncio.Semaphore(1)
    orch._peak_active_steps = 0
    orch._active_steps = 0
    orch.active_tasks["m_rec"] = object()
    store.start_mission("m_rec", "g")

    plan = _linear_plan(2)
    runner = asyncio.ensure_future(
        orch._execute_plan(plan, workspace, "m_rec", "g"))
    await asyncio.wait_for(agent.started.wait(), timeout=2)
    orch.cancel_all()
    results = await asyncio.wait_for(runner, timeout=5)

    assert results == [], "a halted mission returns no step results"
    stored = {s["step_id"]: s["status"] for s in store.get_steps("m_rec")}
    assert stored.get("s1") == "failed", stored
    assert stored.get("s2") == "failed", stored


@pytest.mark.asyncio
async def test_an_agent_that_returns_after_the_halt_is_still_halted(
        scratch, monkeypatch):
    """
    The likelier race, and the one that looks like success.

    A real `BaseAgent.run` catches the CancelledError from the aborted request
    and *returns* "Task halted by emergency stop." -- an ordinary string, an
    ordinary completed future. Taken at face value the scheduler files that
    sentence as the step's artifact and lets the rest of the DAG read it.
    """
    root, store = scratch
    orch = Orchestrator()
    agent = _Agent(hold=True, returns_on_cancel=True)
    _wire(orch, monkeypatch, {"researcher": agent})

    workspace = MissionWorkspace("m_ret")
    agent.workspace = workspace
    orch._slots = asyncio.Semaphore(1)
    orch._peak_active_steps = 0
    orch._active_steps = 0
    orch.active_tasks["m_ret"] = object()
    store.start_mission("m_ret", "g")

    plan = _linear_plan(3)
    runner = asyncio.ensure_future(
        orch._execute_plan(plan, workspace, "m_ret", "g"))
    await asyncio.wait_for(agent.started.wait(), timeout=2)
    orch.cancel_all()
    results = await asyncio.wait_for(runner, timeout=5)

    assert results == [], "the halted step's return value must not be filed as work"
    assert [s.status for s in plan.steps] == ["failed"] * 3
    assert len(agent.calls) == 1, "no step was dispatched after the halt"


@pytest.mark.asyncio
async def test_a_fresh_mission_is_not_born_halted(scratch, monkeypatch):
    """
    The halt is per mission id and has to be cleared, or the office is bricked
    until the process restarts -- pressing HALT once would poison every mission
    that reused an id, and `handle_user_input` clears it for exactly that reason.
    """
    orch = Orchestrator()
    # A halted mission, then resumed under the same id -- the case that made
    # the wakeup worth clearing as well as the flag. A leftover *set* event is
    # picked straight back up by `_execute_plan`, which halts the resumed run
    # before it dispatches a thing.
    orch._halted.add("m_next")
    orch._halt_events["m_next"] = asyncio.Event()
    orch._halt_events["m_next"].set()

    orch._halted.discard("m_next")          # what handle_user_input does
    orch._halt_events.pop("m_next", None)

    agent = _Agent()
    _wire(orch, monkeypatch, {"researcher": agent})
    workspace = MissionWorkspace("m_next")
    agent.workspace = workspace
    orch._slots = asyncio.Semaphore(1)
    orch._peak_active_steps = 0
    orch._active_steps = 0

    plan = _linear_plan(2)
    await orch._execute_plan(plan, workspace, "m_next", "g")

    assert [s.status for s in plan.steps] == ["completed", "completed"]
