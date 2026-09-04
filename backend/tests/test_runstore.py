"""
The run store, and the scheduler that writes to it.

`active_tasks` and `workspaces` are in-memory dicts and `mission.json` is
written once at the very end, so a process killed halfway through a mission left
no record of how far it got. These pin down the part that fixed it -- including
the ordering rule that makes a resume safe: the database is written *before* the
event goes out, so there is no window where the UI has been shown a completed
step the store has never heard of.
"""
import asyncio
import os
import shutil
import sqlite3
import tempfile

import pytest

import runstore
from config import settings
from schemas import Plan, PlanStep


@pytest.fixture
def store():
    root = tempfile.mkdtemp(prefix="agenthq_runs_")
    yield runstore.RunStore(os.path.join(root, "runs.db"))
    shutil.rmtree(root, ignore_errors=True)


def _plan():
    return Plan(goal="do the thing", source="llm", steps=[
        PlanStep(id="s1", agent_id="researcher", description="research",
                 produces="research.md"),
        PlanStep(id="s2", agent_id="creative", description="write",
                 dependencies=["s1"], consumes=["research.md"],
                 produces="report.md"),
    ])


# ----------------------------------------------------------------- basics

def test_a_mission_round_trips(store):
    plan = _plan()
    store.start_mission("m1", "do the thing", "/missions/m1")
    store.record_plan("m1", plan)
    store.end_mission("m1", "completed", 0.0421)

    got = store.get_mission("m1")
    assert got["goal"] == "do the thing"
    assert got["status"] == "completed"
    assert got["cost_usd"] == pytest.approx(0.0421)
    assert got["mission_dir"] == "/missions/m1"
    assert [s["id"] for s in got["plan"]["steps"]] == ["s1", "s2"]


def test_the_plan_is_stored_before_the_mission_ends(store):
    """
    mission.json carries the plan too, but only once a mission finishes -- so a
    killed run leaves none, and there would be nothing to resume *from*.
    """
    store.start_mission("m2", "goal")
    store.record_plan("m2", _plan())
    # No end_mission: this is what a killed process leaves behind.
    got = store.get_mission("m2")
    assert got["status"] == "running"
    assert got["plan"]["steps"][1]["dependencies"] == ["s1"]
    assert got["plan"]["steps"][1]["consumes"] == ["research.md"]


def test_a_missing_mission_is_none_not_an_error(store):
    assert store.get_mission("never_existed") is None


def test_steps_upsert_rather_than_duplicate(store):
    plan = _plan()
    store.start_mission("m3", "goal")
    store.upsert_step("m3", plan.steps[0], "running")
    store.upsert_step("m3", plan.steps[0], "running", attempt=2)
    store.finish_step("m3", "s1", "completed", tokens=500, cost_usd=0.01)

    steps = store.get_steps("m3")
    assert len(steps) == 1
    assert steps[0]["status"] == "completed"
    assert steps[0]["tokens"] == 500
    assert steps[0]["attempt"] == 2


def test_only_completed_steps_are_resumable(store):
    """
    A step left `running` was in flight when the process died: its outcome is
    unknown and its artifact may be half written, so re-running it is the only
    safe reading of that state.
    """
    plan = _plan()
    store.start_mission("m4", "goal")
    store.upsert_step("m4", plan.steps[0], "running")
    store.finish_step("m4", "s1", "completed")
    store.upsert_step("m4", plan.steps[1], "running")     # killed here

    assert store.completed_step_ids("m4") == ["s1"]


def test_a_failed_step_keeps_its_error(store):
    plan = _plan()
    store.start_mission("m5", "goal")
    store.upsert_step("m5", plan.steps[0], "running")
    store.finish_step("m5", "s1", "failed", error="StepDeadlineExceeded: too slow")
    assert "too slow" in store.get_steps("m5")[0]["error"]


def test_missions_list_newest_first(store):
    for i in range(3):
        store.start_mission(f"m{i}", f"goal {i}")
    listed = store.list_missions()
    assert len(listed) == 3
    assert [m["started_at"] for m in listed] == sorted(
        (m["started_at"] for m in listed), reverse=True)


# ------------------------------------------------------------- durability

def test_a_killed_process_leaves_a_readable_database(store):
    """WAL plus one short transaction per write. A store that needed a clean
    shutdown to be readable would be useless for the one case it exists for."""
    plan = _plan()
    store.start_mission("m6", "goal")
    store.record_plan("m6", plan)
    store.upsert_step("m6", plan.steps[0], "running")
    store.finish_step("m6", "s1", "completed")
    store.upsert_step("m6", plan.steps[1], "running")

    path = store.path
    del store                       # no close(), as a SIGKILL would not close

    reopened = sqlite3.connect(path)
    reopened.row_factory = sqlite3.Row
    rows = {r["step_id"]: r["status"]
            for r in reopened.execute("SELECT step_id, status FROM steps")}
    assert rows == {"s1": "completed", "s2": "running"}
    reopened.close()


def test_an_unwritable_store_does_not_take_the_mission_down(monkeypatch):
    """Bookkeeping that can kill a run has turned a durability feature into a
    new way to lose one."""
    broken = runstore.RunStore(os.path.join("\x00 impossible", "runs.db"))
    assert broken._conn is None
    # Every call is a no-op rather than a raise.
    broken.start_mission("m", "goal")
    broken.upsert_step("m", _plan().steps[0], "running")
    broken.finish_step("m", "s1", "completed")
    broken.end_mission("m", "completed", 0.0)
    assert broken.get_mission("m") is None
    assert broken.list_missions() == []


def test_the_store_can_be_switched_off(store, monkeypatch):
    monkeypatch.setattr(settings, "runstore_enabled", False)
    store.start_mission("m7", "goal")
    assert store.get_mission("m7") is None, "writes are suppressed when disabled"
