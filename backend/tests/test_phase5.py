"""
Tests for the planner, the parallel scheduler + workspace, the spatial runtime
and episodic memory.

The memory tests redirect the singleton at a temp file first, so running the
suite never wipes the record the office has actually accumulated.
"""
import asyncio
import os
import shutil
import tempfile
import time

import pytest

from config import settings
from event_bus import event_bus
from memory import AgentMemory, memory, tokenize
from office import OfficeFloor, office
from planner import PlanValidationError, heuristic_plan, validate_plan
from schemas import Event, EventType
from workspace import MissionWorkspace

AGENT_IDS = ["researcher", "coder", "data_analyst", "creative"]


@pytest.fixture
def store_dir():
    """A throwaway directory for memory stores, cleaned up afterwards."""
    path = tempfile.mkdtemp(prefix="agenthq_test_")
    yield path
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def temp_memory(store_dir):
    """Point the memory singleton at a throwaway store for the duration of a test."""
    original_path, original_episodes = memory.path, list(memory.episodes)
    memory.path = os.path.join(store_dir, "memory.json")
    memory.episodes = []
    memory._reindex()
    yield memory
    memory.path, memory.episodes = original_path, original_episodes
    memory._reindex()


# --------------------------------------------------------------------- planner

def test_heuristic_plan_fans_out():
    """The coding bucket must contain a genuine parallel branch, not a chain."""
    steps = heuristic_plan("Fix the bug in the python script", AGENT_IDS)
    assert len(steps) == 4
    parallel = [s for s in steps if s.dependencies == ["s1"]]
    assert len(parallel) == 2, "s2 and s3 should both depend only on s1"
    assert steps[-1].dependencies == ["s2", "s3"], "final step should fan in"


def test_heuristic_plan_drops_missing_agents():
    """A roster without the creative agent must not produce steps assigned to it."""
    steps = heuristic_plan("Analyse this csv data", ["data_analyst", "researcher"])
    assert steps, "should still produce a plan"
    assert all(s.agent_id in ("data_analyst", "researcher") for s in steps)
    known = {s.id for s in steps}
    for step in steps:
        assert set(step.dependencies) <= known, "orphaned dependency survived"


def test_validate_plan_repairs_model_output():
    """Unknown agents are dropped, ids renumbered, dependencies remapped."""
    raw = {"steps": [
        {"id": "alpha", "agent_id": "researcher", "description": "look it up",
         "produces": "notes.md"},
        {"id": "beta", "agent_id": "nobody", "description": "impossible"},
        {"id": "gamma", "agent_id": "coder", "description": "build it",
         "dependencies": ["alpha"], "produces": "impl.md"},
    ]}
    steps = validate_plan(raw, AGENT_IDS)
    assert [s.id for s in steps] == ["s1", "s2"]
    assert steps[1].agent_id == "coder"
    assert steps[1].dependencies == ["s1"], "'alpha' should remap onto the canonical id"
    assert steps[1].consumes == ["notes.md"], "upstream artifact handed over automatically"


def test_validate_plan_rejects_forward_and_cyclic_dependencies():
    """A step may only wait on steps before it, which is what keeps the DAG acyclic."""
    raw = {"steps": [
        {"id": "s1", "agent_id": "coder", "description": "first", "dependencies": ["s2"]},
        {"id": "s2", "agent_id": "creative", "description": "second", "dependencies": ["s1"]},
    ]}
    steps = validate_plan(raw, AGENT_IDS)
    assert steps[0].dependencies == []
    assert steps[1].dependencies == ["s1"]


def test_validate_plan_rejects_unusable_payload():
    with pytest.raises(PlanValidationError):
        validate_plan({"steps": []}, AGENT_IDS)
    with pytest.raises(PlanValidationError):
        validate_plan({"steps": [{"agent_id": "ghost", "description": "x"}]}, AGENT_IDS)


# ------------------------------------------------------------------- workspace

@pytest.mark.asyncio
async def test_workspace_feeds_results_forward():
    ws = MissionWorkspace("task_test")
    await ws.put("research_notes.md", "Use an LRU cache with maxsize=None.",
                 "researcher", "Alice", "s1")

    context = ws.render_context(["research_notes.md"])
    assert "LRU cache" in context
    assert "Alice" in context, "the downstream agent should know who produced it"
    assert ws.render_context(["missing.md"]) == "", "absent artifacts render as nothing"


@pytest.mark.asyncio
async def test_workspace_delivery_emits_both_endpoints():
    """The renderer needs sender and recipient to fly the artifact between desks."""
    ws = MissionWorkspace("task_test")
    await ws.put("analysis.md", "Row counts check out.", "data_analyst", "Bob", "s1")

    seen = []

    async def tap(event: Event):
        seen.append(event)

    event_bus.subscribe(EventType.ARTIFACT_DELIVERED, tap)
    try:
        await ws.deliver(["analysis.md"], "creative", "Eve", "s2")
        await asyncio.sleep(0.05)
    finally:
        event_bus.unsubscribe(EventType.ARTIFACT_DELIVERED, tap)

    assert len(seen) == 1
    data = seen[0].data
    assert data["from_agent_id"] == "data_analyst"
    assert data["to_agent_id"] == "creative"


# ---------------------------------------------------------------------- office

def test_stations_bind_tools():
    assert office.station_for_tool("terminal").id == "ops_console"
    assert office.station_for_tool("web_search").id == "research_library"
    assert office.station_for_tool("write_file").id == "filing_archive"
    assert office.station_for_tool("not_a_tool") is None


def test_travel_time_scales_with_distance_and_is_clamped():
    floor = OfficeFloor()
    near = floor.travel_seconds((5.0, 5.0), (6.0, 5.0))
    far = floor.travel_seconds((2.0, 2.0), (38.0, 22.0))
    assert near <= far
    assert settings.min_walk_seconds <= near <= settings.max_walk_seconds
    assert far <= settings.max_walk_seconds


@pytest.mark.asyncio
async def test_station_serialises_two_agents():
    """
    Two agents needing the same station must not overlap, and the one that waits
    must announce the queue.
    """
    floor = OfficeFloor()
    holding = []
    queued = []

    async def tap(event: Event):
        queued.append(event)

    event_bus.subscribe(EventType.STATION_QUEUED, tap)

    async def use(agent_id: str):
        async with floor.use_tool_station(agent_id, "terminal", agent_name=agent_id) as station:
            assert station is not None
            holding.append(agent_id)
            assert len(holding) == 1, "two agents held the same station at once"
            await asyncio.sleep(0.2)
            holding.remove(agent_id)

    try:
        await asyncio.gather(use("coder"), use("data_analyst"))
        await asyncio.sleep(0.05)
    finally:
        event_bus.unsubscribe(EventType.STATION_QUEUED, tap)

    assert len(queued) == 1, "the second agent should have reported queueing"
    assert queued[0].data["station_id"] == "ops_console"


@pytest.mark.asyncio
async def test_station_released_when_the_tool_raises():
    """A crashing tool must not deadlock the floor."""
    floor = OfficeFloor()
    with pytest.raises(RuntimeError):
        async with floor.use_tool_station("coder", "terminal"):
            raise RuntimeError("tool blew up")

    async with floor.use_tool_station("researcher", "terminal") as station:
        assert station is not None, "station should be free again"


@pytest.mark.asyncio
async def test_spatial_can_be_switched_off(monkeypatch):
    floor = OfficeFloor()
    monkeypatch.setattr(settings, "spatial_enabled", False)
    async with floor.use_tool_station("coder", "terminal") as station:
        assert station is None, "tools should be callable from the desk when disabled"


# ---------------------------------------------------------------------- memory

def test_tokenize_drops_stopwords():
    tokens = tokenize("Please run the tests for the Python fibonacci cache")
    assert "fibonacci" in tokens and "cache" in tokens
    assert "the" not in tokens and "for" not in tokens


def test_recall_prefers_similar_episodes(store_dir):
    store = AgentMemory(path=os.path.join(store_dir, "memory.json"))
    store.record("coder", "Charlie", "goal", "Optimise the fibonacci cache", "success")
    store.record("coder", "Charlie", "goal", "Draft the quarterly marketing copy", "success")

    recalled = store.recall("coder", "make the fibonacci cache faster", k=1)
    assert len(recalled) == 1
    assert "fibonacci" in recalled[0].subtask.lower()


def test_recall_is_scoped_to_the_agent(store_dir):
    store = AgentMemory(path=os.path.join(store_dir, "memory.json"))
    store.record("coder", "Charlie", "goal", "Optimise the fibonacci cache", "success")
    assert store.recall("researcher", "fibonacci cache") == []


def test_memory_survives_a_reload(store_dir):
    path = os.path.join(store_dir, "memory.json")
    AgentMemory(path=path).record("coder", "Charlie", "g", "Ship the parser", "success")
    assert len(AgentMemory(path=path).episodes) == 1


def test_competence_is_neutral_without_evidence(store_dir):
    store = AgentMemory(path=os.path.join(store_dir, "memory.json"))
    assert store.competence("coder", "anything at all") == 0.5


def test_routing_reassigns_a_step_to_the_stronger_agent(store_dir, monkeypatch):
    """
    A specialist with a track record of failing this kind of work loses the step
    to one that has succeeded at it.
    """
    store = AgentMemory(path=os.path.join(store_dir, "memory.json"))
    monkeypatch.setattr(settings, "memory_min_episodes_for_routing", 4)

    for _ in range(3):
        store.record("coder", "Charlie", "g", "Write the SQL migration query", "failure")
        store.record("data_analyst", "Bob", "g", "Write the SQL migration query", "success")

    suggestion = store.suggest_agent("Write the SQL migration query", AGENT_IDS, "coder")
    assert suggestion is not None
    new_id, new_score, old_score = suggestion
    assert new_id == "data_analyst"
    assert new_score > old_score


def test_routing_stays_out_of_the_way_on_a_cold_memory(store_dir):
    store = AgentMemory(path=os.path.join(store_dir, "memory.json"))
    store.record("data_analyst", "Bob", "g", "Write the SQL migration query", "success")
    assert store.suggest_agent("Write the SQL migration query", AGENT_IDS, "coder") is None


# -------------------------------------------------------------------- pipeline

@pytest.mark.asyncio
async def test_mission_runs_independent_steps_in_parallel(temp_memory):
    """
    End to end: the graph runs, the fan-out really is concurrent, every step
    files an artifact, and the run is written to memory.
    """
    from orchestrator import orchestrator

    starts = {}
    artifacts_created = []

    async def tap(event: Event):
        if event.type == EventType.TASK_ASSIGNED:
            starts[event.data["step_id"]] = time.time()
        elif event.type == EventType.ARTIFACT_CREATED:
            artifacts_created.append(event.data["name"])

    event_bus.subscribe(EventType.TASK_ASSIGNED, tap)
    event_bus.subscribe(EventType.ARTIFACT_CREATED, tap)
    try:
        result = await orchestrator.handle_user_input(
            "Implement and test a fast Python fibonacci cache"
        )
        await asyncio.sleep(0.1)
    finally:
        event_bus.unsubscribe(EventType.TASK_ASSIGNED, tap)
        event_bus.unsubscribe(EventType.ARTIFACT_CREATED, tap)

    assert result["status"] == "completed"
    assert len(result["subtasks"]) == 4
    # The final artifact is named after what the goal asked for rather than
    # always "briefing.md" -- this goal asks for neither a brief nor a guide nor
    # a summary, so it lands as report.md. See planner._deliverable_name.
    assert {a["name"] for a in result["artifacts"]} == {
        "research_notes.md", "implementation.md", "analysis.md", "report.md"
    }

    # s2 and s3 are the fan-out; they must have been dispatched together.
    assert abs(starts["s2"] - starts["s3"]) < 0.5

    # Every finished step is now something the office remembers.
    assert len(temp_memory.episodes) == 4
    assert temp_memory.stats("coder")["runs"] == 1


@pytest.mark.asyncio
async def test_second_mission_recalls_the_first(temp_memory):
    """Once there is history, agents start their next task holding it."""
    from orchestrator import orchestrator

    recalled = []

    async def tap(event: Event):
        recalled.append(event)

    await orchestrator.handle_user_input("Fix the bug in the python cache script")

    event_bus.subscribe(EventType.MEMORY_RECALLED, tap)
    try:
        await orchestrator.handle_user_input("Fix the bug in the python cache script")
        await asyncio.sleep(0.1)
    finally:
        event_bus.unsubscribe(EventType.MEMORY_RECALLED, tap)

    assert recalled, "the second run should have recalled the first"
    assert recalled[0].data["episodes"] >= 1
