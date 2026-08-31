import pytest
import asyncio
from schemas import Event, EventType, AgentStatus, AgentManifest, Task
from event_bus import EventBus


@pytest.mark.asyncio
async def test_event_bus_pub_sub():
    bus = EventBus()
    received_events = []

    async def on_agent_status(event: Event):
        received_events.append(event)

    bus.subscribe(EventType.AGENT_STATUS, on_agent_status)

    test_event = Event(
        type=EventType.AGENT_STATUS,
        agent_id="scout",
        data={"status": AgentStatus.ACTIVE.value, "mood": "focused"}
    )

    await bus.emit(test_event)
    await asyncio.sleep(0.05)  # Allow async tasks to dispatch

    assert len(received_events) == 1
    assert received_events[0].agent_id == "scout"
    assert received_events[0].data["status"] == "active"


@pytest.mark.asyncio
async def test_event_bus_global_subscriber():
    bus = EventBus()
    all_events = []

    def on_any_event(event: Event):
        all_events.append(event)

    bus.subscribe_all(on_any_event)

    await bus.emit(Event(type=EventType.TASK_CREATED, data={"task": "build app"}))
    await bus.emit(Event(type=EventType.AGENT_SPEAKING, data={"text": "Starting work"}))
    await asyncio.sleep(0.05)

    assert len(all_events) == 2
    assert all_events[0].type == EventType.TASK_CREATED
    assert all_events[1].type == EventType.AGENT_SPEAKING


def test_agent_manifest_validation():
    manifest = AgentManifest(
        id="jim",
        name="Jim (Bug Hunter)",
        role="Test & Code Verification Specialist",
        avatar_emoji="🐛",
        system_prompt="You find and patch bugs.",
        tools=["terminal", "code_analysis"],
        desk_position={"x": 200, "y": 300}
    )

    assert manifest.id == "jim"
    assert manifest.desk_position.x == 200
    assert "terminal" in manifest.tools
