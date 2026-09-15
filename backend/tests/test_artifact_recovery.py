import asyncio
import os
import shutil
import tempfile
import pytest
from unittest.mock import AsyncMock, patch

from base_agent import BaseAgent
from config import settings
from orchestrator import Orchestrator
from schemas import AgentManifest, PlanStep, Task
from workspace import MissionWorkspace


@pytest.fixture
def missions(monkeypatch):
    root = tempfile.mkdtemp(prefix="agenthq_missions_")
    monkeypatch.setattr(settings, "missions_path", os.path.join(root, "missions"))
    monkeypatch.setattr(settings, "persist_missions", True)
    yield root
    shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def manifest():
    return AgentManifest(
        id="creative",
        name="Eve",
        role="Creative Specialist",
        skills=["writing"],
        tools=["read_file", "write_file", "search_workspace"],
        system_prompt="You are a creative writer."
    )


@pytest.mark.asyncio
async def test_truncated_turn_retries_with_guidance(manifest, monkeypatch):
    agent = BaseAgent(manifest)
    task = Task(id="t1", description="Write comprehensive report", assigned_to="creative")

    step_counter = 0

    async def mock_call_llm(messages, tools, task_arg):
        nonlocal step_counter
        step_counter += 1
        if step_counter == 1:
            # Simulate turn truncated due to token budget during reasoning
            return {
                "truncated": True,
                "content": "",
                "thought": "Thinking...",
                "tool_calls": [],
                "prompt_tokens": 100,
                "completion_tokens": 8000,
                "cost_usd": 0.01,
                "model": "test-model",
                "attempts": 1,
                "fell_back": False,
                "estimated": False
            }
        # Turn 2 delivers real answer
        return {
            "truncated": False,
            "content": "# Full Deliverable Report Content",
            "thought": "Writing deliverable",
            "tool_calls": [],
            "prompt_tokens": 200,
            "completion_tokens": 500,
            "cost_usd": 0.002,
            "model": "test-model",
            "attempts": 1,
            "fell_back": False,
            "estimated": False
        }

    monkeypatch.setattr(agent, "_call_llm", mock_call_llm)

    result = await agent.run(task, produces="final_report.md")
    assert result == "# Full Deliverable Report Content"
    assert step_counter == 2


@pytest.mark.asyncio
async def test_late_step_tool_gating_blocks_exploration(manifest, monkeypatch):
    agent = BaseAgent(manifest)
    task = Task(id="t2", description="Write final report", assigned_to="creative")

    # Set max steps to 2 so step 1 has remaining = 1 (final turn)
    monkeypatch.setattr(settings, "max_agent_steps", 2)

    turn = 0
    received_tool_error = False

    async def mock_call_llm(messages, tools, task_arg):
        nonlocal turn, received_tool_error
        turn += 1
        if turn == 1:
            # Model attempts search_workspace on final turn
            return {
                "truncated": False,
                "content": "",
                "thought": "Searching...",
                "tool_calls": [{"name": "search_workspace", "arguments": {"query": "stats"}}],
                "prompt_tokens": 100,
                "completion_tokens": 50,
                "cost_usd": 0.001,
                "model": "test-model",
                "attempts": 1,
                "fell_back": False,
                "estimated": False
            }
        # Inspect messages to see if tool failure was injected
        for msg in messages:
            if msg.get("role") == "tool" and "Turn limit reached" in str(msg.get("content")):
                received_tool_error = True
        return {
            "truncated": False,
            "content": "Done with report.",
            "thought": "Completed",
            "tool_calls": [],
            "prompt_tokens": 150,
            "completion_tokens": 30,
            "cost_usd": 0.001,
            "model": "test-model",
            "attempts": 1,
            "fell_back": False,
            "estimated": False
        }

    monkeypatch.setattr(agent, "_call_llm", mock_call_llm)

    res = await agent.run(task, extra_tools=["write_file"], produces="final_report.md")
    assert received_tool_error is True
    assert res == "Done with report."


@pytest.mark.asyncio
async def test_orchestrator_recovers_missing_artifact_from_workspace(missions, monkeypatch):
    orch = Orchestrator()
    w = MissionWorkspace("m_recover")
    await w.put("evidence.json", '{"findings": "AI SaaS solves compliance pain"}', "alice", "Alice", "s1")
    await w.put("analysis.json", '{"tam": "$2B"}', "alice", "Alice", "s2")

    step = PlanStep(
        id="s4",
        agent_id="creative",
        description="Write final report summarizing evidence.json and analysis.json",
        produces="final_report.md",
        consumes=["evidence.json", "analysis.json"]
    )

    class StubSpecialist:
        name = "Eve"

    # Mock llm.acompletion to return a synthesized markdown document
    class MockChoice:
        message = type("Message", (), {"content": "# Synthesized Executive Report\n\nFull recovered content." * 5})()

    class MockResult:
        response = type("Resp", (), {"choices": [MockChoice()]})()

    sent = {}

    async def mock_acompletion(*args, **kwargs):
        sent.update(kwargs)
        return MockResult()

    monkeypatch.setattr("llm.acompletion", mock_acompletion)
    monkeypatch.setattr("orchestrator.llm_configured", lambda: True)

    recovered = await orch._recover_missing_artifact(
        w, step, StubSpecialist(), "Create complete report", ["Length too short"]
    )

    assert recovered is not None
    assert "Synthesized Executive Report" in recovered
    assert w.get("final_report.md") is not None
    assert w.get("final_report.md").content == recovered
    # Marked in the file itself, and the model was held to the documents.
    from orchestrator import RECOVERY_BANNER
    assert recovered.startswith(RECOVERY_BANNER)
    prompt = sent["messages"][-1]["content"]
    assert "Invent none of them" in prompt


@pytest.mark.asyncio
async def test_a_recovered_artifact_does_not_pass_the_step(missions, monkeypatch):
    """
    Recovery used to set the verdict to "accept" with no reviewer reading the
    result. Observed: a recovered report of invented records, URLs and meeting
    quotes filed as a passing step. It stays "revise" now, and says recovered.
    """
    import contracts
    orch = Orchestrator()
    w = MissionWorkspace("m_recover_verdict")
    step = PlanStep(id="s1", agent_id="creative", description="Write the report",
                    produces="report_out.md")

    class Specialist:
        id = "creative"
        name = "Eve"
        tools = ["write_file"]
        last_run = {"estimated": False}

    class Span:
        def set(self, **kw):
            self.attrs = kw

    async def dispatch(*args, **kwargs):
        return ""                         # the agent wrote nothing

    async def recover(*args, **kwargs):
        return RECOVERY_TEXT

    RECOVERY_TEXT = "> reconstructed\n\n# Report\n\n" + "text " * 40
    monkeypatch.setattr(settings, "memory_enabled", False)
    monkeypatch.setattr(settings, "max_repair_attempts", 0)
    monkeypatch.setattr(orch, "_agent_by_id", lambda _id: Specialist())
    monkeypatch.setattr(orch, "_dispatch_attempt", dispatch)
    monkeypatch.setattr(orch, "_recover_missing_artifact", recover)
    monkeypatch.setattr(contracts, "check",
                        lambda text, spec: contracts.CheckResult(ok=False, failures=["empty"]))

    span = Span()
    result = await orch._run_step_body(step, w, "m_recover_verdict", "goal", span)

    assert result["verdict"] == "revise"
    assert result["recovered"] is True
    assert span.attrs["recovered"] is True
