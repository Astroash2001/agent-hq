"""
The API a deployment exposes, locked down (api_guard.py).

Each test is a hole that was open on a hosted server:
- only the kill switch and the socket checked the token, and only when set;
- `%2e%2e` as a mission id served `backend/data/memory.json`;
- production started with no token at all;
- a second connection could start a second mission, on the owner's keys;
- an upload was read whole into memory before its size was checked.
"""
import asyncio

import pytest
from fastapi.testclient import TestClient

import api_guard
from config import settings
from main import app

TOKEN = "guard-test-token"


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def locked(monkeypatch):
    monkeypatch.setattr(settings, "api_token", TOKEN)


AUTH = {"Authorization": f"Bearer {TOKEN}"}

# Every route that reads or changes state. Health is the one exception.
PROTECTED = [
    ("get", "/api/agents"), ("get", "/api/history"), ("get", "/api/office"),
    ("get", "/api/plan"), ("get", "/api/memory"), ("post", "/api/memory/reset"),
    ("get", "/api/attachments"), ("delete", "/api/attachments/x.txt"),
    ("get", "/api/missions"), ("get", "/api/missions/task_x"),
    ("get", "/api/missions/task_x/trace"), ("get", "/api/missions/task_x/artifacts/a.md"),
    ("post", "/api/missions/task_x/resume"), ("post", "/api/emergency-stop"),
]


@pytest.mark.parametrize("method,path", PROTECTED)
def test_every_api_route_needs_the_token(client, locked, method, path):
    response = getattr(client, method)(path)
    assert response.status_code == 401, path


def test_a_wrong_token_and_a_query_string_token_are_both_refused(client, locked):
    assert client.get("/api/agents", headers={"Authorization": "Bearer nope"}).status_code == 401
    # REST takes headers only: a token in the URL ends up in access logs.
    assert client.get(f"/api/agents?token={TOKEN}").status_code == 401


def test_the_right_token_passes_and_health_needs_none(client, locked):
    assert client.get("/api/agents", headers=AUTH).status_code == 200
    assert client.get("/api/agents", headers={"X-API-Token": TOKEN}).status_code == 200
    assert client.get("/api/health").status_code == 200


def test_the_frontend_itself_is_public(client, locked):
    """The page has to load so it can ask for the token."""
    assert client.get("/").status_code == 200


def test_no_token_configured_leaves_a_local_server_open(client, monkeypatch):
    monkeypatch.setattr(settings, "api_token", "")
    assert client.get("/api/agents").status_code == 200


@pytest.mark.parametrize("mission_id", ["%2e%2e", "..", "a%2f..", "%2e", "task_x%00"])
def test_a_mission_id_cannot_climb_out_of_the_missions_directory(client, mission_id):
    """
    `%2e%2e` decodes to `..`, and the artifact route used to serve
    backend/data/memory.json through it.
    """
    for suffix in ("", "/trace", "/artifacts/memory.json"):
        response = client.get(f"/api/missions/{mission_id}{suffix}")
        assert response.status_code in (400, 404), (mission_id, suffix, response.status_code)
        assert b"episodes" not in response.content


def test_production_refuses_to_start_without_a_token(monkeypatch):
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "api_token", "")
    monkeypatch.setattr(settings, "allow_unauthenticated", False)
    with pytest.raises(RuntimeError, match="AGENTHQ_API_TOKEN"):
        api_guard.check_startup()

    monkeypatch.setattr(settings, "allow_unauthenticated", True)
    api_guard.check_startup()          # explicit opt-out: warns, starts

    monkeypatch.setattr(settings, "allow_unauthenticated", False)
    monkeypatch.setattr(settings, "api_token", TOKEN)
    api_guard.check_startup()          # a token: starts


def test_production_hides_the_interactive_docs(monkeypatch):
    """Checked on a fresh app: `docs_url` is fixed when the app is built."""
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "api_token", TOKEN)
    import importlib
    import main as main_mod
    rebuilt = importlib.reload(main_mod)
    try:
        prod = TestClient(rebuilt.app)
        assert prod.get("/docs").status_code != 200 or b"swagger" not in prod.get("/docs").content.lower()
        assert prod.get("/openapi.json", headers=AUTH).status_code == 404
    finally:
        monkeypatch.setattr(settings, "environment", "development")
        importlib.reload(main_mod)


@pytest.mark.asyncio
async def test_only_one_mission_runs_at_a_time(monkeypatch):
    monkeypatch.setattr(settings, "max_concurrent_missions", 1)
    slots = api_guard.MissionSlots()
    release = asyncio.Event()

    async def mission():
        await release.wait()

    first = slots.start(mission())
    assert first is not None and slots.running == 1
    assert slots.start(mission()) is None, "a second mission must be refused"

    release.set()
    await first
    assert slots.running == 0
    second = slots.start(mission())
    assert second is not None, "the slot frees when the first mission ends"
    await second


@pytest.mark.asyncio
async def test_a_slot_is_released_when_the_mission_raises(monkeypatch):
    monkeypatch.setattr(settings, "max_concurrent_missions", 1)
    slots = api_guard.MissionSlots()

    async def boom():
        raise RuntimeError("mission crashed")

    task = slots.start(boom())
    with pytest.raises(RuntimeError):
        await task
    assert slots.running == 0


def test_an_oversized_upload_is_refused(client, monkeypatch):
    monkeypatch.setattr(settings, "api_token", "")
    monkeypatch.setattr(settings, "max_attachment_bytes", 100)
    response = client.post("/api/attachments",
                           files={"file": ("big.txt", b"x" * 5000, "text/plain")})
    assert response.status_code == 400
    assert "limit" in response.json()["detail"]
