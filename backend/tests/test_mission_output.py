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
    create its basetemp under the system temp dir (WinError 5), and these
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


# --- the artifact a step files ------------------------------------------
# A real GPT-5.5 run filed "Task completed after 4 execution steps." (39 chars)
# as solar_efficiency_research.md, while the 2.4KB report the agent actually
# wrote went to a loose path in backend/. The next step then consumed the
# sentence. These pin down which of the two becomes the artifact.

from orchestrator import Orchestrator
from schemas import Plan, PlanStep
from tools import file_ops


@pytest.mark.asyncio
async def test_the_briefing_leads_with_the_answer_and_folds_the_rest(missions, monkeypatch):
    """
    A finished mission read as a log: the graph, the handoffs and the run
    record sat at the same level as the finding. A reader wants what was asked,
    what the answer is, and whether to trust it -- then the evidence.
    """
    import synthesis

    orch = Orchestrator()
    workspace = MissionWorkspace("m_answer")
    await workspace.put("dream_xi.md", "# XI\n" + ("detail " * 50), "creative", "Eve", "s1")

    plan = Plan(goal="Pick the best XI", source="llm", steps=[
        PlanStep(id="s1", agent_id="creative", description="Pick the XI",
                 produces="dream_xi.md")])
    plan.steps[0].status = "completed"

    # The model writes the answer; a test must not call one. This stands in for
    # whatever it would say, so the test checks placement and nothing else.
    stub_answer = "STUB ANSWER SENTENCE\n\n| Column | Value |\n|---|---|\n| a | b |"

    async def conclusion(*args, **kwargs):
        return synthesis.Conclusion(text=stub_answer, skipped=False)

    monkeypatch.setattr(synthesis, "conclude", conclusion)
    orch._provenance_checkable = False

    text = await orch._synthesize_results(
        "Pick the best XI", [{"step_id": "s1", "agent_name": "Eve", "subtask": "Pick",
                              "result": "done", "consumes": [], "tools_used": []}],
        12.0, plan, workspace)

    answer_at = text.index("## ✅ The answer")
    details_at = text.index("<details>")
    assert text.index("**You asked:**") < answer_at < details_at, "answer before the audit trail"
    assert stub_answer.splitlines()[0] in text[answer_at:details_at], \
        "whatever the model wrote is the answer section"
    assert "✅ **Verified.**" in text[answer_at:details_at], "trust stated with the answer"
    assert "`dream_xi.md`" in text[answer_at:details_at], "files named with the answer"
    assert "Execution Graph" in text[details_at:], "the graph moves inside the fold"
    assert text.rstrip().endswith("</details>"), "the fold is closed"


@pytest.mark.asyncio
async def test_write_file_files_into_the_workspace(missions):
    w = MissionWorkspace("m7")
    file_ops.bind_workspace(w, "creative", "Eve", "s1")
    try:
        res = await file_ops.write_file("report.md", "# Real content\n" * 50)
    finally:
        file_ops.bind_workspace(None)

    assert res["success"] is True
    art = w.get("report.md")
    assert art is not None and art.author_name == "Eve"
    assert os.path.isfile(os.path.join(w.dir, "report.md"))


@pytest.mark.asyncio
async def test_write_file_without_a_mission_still_writes_to_disk(missions):
    """A bare agent, or a test, must keep the old behaviour."""
    file_ops.bind_workspace(None)
    target = os.path.join(missions, "loose.md")
    res = await file_ops.write_file(target, "hello")
    assert res["success"] is True and os.path.isfile(target)


@pytest.mark.asyncio
async def test_written_file_beats_the_closing_message(missions):
    w = MissionWorkspace("m8")
    before = set(w.names())
    step = PlanStep(id="s1", agent_id="researcher", description="research",
                    produces="research.md")

    # The agent wrote its work under a name of its own choosing.
    await w.put("solar_notes.md", "# Findings\n" + ("detail " * 200),
                "researcher", "Alice", "s1")

    best = Orchestrator._best_artifact(w, step, before)
    assert best is not None
    assert best.name == "solar_notes.md"
    assert best.size_chars > 100


@pytest.mark.asyncio
async def test_exact_name_wins_over_a_larger_draft(missions):
    w = MissionWorkspace("m9")
    before = set(w.names())
    step = PlanStep(id="s1", agent_id="researcher", description="r",
                    produces="research.md")
    await w.put("scratch.md", "x" * 5000, "researcher", "Alice", "s1")
    await w.put("research.md", "the real thing", "researcher", "Alice", "s1")
    assert Orchestrator._best_artifact(w, step, before).name == "research.md"


@pytest.mark.asyncio
async def test_nothing_written_falls_back_to_the_message(missions):
    w = MissionWorkspace("m10")
    before = set(w.names())
    step = PlanStep(id="s1", agent_id="researcher", description="r",
                    produces="research.md")
    assert Orchestrator._best_artifact(w, step, before) is None


@pytest.mark.asyncio
async def test_pre_existing_artifacts_are_not_mistaken_for_this_step(missions):
    """Only files new since the step started count as its output."""
    w = MissionWorkspace("m11")
    await w.put("upstream.md", "y" * 9000, "researcher", "Alice", "s0")
    before = set(w.names())
    step = PlanStep(id="s2", agent_id="creative", description="w", produces="out.md")
    assert Orchestrator._best_artifact(w, step, before) is None


@pytest.mark.asyncio
async def test_a_meeting_chair_writes_into_the_workspace_too(missions, monkeypatch):
    """
    _run_meeting bound the meeting tools but not file_ops, so a chair holding
    write_file wrote past the workspace and left an orphan copy in backend/.
    Observed: sodium_ion_findings.md existed both in the mission directory and
    loose on disk.
    """
    from tools import file_ops

    w = MissionWorkspace("m12")
    file_ops.bind_workspace(w, "creative", "Eve", "s2")
    try:
        await file_ops.write_file("findings.md", "chair's notes")
    finally:
        file_ops.bind_workspace(None)

    assert w.get("findings.md") is not None, "the chair's file missed the workspace"
    assert os.path.isfile(os.path.join(w.dir, "findings.md"))
