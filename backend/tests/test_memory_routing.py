"""
Tests for capability-constrained episodic memory routing.

Memory routing optimizes which specialist takes a step based on past competence,
but must never violate hard capability constraints (step.needs_tools) or hijack
steps assigned to temporary contractors spawned specifically for them.
"""
import os
import shutil
import tempfile
import pytest

from config import settings
from memory import memory
from orchestrator import orchestrator
from schemas import Plan, PlanStep


@pytest.fixture
def store_dir():
    path = tempfile.mkdtemp(prefix="agenthq_mem_test_")
    yield path
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def temp_memory(store_dir, monkeypatch):
    monkeypatch.setattr(settings, "memory_enabled", True)
    monkeypatch.setattr(settings, "memory_min_episodes_for_routing", 2)
    monkeypatch.setattr(settings, "memory_reroute_margin", 0.15)
    original_path, original_episodes = memory.path, list(memory.episodes)
    memory.path = os.path.join(store_dir, "memory.json")
    memory.episodes = []
    memory._reindex()
    yield memory
    memory.path, memory.episodes = original_path, original_episodes
    memory._reindex()


@pytest.mark.asyncio
async def test_memory_routing_rejects_challenger_lacking_tools(temp_memory):
    """
    Alice has a great record on data analysis tasks, but lacks query_data.
    Bob holds query_data. A step needing query_data must not be rerouted to Alice.
    """
    desc = "Query the sqlite database for active customers"
    for i in range(3):
        temp_memory.record(
            agent_id="researcher", agent_name="Alice",
            goal="Test goal", subtask=desc,
            outcome="success", summary="Found customers",
            duration_sec=1.0, total_tokens=100,
            tools_used=["read_file"], artifact=f"out_{i}.md"
        )

    step = PlanStep(
        id="s1",
        agent_id="data_analyst",
        description=desc,
        needs_tools=["query_data"],
        produces="customers.csv",
    )
    plan = Plan(goal="Customer audit", source="llm", steps=[step])

    orchestrator._apply_memory_routing(plan, "test_task")

    # Bob must retain the step because Alice does not hold query_data
    assert step.agent_id == "data_analyst"
    assert step.routed_from is None


@pytest.mark.asyncio
async def test_memory_routing_accepts_qualified_challenger(temp_memory):
    """
    Bob has a strong record on calculation tasks.
    Charlie holds calculate, but Bob holds calculate as well.
    A step needing calculate should successfully reroute from Charlie to Bob.
    """
    desc = "Calculate the compound interest formula"
    for i in range(3):
        temp_memory.record(
            agent_id="data_analyst", agent_name="Bob",
            goal="Math goal", subtask=desc,
            outcome="success", summary="Math done",
            duration_sec=1.0, total_tokens=100,
            tools_used=["calculate"], artifact=f"calc_{i}.md"
        )

    step = PlanStep(
        id="s1",
        agent_id="coder",
        description=desc,
        needs_tools=["calculate"],
        produces="interest.md",
    )
    plan = Plan(goal="Interest calculation", source="llm", steps=[step])

    orchestrator._apply_memory_routing(plan, "test_task")

    # Both coder and data_analyst hold calculate; Bob's record wins
    assert step.agent_id == "data_analyst"
    assert step.routed_from == "coder"


@pytest.mark.asyncio
async def test_memory_routing_never_hijacks_spawned_contractors(temp_memory):
    """
    A contractor hired for a gap step must never be rerouted away,
    even if an existing agent has high competence on similar text.
    """
    desc = "Run terminal commands and web search in parallel"
    for i in range(3):
        temp_memory.record(
            agent_id="coder", agent_name="Charlie",
            goal="Python goal", subtask=desc,
            outcome="success", summary="Commands run",
            duration_sec=1.0, total_tokens=100,
            tools_used=["terminal"], artifact=f"run_{i}.md"
        )

    step = PlanStep(
        id="s1",
        agent_id="contractor_nina",
        description=desc,
        needs_tools=["terminal", "web_search"],
        produces="audit.md",
    )
    plan = Plan(goal="Security check", source="llm", steps=[step])

    # Mark contractor_nina as spawned
    orchestrator._spawned_ids.add("contractor_nina")
    try:
        orchestrator._apply_memory_routing(plan, "test_task")
        assert step.agent_id == "contractor_nina"
        assert step.routed_from is None
    finally:
        orchestrator._spawned_ids.discard("contractor_nina")


@pytest.mark.asyncio
async def test_memory_routing_write_file_auto_grant_on_produces(temp_memory):
    """
    If a step produces an artifact, write_file is automatically granted by
    _run_step. A candidate holding web_search should not be rejected simply
    because their base manifest does not list write_file.
    """
    desc = "Search for latest AI developments"
    for i in range(3):
        temp_memory.record(
            agent_id="researcher", agent_name="Alice",
            goal="AI research", subtask=desc,
            outcome="success", summary="Researched",
            duration_sec=1.0, total_tokens=100,
            tools_used=["web_search"], artifact=f"ai_{i}.md"
        )

    # Eve holds write_file and web_search; Alice holds web_search but not write_file in base manifest
    step = PlanStep(
        id="s1",
        agent_id="creative",
        description=desc,
        needs_tools=["web_search", "write_file"],
        produces="summary.md",  # producing step -> write_file is auto-granted
    )
    plan = Plan(goal="Research mission", source="llm", steps=[step])

    orchestrator._apply_memory_routing(plan, "test_task")

    # Alice should be eligible despite not listing write_file, and her score wins
    assert step.agent_id == "researcher"
    assert step.routed_from == "creative"
