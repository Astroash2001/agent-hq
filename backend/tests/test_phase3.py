import pytest
import asyncio
from orchestrator import orchestrator
from schemas import EventType


def test_orchestrator_loads_manifests():
    manifests = orchestrator.get_manifests()
    assert len(manifests) >= 4  # orchestrator + researcher + coder + data_analyst + creative
    ids = [m["id"] for m in manifests]
    assert "orchestrator" in ids
    assert "coder" in ids
    assert "researcher" in ids


@pytest.mark.asyncio
async def test_orchestrator_execution_pipeline():
    result = await orchestrator.handle_user_input("Implement and test a fast Python fibonacci cache")
    assert result["status"] == "completed"
    assert "fibonacci" in result["result"].lower() or "mission briefing" in result["result"].lower()
    assert len(result["subtasks"]) >= 2
