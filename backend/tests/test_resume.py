"""
Resuming a killed mission, and the scheduler that makes it possible.

The scheduler used to take a slice of the ready set -- `ready[:limit]` -- which
made `max_parallel_agents` a batch size rather than a concurrency limit: a wave
of five ran three, waited for all three, then ran two. The high-water-mark test
below is the one that can tell the difference, because a counter read at rest
reads zero however broken the scheduler is.
"""
import asyncio
import os
import shutil
import tempfile

import pytest

import runstore
import tracing
from config import settings
from schemas import TokenUsage
from orchestrator import Orchestrator, StepDeadlineExceeded
from schemas import Plan, PlanStep
from workspace import MissionWorkspace


@pytest.fixture
def scratch(monkeypatch):
    """A private run store and mission directory for one test."""
    root = tempfile.mkdtemp(prefix="agenthq_resume_")
    monkeypatch.setattr(settings, "missions_path", os.path.join(root, "missions"))
    monkeypatch.setattr(settings, "persist_missions", True)
    monkeypatch.setattr(settings, "memory_enabled", False)
    monkeypatch.setattr(settings, "runstore_enabled", True)
    store = runstore.reset_store(os.path.join(root, "runs.db"))
    yield root, store
    runstore.reset_store(settings.runstore_path)
    shutil.rmtree(root, ignore_errors=True)


class _Agent:
    """A specialist that records what it was asked and writes its artifact."""

    def __init__(self, agent_id="researcher", name="Alice", delay=0.02,
                 fail_with=None, hang=False):
        self.id = agent_id
        self.name = name
        self.tools = ["write_file"]
        self.delay = delay
        self.fail_with = fail_with
        self.hang = hang
        self.calls = []
        self.last_run = {}
        self.workspace = None
        # The budget gate sums this across the roster between dispatches.
        self.token_usage = TokenUsage()

    async def run(self, task, context="", hints="", goal="", extra_tools=None):
        self.calls.append(task.description)
        if self.hang:
            await asyncio.sleep(3600)
        await asyncio.sleep(self.delay)
        if self.fail_with is not None:
            exc = self.fail_with
            self.fail_with = None          # fail once, then behave
            self.last_run = {"outcome": "failure", "estimated": True}
            raise exc
        name = task.assigned_to + "_out.md"
        # Tagged with the step, as write_file tags a real agent's file: filing
        # only credits a step with artifacts it wrote.
        # Ends on a full stop, as a finished artifact does: a body that stops
        # mid-word is what `contracts._looks_cut_off` exists to reject.
        await self.workspace.put(name=name, content="# Work\n" + ("body " * 60) + "end.",
                                 author_id=self.id, author_name=self.name,
                                 step_id=task.id.rsplit("_", 1)[-1])
        self.last_run = {"duration_sec": self.delay, "tokens": 10, "tools_used": [],
                         "steps": 1, "outcome": "success", "estimated": True,
                         "cost_usd": 0.0, "model": "", "prompt_tokens": 6,
                         "completion_tokens": 4, "retries": 0, "fell_back": False}
        return "done"

    async def update_status(self, *a, **k):
        pass

    async def speak(self, *a, **k):
        pass


def _wire(orch, monkeypatch, agents):
    """Point the orchestrator at stub specialists and away from the planner."""
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


def _fanout_plan(n=5):
    return Plan(goal="g", source="llm", steps=[
        PlanStep(id=f"s{i}", agent_id=f"a{i}", description=f"step {i}",
                 produces=f"s{i}.md")
        for i in range(1, n + 1)])


# --------------------------------------------------------- the concurrency cap

@pytest.mark.asyncio
async def test_the_cap_is_a_concurrency_limit_not_a_batch_size(scratch, monkeypatch):
    """
    Five independent steps, a cap of three. The peak must reach three -- and
    never exceed it. `ready[:limit]` also never exceeded it, which is why the
    peak alone is not enough: the fourth step must start the moment a slot
    frees, not after the whole first batch has drained.
    """
    root, store = scratch
    orch = Orchestrator()
    monkeypatch.setattr(settings, "max_parallel_agents", 3)
    monkeypatch.setattr(settings, "parallel_enabled", True)

    # Different agents, so the per-agent lock is not what serialises them.
    # Uneven delays, so a batching scheduler visibly idles slots.
    agents = {f"a{i}": _Agent(agent_id=f"a{i}", name=f"A{i}", delay=0.02 * i)
              for i in range(1, 6)}
    _wire(orch, monkeypatch, agents)

    workspace = MissionWorkspace("m_cap")
    for agent in agents.values():
        agent.workspace = workspace

    orch._slots = asyncio.Semaphore(3)
    orch._peak_active_steps = 0
    orch._active_steps = 0

    results = await orch._execute_plan(_fanout_plan(5), workspace, "m_cap", "g")

    assert len(results) == 5
    assert orch._peak_active_steps == 3, (
        f"peak was {orch._peak_active_steps}; the cap must be reached and "
        f"never exceeded")
    assert orch._active_steps == 0, "every slot was handed back"


@pytest.mark.asyncio
async def test_serial_mode_runs_one_at_a_time(scratch, monkeypatch):
    root, store = scratch
    orch = Orchestrator()
    monkeypatch.setattr(settings, "parallel_enabled", False)

    agents = {f"a{i}": _Agent(agent_id=f"a{i}", name=f"A{i}") for i in range(1, 4)}
    _wire(orch, monkeypatch, agents)
    workspace = MissionWorkspace("m_serial")
    for agent in agents.values():
        agent.workspace = workspace

    orch._slots = asyncio.Semaphore(1)
    orch._peak_active_steps = 0
    orch._active_steps = 0
    await orch._execute_plan(_fanout_plan(3), workspace, "m_serial", "g")
    assert orch._peak_active_steps == 1


# ------------------------------------------------------------- the deadline

@pytest.mark.asyncio
async def test_a_step_that_runs_over_is_stopped_and_recorded(scratch, monkeypatch):
    root, store = scratch
    orch = Orchestrator()
    # Comfortably past _dispatch_attempt's 0.3s visual handoff pacing, which is
    # inside the timed region -- correctly, it is real elapsed time -- so a
    # tighter deadline would fire before the agent was ever called and the test
    # would prove nothing about the agent hanging.
    monkeypatch.setattr(settings, "step_timeout_sec", 0.6)
    monkeypatch.setattr(settings, "memory_enabled", True)
    monkeypatch.setattr(settings, "step_max_attempts", 2)

    agent = _Agent(hang=True)
    _wire(orch, monkeypatch, {"researcher": agent})
    workspace = MissionWorkspace("m_slow")
    agent.workspace = workspace

    from memory import memory
    memory_path = memory.path
    memory.path = os.path.join(root, "memory.json")
    memory.episodes = []
    try:
        orch._slots = asyncio.Semaphore(1)
        store.start_mission("m_slow", "g")
        results = await orch._execute_plan(_linear_plan(1), workspace, "m_slow", "g")

        assert results == []
        assert len(agent.calls) == 1, "a deadline must not be retried"
        rows = {r["step_id"]: r for r in store.get_steps("m_slow")}
        assert rows["s1"]["status"] == "failed"
        assert "StepDeadlineExceeded" in rows["s1"]["error"]
        # Failure recording is what makes competence rerouting real.
        assert memory.episodes and memory.episodes[-1].outcome == "failure"
    finally:
        memory.path = memory_path
        memory.load()


@pytest.mark.asyncio
async def test_a_deadline_is_not_read_as_transient():
    """llm.is_transient treats the substring "Timeout" in a class name as
    retryable, which is why the deadline exception is not called one."""
    import llm
    assert llm.is_transient(StepDeadlineExceeded("too slow")) is False
    assert llm.is_transient(asyncio.TimeoutError()) is True


# -------------------------------------------------------------- step retry

@pytest.mark.asyncio
async def test_a_transient_step_failure_is_retried(scratch, monkeypatch):
    root, store = scratch
    orch = Orchestrator()
    monkeypatch.setattr(settings, "step_max_attempts", 2)

    agent = _Agent(fail_with=ConnectionError("connection reset"))
    _wire(orch, monkeypatch, {"researcher": agent})
    workspace = MissionWorkspace("m_retry")
    agent.workspace = workspace

    orch._slots = asyncio.Semaphore(1)
    store.start_mission("m_retry", "g")
    results = await orch._execute_plan(_linear_plan(1), workspace, "m_retry", "g")

    assert len(agent.calls) == 2, "a transient failure gets one more go"
    assert len(results) == 1, "and the step then succeeds"
    assert store.get_steps("m_retry")[0]["status"] == "completed"


@pytest.mark.asyncio
async def test_a_garbage_output_failure_is_not_retried(scratch, monkeypatch):
    """A step that failed because the agent produced nonsense is contracts.py
    and critic.py's problem. Retrying it just buys the same nonsense twice."""
    root, store = scratch
    orch = Orchestrator()
    monkeypatch.setattr(settings, "step_max_attempts", 3)

    agent = _Agent(fail_with=ValueError("the model returned unusable output"))
    _wire(orch, monkeypatch, {"researcher": agent})
    workspace = MissionWorkspace("m_garbage")
    agent.workspace = workspace

    orch._slots = asyncio.Semaphore(1)
    store.start_mission("m_garbage", "g")
    results = await orch._execute_plan(_linear_plan(1), workspace, "m_garbage", "g")

    assert len(agent.calls) == 1
    assert results == []
    assert store.get_steps("m_garbage")[0]["status"] == "failed"


# ------------------------------------------------------------------ resume

@pytest.mark.asyncio
async def test_a_resumed_mission_runs_only_what_is_left(scratch, monkeypatch):
    root, store = scratch
    orch = Orchestrator()
    plan = _linear_plan(3)

    store.start_mission("m_res", "g", "")
    store.record_plan("m_res", plan)
    store.upsert_step("m_res", plan.steps[0], "running")
    store.finish_step("m_res", "s1", "completed")
    store.upsert_step("m_res", plan.steps[1], "running")     # killed mid-flight

    agent = _Agent()
    _wire(orch, monkeypatch, {"researcher": agent})
    workspace = MissionWorkspace("m_res")
    agent.workspace = workspace

    orch._slots = asyncio.Semaphore(2)
    already = set(store.completed_step_ids("m_res"))
    assert already == {"s1"}

    results = await orch._execute_plan(plan, workspace, "m_res", "g", already)

    ran = [c for c in agent.calls]
    assert len(ran) == 2, "s1 was done; s2 (interrupted) and s3 were not"
    assert all("step 1" not in c for c in ran), "the completed step must not re-run"
    assert {r["step_id"] for r in results} == {"s2", "s3"}
    assert plan.steps[0].status == "completed"


@pytest.mark.asyncio
async def test_completed_artifacts_are_readable_by_the_resumed_steps(scratch, monkeypatch):
    """
    The workspace is in-memory and dies with the process. Without rehydration a
    resumed step consumes an artifact the store has never heard of and gets an
    empty context, redoing work that is sitting on disk beside it.
    """
    root, store = scratch
    orch = Orchestrator()
    workspace = MissionWorkspace("m_hydrate")
    body = "# Upstream research\n" + ("findings " * 80)
    await workspace.put("research.md", body, "researcher", "Alice", "s1")

    # A new process: a fresh workspace over the same directory.
    reopened = MissionWorkspace("m_hydrate")
    assert reopened.names() == [], "a new process starts with an empty blackboard"

    restored = orch._rehydrate_workspace(reopened)
    assert "research.md" in restored
    assert reopened.get("research.md").content == body
    assert "Upstream research" in reopened.render_context(["research.md"])


@pytest.mark.asyncio
async def test_rehydration_skips_the_run_metadata(scratch):
    root, store = scratch
    orch = Orchestrator()
    workspace = MissionWorkspace("m_skip")
    await workspace.put("real.md", "the work", "a", "A", "s1")
    workspace.save_report("the briefing")
    workspace.save_manifest({"goal": "g"})
    with open(os.path.join(workspace.dir, tracing.TRACE_FILENAME), "w",
              encoding="utf-8") as fh:
        fh.write('{"kind":"mission"}\n')

    restored = orch._rehydrate_workspace(MissionWorkspace("m_skip"))
    assert restored == ["real.md"], (
        "report.md, mission.json and trace.jsonl are records of the run, "
        "not artifacts of it")


@pytest.mark.asyncio
async def test_resuming_an_unknown_mission_is_refused(scratch, monkeypatch):
    orch = Orchestrator()
    with pytest.raises(KeyError, match="no mission"):
        await orch.handle_user_input("g", resume_from="never_existed")


@pytest.mark.asyncio
async def test_resuming_a_mission_that_never_planned_is_refused(scratch, monkeypatch):
    """It died before there was a graph. There is nothing to carry on."""
    root, store = scratch
    orch = Orchestrator()
    store.start_mission("m_noplan", "g")
    with pytest.raises(ValueError, match="no stored plan"):
        await orch.handle_user_input("g", resume_from="m_noplan")


# -------------------------------------------------------------- unreachable

@pytest.mark.asyncio
async def test_steps_behind_a_failure_are_marked_unreachable(scratch, monkeypatch):
    root, store = scratch
    orch = Orchestrator()
    monkeypatch.setattr(settings, "step_max_attempts", 1)

    agent = _Agent(fail_with=ValueError("nope"))
    _wire(orch, monkeypatch, {"researcher": agent})
    workspace = MissionWorkspace("m_block")
    agent.workspace = workspace

    orch._slots = asyncio.Semaphore(2)
    store.start_mission("m_block", "g")
    plan = _linear_plan(3)
    results = await orch._execute_plan(plan, workspace, "m_block", "g")

    assert results == []
    assert [s.status for s in plan.steps] == ["failed", "failed", "failed"]
    rows = {r["step_id"]: r for r in store.get_steps("m_block")}
    assert "unreachable" in (rows["s3"]["error"] or "")


@pytest.mark.asyncio
async def test_a_mission_that_raises_is_not_left_running(scratch, monkeypatch):
    """
    `end_mission` sits at the end of the happy path, so a mission that raised
    on the way there kept the status `start_mission` gave it: the row read
    `running` for the life of the database, which is not "this broke" but
    "this is still going" -- the one thing it certainly was not.

    Seen for real on a mission whose planner took a 502 from the provider. No
    plan, no steps, and a row still claiming to be in flight hours later.
    """
    root, store = scratch
    orch = Orchestrator()
    _wire(orch, monkeypatch, {"researcher": _Agent()})

    store.start_mission("m_raise", "goal")
    store.record_plan("m_raise", _linear_plan(2))

    async def boom(*a, **kw):
        raise RuntimeError("planner took a 502")

    monkeypatch.setattr(orch, "_run_mission", boom)

    with pytest.raises(RuntimeError):
        await orch.handle_user_input("goal", resume_from="m_raise")

    got = store.get_mission("m_raise")
    assert got["status"] == "failed", "a mission that raised still reads as running"
    assert got["ended_at"] is not None


@pytest.mark.asyncio
async def test_a_cancelled_mission_is_not_left_running(scratch, monkeypatch):
    """
    CancelledError is not an Exception, so an `except Exception` here would
    catch the planner's 502 and miss the commonest way out that is not a
    return. A mission cancelled at shutdown strands its row exactly as a
    crashed one does.
    """
    root, store = scratch
    orch = Orchestrator()
    _wire(orch, monkeypatch, {"researcher": _Agent()})

    store.start_mission("m_cancel", "goal")
    store.record_plan("m_cancel", _linear_plan(2))

    async def hang(*a, **kw):
        await asyncio.sleep(3600)

    monkeypatch.setattr(orch, "_run_mission", hang)

    task = asyncio.create_task(orch.handle_user_input("goal", resume_from="m_cancel"))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert store.get_mission("m_cancel")["status"] == "failed"


@pytest.mark.asyncio
async def test_a_halt_that_escapes_is_not_downgraded_to_failed(scratch, monkeypatch):
    """
    A halt that goes through the normal path already ends as `halted`, and that
    distinction is the first thing you want from a red row: a run somebody
    stopped on purpose is not a run that broke. The rare halt that escapes by
    raising must not lose it.
    """
    root, store = scratch
    orch = Orchestrator()
    _wire(orch, monkeypatch, {"researcher": _Agent()})

    store.start_mission("m_halt", "goal")
    store.record_plan("m_halt", _linear_plan(2))

    async def boom(*a, **kw):
        orch._halted.add("m_halt")
        raise RuntimeError("stopped mid-flight")

    monkeypatch.setattr(orch, "_run_mission", boom)

    with pytest.raises(RuntimeError):
        await orch.handle_user_input("goal", resume_from="m_halt")

    assert store.get_mission("m_halt")["status"] == "halted"


@pytest.mark.asyncio
async def test_a_failed_resume_keeps_what_the_first_run_spent(scratch, monkeypatch):
    """
    The cost of a mission that died on the way to `end_mission` is unknown, not
    zero. A resumed mission is already carrying its first run's bill, and
    writing a confident 0.0 over that reports money as never having been spent.
    """
    root, store = scratch
    orch = Orchestrator()
    _wire(orch, monkeypatch, {"researcher": _Agent()})

    store.start_mission("m_cost", "goal")
    store.record_plan("m_cost", _linear_plan(2))
    store.end_mission("m_cost", "failed", 0.37)      # what the dead run spent

    async def boom(*a, **kw):
        raise RuntimeError("died again")

    monkeypatch.setattr(orch, "_run_mission", boom)

    with pytest.raises(RuntimeError):
        await orch.handle_user_input("goal", resume_from="m_cost")

    assert store.get_mission("m_cost")["cost_usd"] == pytest.approx(0.37)
