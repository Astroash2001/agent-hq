"""
Two people sharing one deployment do not see each other's work.

One API token is handed to everyone, so before sessions every event went to
every socket, every mission was listed to every caller, and staged attachments
sat in one shared tray. Each browser now sends a session id, and everything a
mission produces is visible to that session only.
"""
import asyncio
import json
import os
import shutil
import tempfile

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import api_missions
import attachments
import runstore
import sessions
from config import settings
from event_bus import EventBus
from memory import AgentMemory
from schemas import Event, EventType

ALICE = "alice_" + "a" * 20
BOB = "bob_" + "b" * 20


@pytest.fixture
def client(monkeypatch):
    root = tempfile.mkdtemp(prefix="agenthq_sessions_")
    monkeypatch.setattr(settings, "missions_path", os.path.join(root, "missions"))
    os.makedirs(settings.missions_path)
    store = runstore.reset_store(os.path.join(root, "runs.db"))

    for mission_id, owner in (("m_alice", ALICE), ("m_bob", BOB), ("m_legacy", None)):
        directory = os.path.join(settings.missions_path, mission_id)
        os.makedirs(directory)
        with open(os.path.join(directory, "report.md"), "w", encoding="utf-8") as fh:
            fh.write(f"# {mission_id}\n")
        store.start_mission(mission_id, f"goal of {mission_id}", directory,
                            session_id=owner)

    # On disk only, no run-store row: ownership comes from mission.json.
    disk = os.path.join(settings.missions_path, "m_disk_bob")
    os.makedirs(disk)
    with open(os.path.join(disk, "mission.json"), "w", encoding="utf-8") as fh:
        json.dump({"goal": "bob's disk mission", "session_id": BOB}, fh)

    app = FastAPI()
    app.middleware("http")(sessions.middleware)
    app.include_router(api_missions.router)
    yield TestClient(app)
    runstore.reset_store(os.path.join(root, "closed.db")).close()
    shutil.rmtree(root, ignore_errors=True)


def _ids(client, session):
    headers = {sessions.HEADER: session} if session else {}
    body = client.get("/api/missions", headers=headers).json()
    return {m["id"] for m in body["missions"]}


def test_each_session_lists_only_its_own_missions(client):
    assert _ids(client, ALICE) == {"m_alice"}
    assert _ids(client, BOB) == {"m_bob", "m_disk_bob"}
    # No header: only what predates sessions.
    assert _ids(client, None) == {"m_legacy"}


def test_another_sessions_mission_is_not_found(client):
    """404, not 403: a different answer would confirm the id exists."""
    as_alice = {sessions.HEADER: ALICE}
    for path in ("/api/missions/m_bob", "/api/missions/m_bob/trace",
                 "/api/missions/m_bob/bundle",
                 "/api/missions/m_bob/artifacts/report.md",
                 "/api/missions/m_disk_bob"):
        assert client.get(path, headers=as_alice).status_code == 404, path
    assert client.post("/api/missions/m_bob/resume", headers=as_alice).status_code == 404
    assert client.get("/api/missions/m_alice", headers=as_alice).status_code == 200


def test_a_malformed_session_header_is_refused(client):
    """Dropped instead, it would fall into the None session and see legacy work."""
    r = client.get("/api/missions", headers={sessions.HEADER: "../x"})
    assert r.status_code == 400


def test_events_are_stamped_and_history_is_filtered():
    bus = EventBus()

    async def emit_as(session):
        sessions.set_current(session)
        await bus.emit(Event(type=EventType.TERMINAL_LOG, data={"who": session}))

    asyncio.run(emit_as(ALICE))
    asyncio.run(emit_as(BOB))
    asyncio.run(emit_as(None))

    seen = [e.data["who"] for e in bus.get_history(session_id=ALICE)]
    assert seen == [ALICE, None]


def test_a_mission_task_inherits_the_socket_session():
    """The whole design rests on asyncio.create_task copying the context."""
    bus = EventBus()

    async def socket_handler():
        sessions.set_current(ALICE)
        await asyncio.create_task(
            bus.emit(Event(type=EventType.TERMINAL_LOG, data={})))

    asyncio.run(socket_handler())
    assert bus.get_history(session_id=ALICE)[0].session_id == ALICE


def test_the_socket_broadcast_goes_to_the_owning_session_only():
    from main import ConnectionManager

    class FakeSocket:
        def __init__(self):
            self.sent = []

        async def send_json(self, payload):
            self.sent.append(payload)

    manager = ConnectionManager()
    alice, bob = FakeSocket(), FakeSocket()
    manager.active_connections = {alice: ALICE, bob: BOB}

    asyncio.run(manager.broadcast_event(
        Event(type=EventType.TERMINAL_LOG, session_id=ALICE)))
    asyncio.run(manager.broadcast_event(
        Event(type=EventType.SYSTEM_STATUS)))

    assert len(alice.sent) == 2
    assert len(bob.sent) == 1 and bob.sent[0]["session_id"] is None


def test_staged_attachments_are_per_session(monkeypatch):
    root = tempfile.mkdtemp(prefix="agenthq_attach_sessions_")
    monkeypatch.setattr(settings, "attachments_path", root)
    try:
        token = sessions.set_current(ALICE)
        attachments.save("notes.txt", b"alice's notes")
        sessions.set_current(BOB)
        assert attachments.listing() == []
        assert attachments.read("notes.txt") == ""
        assert attachments.discard("notes.txt") is False
        sessions.set_current(ALICE)
        assert attachments.read("notes.txt") == "alice's notes"
        sessions._current.reset(token)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_memory_recall_stays_within_the_session(monkeypatch):
    """A recalled subtask is pasted into the next prompt, so it must not cross users."""
    monkeypatch.setattr(settings, "memory_similarity_floor", 0.0)
    path = os.path.join(tempfile.mkdtemp(prefix="agenthq_mem_"), "memory.json")
    mem = AgentMemory(path)
    token = sessions.set_current(ALICE)
    try:
        mem.record("researcher", "R", "battery prices", "survey sodium-ion cost",
                   "success")
        sessions.set_current(BOB)
        assert mem.recall("researcher", "sodium-ion cost") == []
        sessions.set_current(ALICE)
        assert len(mem.recall("researcher", "sodium-ion cost")) == 1
    finally:
        sessions._current.reset(token)
        shutil.rmtree(os.path.dirname(path), ignore_errors=True)
