import pytest
from fastapi.testclient import TestClient
from main import app
from event_bus import event_bus
from schemas import EventType


client = TestClient(app)


def test_health_check():
    response = client.get("/api/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"
    assert data["agents_count"] >= 4


def test_get_agents_endpoint():
    response = client.get("/api/agents")
    assert response.status_code == 200
    agents = response.json()
    assert len(agents) >= 4
    agent_ids = [a["id"] for a in agents]
    assert "orchestrator" in agent_ids
    assert "coder" in agent_ids


def test_websocket_connection():
    event_bus.clear_history()
    with client.websocket_connect("/ws") as websocket:
        # First message should be system handshake
        data = websocket.receive_json()
        assert data["type"] == EventType.SYSTEM_STATUS.value
        assert "Connected" in data["data"]["message"]

        # Test ping / pong
        websocket.send_json({"action": "ping"})
        pong_response = websocket.receive_json()
        assert pong_response["type"] == "pong"
