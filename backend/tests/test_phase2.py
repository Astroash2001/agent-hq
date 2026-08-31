import pytest
import asyncio
from schemas import AgentManifest, Task, EventType, AgentStatus
from tool_registry import tool_registry
from base_agent import BaseAgent
from event_bus import event_bus


@pytest.mark.asyncio
async def test_tool_registry_terminal():
    result = await tool_registry.execute("terminal", {"command": "python --version"})
    assert result["success"] is True
    assert "Python" in result["stdout"] or "Python" in result["stderr"]


@pytest.mark.asyncio
async def test_tool_registry_web_search():
    result = await tool_registry.execute("web_search", {"query": "safe SQL and RAG best practices"})
    assert result["success"] is True
    assert len(result["results"]) > 0


@pytest.mark.asyncio
async def test_base_agent_react_loop():
    manifest = AgentManifest(
        id="scout",
        name="Scout (Researcher)",
        role="Research & Retrieval Specialist",
        avatar_emoji="🔍",
        system_prompt="You research data.",
        tools=["web_search", "read_file"],
        desk_position={"x": 100, "y": 200}
    )

    agent = BaseAgent(manifest)
    task = Task(id="task_1", description="Research RAG chunking techniques")

    emitted_statuses = []

    async def track_status(event):
        if event.agent_id == "scout":
            emitted_statuses.append(event.data.get("status"))

    event_bus.subscribe(EventType.AGENT_STATUS, track_status)

    result = await agent.run(task)
    await asyncio.sleep(0.1)  # Yield to event loop to let async event tasks process

    assert result is not None
    assert len(result) > 0
    assert "done" in emitted_statuses
