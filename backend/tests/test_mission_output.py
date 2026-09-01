"""
Mission output on disk.

The workspace used to be purely in memory, so the only copy of a finished
mission lived in the open browser tab. These pin the mirror down -- including
the part where artifact names come from a model and reach the filesystem
untrusted.
"""
import json
import os
import shutil
import tempfile

import pytest

from config import settings
from workspace import MissionWorkspace, _safe_name


@pytest.fixture
def missions(monkeypatch):
    """
    A scratch mission root.

    tempfile rather than pytest's tmp_path: on this machine pytest cannot
    create its basetemp under AppData\Local\Temp (WinError 5), and these
    tests should not depend on that working.
    """
    root = tempfile.mkdtemp(prefix="agenthq_missions_")
    monkeypatch.setattr(settings, "missions_path", os.path.join(root, "missions"))
    monkeypatch.setattr(settings, "persist_missions", True)
    yield root
    shutil.rmtree(root, ignore_errors=True)


@pytest.mark.asyncio
async def test_artifact_is_written_as_it_is_filed(missions):
    w = MissionWorkspace("m1")
    await w.put("research.md", "# Findings", "alice", "Alice", "s1")
    assert os.path.isfile(os.path.join(w.dir, "research.md"))
    with open(os.path.join(w.dir, "research.md"), encoding="utf-8") as fh:
        assert fh.read() == "# Findings"


@pytest.mark.asyncio
async def test_report_and_manifest(missions):
    w = MissionWorkspace("m2")
    await w.put("a.md", "body", "alice", "Alice", "s1")
    w.save_report("# Final")
    w.save_manifest({"goal": "do the thing", "steps": [{"id": "s1"}]})

    with open(os.path.join(w.dir, "report.md"), encoding="utf-8") as fh:
        assert fh.read() == "# Final"
    with open(os.path.join(w.dir, "mission.json"), encoding="utf-8") as fh:
        assert json.load(fh)["goal"] == "do the thing"


@pytest.mark.parametrize("name,expected", [
    ("../../etc/passwd", "passwd"),
    ("/abs/path.md", "path.md"),
    ("..", "untitled.md"),
    ("", "untitled.md"),
    ("nor mal name.md", "nor_mal_name.md"),
    ("ok-file_1.md", "ok-file_1.md"),
])
def test_names_cannot_escape_the_mission_directory(name, expected):
    assert _safe_name(name) == expected


@pytest.mark.asyncio
async def test_hostile_name_stays_inside(missions):
    w = MissionWorkspace("m3")
    await w.put("../../escape.md", "nope", "bob", "Bob", "s1")
    assert os.listdir(w.dir) == ["escape.md"]
    assert not os.path.exists(os.path.join(w.dir, "..", "..", "escape.md"))


@pytest.mark.asyncio
async def test_mission_survives_an_unwritable_directory(missions, monkeypatch):
    """Losing the mirror must not lose the run."""
    monkeypatch.setattr(settings, "missions_path", "\x00 impossible")
    w = MissionWorkspace("m4")
    assert w.dir is None
    art = await w.put("a.md", "still works", "alice", "Alice", "s1")
    assert art.content == "still works"
    assert w.get("a.md") is not None
    assert w.save_report("x") is None


@pytest.mark.asyncio
async def test_persistence_can_be_switched_off(missions, monkeypatch):
    monkeypatch.setattr(settings, "persist_missions", False)
    target = os.path.join(missions, "off")
    monkeypatch.setattr(settings, "missions_path", target)
    w = MissionWorkspace("m5")
    assert w.dir is None
    await w.put("a.md", "x", "alice", "Alice", "s1")
    assert not os.path.exists(target)


@pytest.mark.asyncio
async def test_summary_carries_bodies_and_flags_truncation(missions):
    w = MissionWorkspace("m6")
    await w.put("small.md", "hi", "alice", "Alice", "s1")
    await w.put("big.md", "x" * (w.MAX_BODY_CHARS + 500), "bob", "Bob", "s2")
    by_name = {a["name"]: a for a in w.summary()}
    assert by_name["small.md"]["content"] == "hi"
    assert by_name["small.md"]["truncated"] is False
    assert by_name["big.md"]["truncated"] is True
    assert len(by_name["big.md"]["content"]) == w.MAX_BODY_CHARS
