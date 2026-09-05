"""
What `dag.update` carries, now that a panel reads it.

The node used to hold a status and nothing else, so the HUD could draw the
shape of a mission but not its price -- cost and latency existed only in
`mission.json`, written once at the end. The cost panel reads this event
instead of keeping its own ledger, which is only safe if the event says the
same thing the manifest will.

The case worth pinning is the step that has not run: it must carry no numbers
at all, because a panel that draws "not started" as $0.0000 is telling the
viewer the step was free.
"""
import asyncio

import pytest

from event_bus import event_bus
from orchestrator import Orchestrator
from schemas import DAGNode, Event, EventType


async def _emit_and_capture(orch, task_id):
    seen = []

    async def _listen(event: Event):
        seen.append(event)

    event_bus.subscribe(EventType.DAG_UPDATE, _listen)
    try:
        await orch._emit_dag(task_id, "llm")
        await asyncio.sleep(0.01)
    finally:
        event_bus.unsubscribe(EventType.DAG_UPDATE, _listen)
    assert len(seen) == 1
    return seen[0].data


@pytest.fixture
def orch():
    o = Orchestrator()
    o.current_dag = [
        DAGNode(id="s1", label="research", agent_id="researcher", status="completed"),
        DAGNode(id="s2", label="write", agent_id="creative", status="running",
                dependencies=["s1"]),
        DAGNode(id="s3", label="review", agent_id="coder", status="pending",
                dependencies=["s2"]),
    ]
    o._step_telemetry["t1"] = {
        "s1": {"step_id": "s1", "agent_id": "researcher", "model": "gemini/gemini-2.5-flash",
               "prompt_tokens": 900, "completion_tokens": 100, "cost_usd": 0.0031,
               "duration_sec": 4.25, "attempts": 1, "estimated": False},
    }
    return o


@pytest.mark.asyncio
async def test_a_finished_step_carries_what_it_cost(orch):
    payload = await _emit_and_capture(orch, "t1")
    node = next(n for n in payload["dag"] if n["id"] == "s1")

    assert node["cost_usd"] == 0.0031
    assert node["duration_sec"] == 4.25
    assert node["total_tokens"] == 1000
    assert node["model"] == "gemini/gemini-2.5-flash"
    assert node["estimated"] is False
    # And still everything the graph needed before.
    assert node["status"] == "completed"
    assert node["agent_id"] == "researcher"


@pytest.mark.asyncio
async def test_a_step_that_has_not_run_carries_no_numbers(orch):
    """"Not started" and "free" are different things, and 0.0 says the second."""
    payload = await _emit_and_capture(orch, "t1")
    node = next(n for n in payload["dag"] if n["id"] == "s3")

    assert "cost_usd" not in node
    assert "duration_sec" not in node
    assert "total_tokens" not in node
    assert node["status"] == "pending"


@pytest.mark.asyncio
async def test_the_mission_total_is_this_mission_not_the_process(orch):
    """
    `_tokens_spent()` sums the roster's lifetime counters, which climb across
    every mission a process has served. The panel must never be handed that.
    """
    orch._step_telemetry["t1"]["s2"] = {
        "step_id": "s2", "agent_id": "creative", "model": "sim",
        "prompt_tokens": 400, "completion_tokens": 50, "cost_usd": 0.001,
        "duration_sec": 2.0, "attempts": 1, "estimated": True,
    }
    payload = await _emit_and_capture(orch, "t1")

    assert payload["cost_usd"] == pytest.approx(0.0041)
    assert payload["mission_tokens"] == 1450
    # One estimated step is enough to make the whole figure an estimate.
    assert payload["tokens_estimated"] is True


@pytest.mark.asyncio
async def test_a_mission_with_no_telemetry_yet_still_emits_a_graph(orch):
    """The first dag.update fires before any step has finished."""
    payload = await _emit_and_capture(orch, "t_unknown")

    assert [n["id"] for n in payload["dag"]] == ["s1", "s2", "s3"]
    assert payload["cost_usd"] == 0.0
    assert payload["mission_tokens"] == 0
    assert payload["tokens_estimated"] is False
    assert payload["source"] == "llm"
