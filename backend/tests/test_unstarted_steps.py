"""
A step that failed without ever being dispatched.

Two routes reach that state: everything left depends on something that failed,
and the token budget ran out between waves. Both wrote the run store and the
mission manifest and stopped there -- no trace span, and no per-step event. So
the same mission disagreed with itself depending on where you read it:
`mission.json` said `failed`, and a replay of `trace.jsonl` showed the step
`pending` for ever, because `replay.js` rebuilds a step from its span and there
was none. `tools/check_replay.js` catches it on two stored missions:

    FAIL  never assigned: s3
    FAIL  s3: manifest says "failed", replay ends "pending"

The office had the matching hole: the DAG node went red from a live
`dag.update`, but no `task.failed` named the step, so the agent's card never
showed it and the log never said which steps died.

A step span with `status="error"` is what a replay turns back into a
`task.failed`, so these tests pin the span, its status, and the event.
"""
import asyncio
import json
import shutil
import tempfile

import pytest

import tracing
from event_bus import event_bus
from orchestrator import Orchestrator
from schemas import DAGNode, Event, EventType, PlanStep


class _Store:
    """Just enough run store to record what it was told."""

    def __init__(self):
        self.rows = []

    def upsert_step(self, task_id, step, status, error=""):
        self.rows.append({"task_id": task_id, "step_id": step.id,
                          "status": status, "error": error})


@pytest.fixture
def trace_dir():
    """
    A private directory for one mission's trace.

    `tmp_path` is not used: pytest's own temp root is not writable for this
    user on this machine (WinError 5 on C:/.../Temp/pytest-of-avina), and the
    rest of the suite already reaches for `tempfile.mkdtemp` for the same
    reason.
    """
    root = tempfile.mkdtemp(prefix="agenthq_unstarted_")
    yield root
    shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def orch():
    o = Orchestrator()
    o.agents = {}
    o.boss_agent = None
    o.current_dag = [DAGNode(id="s3", label="write", agent_id="creative",
                             status="pending", dependencies=["s2"])]
    return o


async def _capture(coro):
    """Run `coro` and collect the task.failed events it emits."""
    seen = []

    async def _listen(event: Event):
        seen.append(event)

    event_bus.subscribe(EventType.TASK_FAILED, _listen)
    try:
        await coro
        await asyncio.sleep(0.01)
    finally:
        event_bus.unsubscribe(EventType.TASK_FAILED, _listen)
    return seen


@pytest.mark.asyncio
async def test_an_unstarted_step_is_written_to_the_store_and_the_dag(orch, trace_dir):
    step = PlanStep(id="s3", agent_id="creative", description="write the report")
    store = _Store()

    tracing.start_mission("m1", trace_dir)
    try:
        await _capture(orch._record_unstarted_step(
            "m1", step, "unreachable after an upstream failure", store))
    finally:
        tracing.end_mission()

    assert step.status == "failed"
    assert orch.current_dag[0].status == "failed"
    assert store.rows == [{"task_id": "m1", "step_id": "s3", "status": "failed",
                           "error": "unreachable after an upstream failure"}]


@pytest.mark.asyncio
async def test_it_leaves_a_failed_step_span_so_a_replay_agrees(orch, trace_dir):
    """
    The regression. `replay.js` turns a `step` span with `status == "error"`
    into a `task.failed` and marks the step failed; with no span it stayed
    `pending` while the manifest said `failed`.
    """
    step = PlanStep(id="s3", agent_id="creative", description="write the report")

    tracing.start_mission("m2", trace_dir)
    try:
        await _capture(orch._record_unstarted_step(
            "m2", step, "token budget exhausted", _Store()))
    finally:
        # `end_mission` returns the path it wrote, so the spans are read back
        # from the file a replay would actually load.
        path = tracing.end_mission()

    spans = [json.loads(line) for line in open(path, encoding="utf-8")]
    steps = [s for s in spans if s["kind"] == "step"]
    assert len(steps) == 1
    span = steps[0]
    assert span["attrs"].get("step_id") == "s3"
    assert span["attrs"].get("agent_id") == "creative"
    assert span["attrs"].get("not_started") is True
    # The two fields a replay reads to reproduce the failure.
    assert span["status"] == "error"
    assert span["error"] == "token budget exhausted"


@pytest.mark.asyncio
async def test_it_names_the_step_in_a_task_failed_event(orch, trace_dir):
    """
    The office needs to know WHICH step died. The budget path emitted one
    mission-level message ("3 step(s) not started") and the unreachable path
    emitted nothing, so no agent card ever showed the failure.
    """
    step = PlanStep(id="s3", agent_id="creative", description="write the report")

    tracing.start_mission("m3", trace_dir)
    try:
        seen = await _capture(orch._record_unstarted_step(
            "m3", step, "unreachable after an upstream failure", _Store()))
    finally:
        tracing.end_mission()

    assert len(seen) == 1
    assert seen[0].agent_id == "creative"
    assert seen[0].data["step_id"] == "s3"
    assert "unreachable" in seen[0].data["error"]


@pytest.mark.asyncio
async def test_tracing_off_does_not_break_the_recording(orch, trace_dir, monkeypatch):
    """No mission span open -- a unit test, or tracing disabled. Still records."""
    from config import settings
    monkeypatch.setattr(settings, "tracing_enabled", False)
    step = PlanStep(id="s3", agent_id="creative", description="write the report")
    store = _Store()

    await _capture(orch._record_unstarted_step("m4", step, "budget", store))

    assert step.status == "failed"
    assert store.rows[0]["status"] == "failed"
