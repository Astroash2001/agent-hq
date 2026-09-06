"""
Serving one artifact's text.

`get_mission` lists artifact filenames and stops there, so a replayed mission
could show the work happening and not what the work said. The trace holds spans,
not bodies, and putting bodies in it would bloat the one file that has to stay
readable when a process is killed mid-write.

The interesting half is containment. The name reaches the filesystem, so it is
resolved and then checked to be inside the mission directory -- not
pattern-matched against a list of bad shapes, which is a losing game.
"""
import os
import shutil
import tempfile

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import api_missions
from config import settings


@pytest.fixture
def client(monkeypatch):
    root = tempfile.mkdtemp(prefix="agenthq_artifacts_")
    monkeypatch.setattr(settings, "missions_path", root)

    mission = os.path.join(root, "m_1")
    os.makedirs(mission)
    with open(os.path.join(mission, "report.md"), "w", encoding="utf-8") as fh:
        fh.write("# Briefing\n\nSodium-ion at $61/kWh.\n")
    # A file OUTSIDE the mission, which is what traversal would be reaching for.
    with open(os.path.join(root, "secret.txt"), "w", encoding="utf-8") as fh:
        fh.write("not yours")

    app = FastAPI()
    app.include_router(api_missions.router)
    yield TestClient(app)
    shutil.rmtree(root, ignore_errors=True)


def test_an_artifact_comes_back_as_its_own_text(client):
    r = client.get("/api/missions/m_1/artifacts/report.md")
    assert r.status_code == 200
    assert "Sodium-ion at $61/kWh." in r.text


def test_a_missing_artifact_is_a_404_not_an_empty_body(client):
    """An empty 200 reads as "the agent wrote nothing", which is a different
    and much worse claim than "there is no such file"."""
    r = client.get("/api/missions/m_1/artifacts/nope.md")
    assert r.status_code == 404


def test_traversal_out_of_the_mission_is_refused(client):
    """
    The name is URL-decoded before it reaches the handler, so `..%2f` arrives as
    `../`. Resolving the path and comparing it to the mission directory catches
    that; a blocklist of shapes would not.
    """
    for name in ("../secret.txt", "..%2Fsecret.txt", r"..\secret.txt",
                 "sub/../../secret.txt"):
        r = client.get(f"/api/missions/m_1/artifacts/{name}")
        assert r.status_code in (400, 404), f"{name!r} returned {r.status_code}"
        assert "not yours" not in r.text, f"{name!r} leaked a file outside the mission"


def test_an_absolute_path_is_refused(client):
    r = client.get("/api/missions/m_1/artifacts//etc/passwd")
    assert r.status_code in (400, 404)
    assert "root:" not in r.text


def test_the_mission_directory_itself_is_not_an_artifact(client):
    """An empty name resolves to the directory, which is not a file and must
    not be treated as one."""
    r = client.get("/api/missions/m_1/artifacts/.")
    assert r.status_code in (400, 404)


def test_an_unknown_mission_is_a_404(client):
    r = client.get("/api/missions/nope/artifacts/report.md")
    assert r.status_code == 404
