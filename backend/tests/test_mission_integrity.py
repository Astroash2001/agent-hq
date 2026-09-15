"""
The four holes two recorded missions fell through, pinned.

1. Isolation. A step could read `.env`, the eval answer key, the run store and
   every earlier mission's files. Observed: a guardrail step copied a previous
   run's "verbatim" refusals without making one probe call.
2. Attribution. The file_ops binding was a module global, so parallel steps
   overwrote each other's -- and filing credited a step with a sibling's file.
   Observed: `extraction.md` was a byte-for-byte copy of `guardrails.md`.
3. Status. A mission whose extraction step scored 0.05 was recorded `completed`.
4. Provenance. A report printed SQL no tool ever ran, beside a figure.
"""
import asyncio
import os
import shutil
import tempfile

import pytest

import provenance
from base_agent import _trace_args
from config import settings
from orchestrator import Orchestrator
from schemas import Plan, PlanStep
from tools import file_ops, meeting
from tools.query_data import query_data
from tools.search_workspace import search_workspace
from workspace import MissionWorkspace


@pytest.fixture
def jail(monkeypatch):
    """A repository-shaped jail with every kind of forbidden file in it."""
    root = tempfile.mkdtemp(prefix="agenthq_integrity_")
    data = os.path.join(root, "backend", "data")
    missions = os.path.join(data, "missions")
    os.makedirs(os.path.join(missions, "m_other"))
    os.makedirs(os.path.join(root, "evals", "fixtures"))

    files = {
        ".env": "OPENAI_API_KEY=sk-not-a-real-key\n",
        os.path.join("backend", ".env"): "OPENAI_API_KEY=sk-not-a-real-key\n",
        os.path.join("evals", "fixtures", "truth.json"): '{"total_goals": 111520}\n',
        os.path.join("backend", "data", "players.csv"): "player,goals\nA,3\nB,5\n",
        os.path.join("backend", "data", "memory.json"): "{}",
        os.path.join("backend", "data", "missions", "m_other", "report.md"):
            "Total goals: 111520 (copied)\n",
    }
    for rel, body in files.items():
        with open(os.path.join(root, rel), "w", encoding="utf-8") as fh:
            fh.write(body)

    monkeypatch.setattr(settings, "jail_root", root)
    monkeypatch.setattr(settings, "missions_path", missions)
    monkeypatch.setattr(settings, "memory_path", os.path.join(data, "memory.json"))
    monkeypatch.setattr(settings, "runstore_path", os.path.join(data, "runs.db"))
    monkeypatch.setattr(settings, "embedding_cache_path", os.path.join(data, "embeddings.json"))
    monkeypatch.setattr(settings, "persist_missions", True)
    monkeypatch.setattr(settings, "file_ops_sandbox_enabled", True)
    file_ops.bind_workspace(None)
    yield root
    file_ops.bind_workspace(None)
    shutil.rmtree(root, ignore_errors=True)


# ----------------------------------------------------------------- 1. isolation

@pytest.mark.asyncio
async def test_secrets_are_refused_by_every_reading_tool(jail):
    env = os.path.join(jail, "backend", ".env")
    read = await file_ops.read_file(env)
    assert read["success"] is False and read["refused"] is True
    assert "secrets" in read["error"]

    queried = await query_data(env, "SELECT * FROM data")
    assert queried.get("refused") is True

    found = await search_workspace("OPENAI_API_KEY", directory=jail)
    assert found["success"] is True
    assert found["match_count"] == 0, "the key line must not be quoted back"

    listed = await file_ops.list_files(jail)
    assert ".env" not in [i["name"] for i in listed["items"]]


@pytest.mark.asyncio
async def test_the_answer_key_and_runtime_state_are_refused(jail):
    truth = await file_ops.read_file(os.path.join(jail, "evals", "fixtures", "truth.json"))
    assert truth.get("refused") is True

    state = await file_ops.read_file(os.path.join(jail, "backend", "data", "memory.json"))
    assert state.get("refused") is True and "runtime state" in state["error"]

    # Next to them, ordinary input data is still readable.
    data = await file_ops.read_file(os.path.join(jail, "backend", "data", "players.csv"))
    assert data["success"] is True


@pytest.mark.asyncio
async def test_a_running_mission_cannot_read_another_missions_files(jail):
    workspace = MissionWorkspace("m_own")
    await workspace.put("mine.md", "# Mine\n", "a", "A", "s1")
    other = os.path.join(jail, "backend", "data", "missions", "m_other", "report.md")

    file_ops.bind_workspace(workspace, "a", "A", "s1")
    try:
        refused = await file_ops.read_file(other)
        own = await file_ops.read_file("mine.md")
        found = await search_workspace("111520", directory=os.path.join(jail, "backend"))
    finally:
        file_ops.bind_workspace(None)

    assert refused.get("refused") is True and "different mission" in refused["error"]
    assert own["success"] is True
    assert found["match_count"] == 0, "a search must not walk into another mission"


# ----------------------------------------------------------------- 2. attribution

@pytest.mark.asyncio
async def test_parallel_steps_keep_their_own_binding(jail):
    """
    Two steps in parallel, the first finishing (and unbinding) while the second
    is still working. As a module global, the second step's write lost its
    workspace and its author; as a ContextVar each task keeps its own.
    """
    workspace = MissionWorkspace("m_parallel")
    first_done = asyncio.Event()

    async def step(step_id, author, wait_for_other):
        file_ops.bind_workspace(workspace, author, author.title(), step_id)
        try:
            if wait_for_other:
                await first_done.wait()
            await file_ops.write_file(f"{step_id}.md", f"# by {author}\n")
        finally:
            file_ops.bind_workspace(None)
            if not wait_for_other:
                first_done.set()

    await asyncio.gather(step("s2", "mara", True), step("s1", "maya", False))

    assert workspace.get("s1.md").step_id == "s1"
    assert workspace.get("s1.md").author_id == "maya"
    assert workspace.get("s2.md") is not None, "the late write escaped the workspace"
    assert workspace.get("s2.md").step_id == "s2"
    assert workspace.get("s2.md").author_id == "mara"


@pytest.mark.asyncio
async def test_filing_never_credits_a_step_with_a_siblings_file(jail):
    workspace = MissionWorkspace("m_sibling")
    before = set(workspace.names())
    extraction = PlanStep(id="s1", agent_id="x", description="extract",
                          produces="extraction.md")
    # The guardrail step, running alongside, files its work mid-attempt.
    await workspace.put("guardrails.md", "# Probes\n" * 40, "y", "Mara", "s2")

    assert Orchestrator._best_artifact(workspace, extraction, before) is None


@pytest.mark.asyncio
async def test_a_recorded_decision_is_tagged_with_its_meeting_step(jail):
    workspace = MissionWorkspace("m_meeting")
    meeting.bind_workspace(workspace, "s3")
    try:
        await meeting.record_decision("We agreed.", "decision.md")
    finally:
        meeting.bind_workspace(None)
    assert workspace.get("decision.md").step_id == "s3"


@pytest.mark.asyncio
async def test_a_step_cannot_write_another_steps_deliverable(jail):
    """
    Observed: the guardrail step also wrote `extraction.md`, overwriting the
    extraction step's file while both were running.
    """
    workspace = MissionWorkspace("m_owners")
    workspace.reserve({"extraction.md": "s1", "guardrails.md": "s2"})

    file_ops.bind_workspace(workspace, "omar", "Omar", "s2")
    try:
        stolen = await file_ops.write_file("extraction.md", "# not mine")
        own = await file_ops.write_file("guardrails.md", "# mine")
        scratch = await file_ops.write_file("notes.md", "# unpromised, allowed")
    finally:
        file_ops.bind_workspace(None)

    assert stolen.get("refused") is True and "step s1" in stolen["error"]
    assert workspace.get("extraction.md") is None
    assert own["success"] is True and scratch["success"] is True

    meeting.bind_workspace(workspace, "s3")
    try:
        clash = await meeting.record_decision("agreed", "extraction.md")
    finally:
        meeting.bind_workspace(None)
    assert clash.get("refused") is True


# ----------------------------------------------------------------- 3. status

def _plan(n=2):
    steps = [PlanStep(id=f"s{i}", agent_id="researcher", description=f"step {i}",
                      produces=f"s{i}.md") for i in range(1, n + 1)]
    for s in steps:
        s.status = "completed"
    return Plan(goal="g", source="llm", steps=steps)


def test_a_mission_with_a_rejected_step_is_not_completed():
    orch = Orchestrator()
    plan = _plan()
    orch._step_telemetry["m_rej"] = {"s1": {"verdict": "accept"},
                                     "s2": {"verdict": "revise"}}
    assert orch._mission_outcome("m_rej", plan) == "unverified"
    assert [s.id for s in orch._unverified_steps("m_rej", plan)] == ["s2"]

    orch._step_telemetry["m_ok"] = {"s1": {"verdict": "accept"}, "s2": {}}
    assert orch._mission_outcome("m_ok", plan) == "completed"

    plan.steps[0].status = "failed"
    assert orch._mission_outcome("m_rej", plan) == "failed", "a failure outranks it"


# ----------------------------------------------------------------- 4. provenance

REPORT = """
## Totals
```sql
SELECT COUNT(*) AS row_count, SUM(goals) FROM data
```
| Figure | SQL |
|---|---|
| Forwards | `SELECT COUNT(*) FROM data WHERE position = 'FW'` |

```sql
SELECT player, ROUND(goals * 90.0 / minutes, 4) FROM data;
```
"""


def test_sql_is_matched_on_meaning_not_layout():
    executed = ["select count( * ) as row_count,\n  sum(goals)\nfrom data;",
                "SELECT COUNT(*) FROM data WHERE position='FW'"]
    flagged = provenance.unexecuted_sql([("scouting_report.md", REPORT)], executed)
    assert flagged == ["scouting_report.md: SELECT player, ROUND(goals * 90.0 / minutes, 4) FROM data"]


def test_a_report_with_no_executed_queries_flags_every_query():
    flagged = provenance.unexecuted_sql([("r.md", REPORT)], [])
    assert len(flagged) == 3


@pytest.mark.asyncio
async def test_query_data_records_what_it_ran_on_the_bound_mission(jail):
    workspace = MissionWorkspace("m_sql")
    csv_path = os.path.join(jail, "backend", "data", "players.csv")
    file_ops.bind_workspace(workspace, "a", "A", "s1")
    try:
        ok = await query_data(csv_path, "SELECT SUM(goals) FROM data")
        bad = await query_data(csv_path, "SELECT nope FROM data")
    finally:
        file_ops.bind_workspace(None)
    assert ok["success"] is True and bad["success"] is False
    # Both reached the engine, so both were attempted.
    assert workspace.executed_queries == ["SELECT SUM(goals) FROM data", "SELECT nope FROM data"]


@pytest.mark.asyncio
async def test_a_refused_query_counts_as_attempted(jail):
    """A guardrail report quoting the DROP it was refused is quoting a real call."""
    workspace = MissionWorkspace("m_refused_sql")
    csv_path = os.path.join(jail, "backend", "data", "players.csv")
    file_ops.bind_workspace(workspace, "a", "A", "s2")
    try:
        refused = await query_data(csv_path, "DROP TABLE data")
    finally:
        file_ops.bind_workspace(None)
    assert refused.get("refused") is True
    assert "DROP TABLE data" in workspace.executed_queries


@pytest.mark.asyncio
async def test_a_capped_result_says_not_to_repeat_the_query(jail, monkeypatch):
    monkeypatch.setattr(settings, "query_data_max_rows", 1)
    csv_path = os.path.join(jail, "backend", "data", "players.csv")
    result = await query_data(csv_path, "SELECT * FROM data", max_rows=30000)
    assert result["truncated"] is True
    assert "capped at 1" in result["hint"]
    assert "Do not repeat this query" in result["hint"]


def test_words_and_fragments_in_backticks_are_not_sql_claims():
    text = "| Probe | run `with` care | `SELECT DISTINCT squad_no` | `SELECT COUNT(*) FROM data` |"
    flagged = provenance.unexecuted_sql([("r.md", text)], [])
    assert flagged == ["r.md: SELECT COUNT(*) FROM data"]


def test_sql_suggested_in_prose_is_not_a_claim_that_it_ran():
    """
    Observed in a clean run: a dissent reading "Bob would have shown
    `SELECT squad_no FROM data LIMIT 2`" was flagged as SQL nobody ran.
    """
    prose = "- **Bob** would have verified it with `SELECT squad_no FROM data LIMIT 2`."
    assert provenance.unexecuted_sql([("decision.md", prose)], []) == []


def test_a_summary_row_citing_one_column_of_a_query_that_ran_passes():
    """
    Observed: `SELECT SUM(goals) FROM data` in a summary table, beside a figure
    from `SELECT COUNT(*), SUM(goals), SUM(minutes) FROM data`, was flagged.
    """
    ran = ["SELECT COUNT(*) AS row_count, SUM(goals) AS total_goals, SUM(minutes) FROM data"]
    table = ("| Total goals | 111,520 | `SELECT SUM(goals) FROM data` |\n"
             "| Rows | 30,000 | `SELECT COUNT(*) FROM data` |\n"
             "| Liverpool | 16,508 | `SELECT SUM(goals) FROM data WHERE club = 'Liverpool'` |")
    flagged = provenance.unexecuted_sql([("report.md", table)], ran)
    assert flagged == ["report.md: SELECT SUM(goals) FROM data WHERE club = 'Liverpool'"], \
        "a different WHERE is a different query"


@pytest.mark.asyncio
async def test_a_query_that_failed_as_sql_counts_as_attempted(jail):
    """An extraction showing its failed first attempt is reporting honestly."""
    workspace = MissionWorkspace("m_failed_sql")
    csv_path = os.path.join(jail, "backend", "data", "players.csv")
    file_ops.bind_workspace(workspace, "a", "A", "s1")
    try:
        failed = await query_data(csv_path, "SELECT name FROM data")
        missing = await query_data(os.path.join(jail, "nope.csv"), "SELECT 1 FROM data")
    finally:
        file_ops.bind_workspace(None)
    assert failed["success"] is False and missing["success"] is False
    assert workspace.executed_queries == ["SELECT name FROM data"]


# ----------------------------------------------------------------- trace args

def test_trace_args_keep_choices_and_drop_bodies():
    args = _trace_args({"filepath": "extraction.md", "content": "x" * 5000,
                        "query": "SELECT " + "a" * 400, "max_rows": 5})
    assert args["filepath"] == "extraction.md"
    assert args["content"] == "<5000 chars>"
    assert len(args["query"]) <= 303 and args["query"].endswith("...")
    assert args["max_rows"] == 5
