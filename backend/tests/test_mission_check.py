"""
evals/mission_check.py grades a mission from its files, not its report.

Built from two recorded runs whose reports both read as finished. These pin the
rules that caught them, on synthetic mission directories shaped like theirs.
"""
import json
import os
import shutil
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from evals import mission_check as mc  # noqa: E402


def _span(span_id, parent, kind, name, **attrs):
    return {"span_id": span_id, "parent_id": parent, "kind": kind, "name": name, "attrs": attrs}


def _write_mission(root, *, outcome="completed", extraction="# Extraction\nSELECT 1\n",
                   guardrails="# Guardrails\n", s1_verdict="accept", s1_tools=None,
                   unexecuted=None, report="Total goals: 111,520\n"):
    os.makedirs(root, exist_ok=True)
    manifest = {
        "outcome": outcome,
        "total_tokens": 1000, "token_budget": 400000,
        "ungrounded_figures": [], "unexecuted_sql": unexecuted or [],
        "steps": [
            {"id": "s1", "produces": "extraction.md", "needs_tools": ["query_data"]},
            {"id": "s2", "produces": "guardrails.md", "needs_tools": ["fetch_url"]},
            {"id": "s3", "produces": "scouting_report.md", "needs_tools": []},
        ],
    }
    tools = s1_tools if s1_tools is not None else [("query_data", True)]
    spans = [
        _span("m", None, "mission", "goal"),
        _span("st1", "m", "step", "s1:x", step_id="s1", verdict=s1_verdict, quality=0.9),
        _span("at1", "st1", "attempt", "s1 attempt 1"),
        _span("st2", "m", "step", "s2:y", step_id="s2", verdict="accept", quality=0.9),
        _span("t2", "st2", "tool", "fetch_url", success=False, args={"url": "x"},
              tool_error="refused"),
        _span("st3", "m", "step", "s3:z", step_id="s3", verdict="accept", quality=0.9),
    ]
    for i, (name, ok) in enumerate(tools):
        spans.append(_span(f"t1{i}", "at1", "tool", name, success=ok, args={"q": i}))

    files = {"mission.json": json.dumps(manifest), "extraction.md": extraction,
             "guardrails.md": guardrails, "scouting_report.md": report,
             "trace.jsonl": "\n".join(json.dumps(s) for s in spans)}
    for name, body in files.items():
        with open(os.path.join(root, name), "w", encoding="utf-8") as fh:
            fh.write(body)
    return mc.LocalSource(root)


@pytest.fixture
def scratch():
    path = tempfile.mkdtemp(prefix="agenthq_check_")
    yield path
    shutil.rmtree(path, ignore_errors=True)


def _levels(results):
    return {r.rule: r.level for r in results}


def test_the_rules_read_artifacts_carried_inside_the_report(scratch):
    """
    A finished mission keeps one file. Everything the rules check is still
    there, one level in: the report holds each artifact in full.
    """
    root = os.path.join(scratch, "embedded")
    source = _write_mission(root)
    report = ("# Briefing\n\n## ✅ The answer\n\nshort\n\n---\n"
              "## 📄 scouting_report.md\n\nTotal goals: 111,520\n\n---\n"
              "## 📎 extraction.md\n\n# Extraction\nSELECT 1\n\n---\n"
              "## 📎 guardrails.md\n\n# Guardrails\n\n---\n")
    for name in ("extraction.md", "guardrails.md", "scouting_report.md"):
        os.remove(os.path.join(root, name))
    with open(os.path.join(root, "report.md"), "w", encoding="utf-8") as fh:
        fh.write(report)

    carried = mc.embedded_artifacts(source)
    assert set(carried) == {"scouting_report.md", "extraction.md", "guardrails.md"}

    levels = _levels(mc.check_mission(source))
    assert levels["artifacts"] == mc.PASS, "promised artifacts are found in the report"
    assert levels["distinct"] == mc.PASS

    spec = {"figures_in": "scouting_report.md", "_truth": {"total_goals": 111520}}
    by_rule = {r.rule: r for r in mc.check_mission(source, spec)}
    assert by_rule["figures"].level == mc.PASS


class _Report:
    """A mission that is only its report -- enough for the totals rule."""

    def __init__(self, report):
        self._report = report

    def artifact(self, name):
        return self._report if name == "report.md" else ""


def test_an_answer_that_contradicts_its_own_budget_table_fails():
    """
    Observed: a report answering "the trip costs ₹5,320" over a
    `budget_table.md` whose own total was ₹9,100. Every rule passed --
    `grounding` asks whether a figure appears in some artifact, never whether
    the artifacts agree, and a reader takes the first number they see.
    """
    source = _Report("## ✅ The answer\n\nThe trip costs **₹5,320** for two "
                     "and fits the ₹20,000 budget.\n\n## 📎 next\n")
    result = mc._totals_agree(source, {"budget_table.md": "| **Total** | | **9,100** |"})

    assert result.level == mc.FAIL
    assert "5,320" in result.detail and "9,100" in result.detail


def test_one_file_contradicting_itself_fails():
    """
    Where the observed run actually did it: `budget_table.md` opened with
    "Result: ₹9,100 total" and closed with a table row totalling ₹5,320.
    Comparing file against file never looked inside either one.
    """
    body = ("**Result: ₹9,100 total — well under the ₹20,000 budget.**\n\n"
            "| Item | Detail | Cost |\n|---|---|---|\n"
            "| Bus | return for two | 2,320 |\n"
            "| **Total** | | **₹5,320** |\n")
    result = mc._totals_agree(_Report(""), {"budget_table.md": body})

    assert result.level == mc.FAIL
    assert "5,320" in result.detail and "9,100" in result.detail
    assert "20,000" not in result.detail, "the budget on the line is not a second total"


def test_two_files_stating_different_totals_are_a_note_not_a_failure():
    """
    One step disputing another's figure is the work, not a defect. A real run
    had a checking step reject a ₹3,880 headline and verify ₹2,000, and said so
    in its answer. Failing that would punish the mission for checking itself.
    """
    result = mc._totals_agree(_Report(""), {"a.md": "Total 5,000", "b.md": "Total 7,500"})
    assert result.level == mc.WARN
    assert "5,000" in result.detail and "7,500" in result.detail


def test_an_answer_quoting_the_files_total_passes():
    source = _Report("## ✅ The answer\n\nThe trip costs **₹18,420** for two.\n")
    assert mc._totals_agree(source, {"costs.md": "Grand total: ₹18,420"}).level == mc.PASS


def test_a_mission_that_priced_nothing_is_not_a_contradiction():
    """`Not obtained` on a total line states no figure, and a run that honestly
    priced nothing must not be failed for disagreeing with itself."""
    source = _Report("## ✅ The answer\n\nNo total could be determined.\n")
    result = mc._totals_agree(source, {"costs.md": "| Trip total | Not obtained |"})
    assert result.level == mc.PASS


def test_a_subtotal_beside_the_real_total_is_not_a_disagreement():
    """A file may carry a total per day and a total for the trip; the answer
    quoting the trip total is right, not inconsistent."""
    source = _Report("## ✅ The answer\n\nThe trip costs ₹5,000.\n")
    body = "Day 1 total 2,000\nDay 2 total 3,000\nTrip total 5,000\n"
    assert mc._totals_agree(source, {"costs.md": body}).level == mc.PASS


def test_a_clean_mission_passes_every_rule(scratch):
    levels = _levels(mc.check_mission(_write_mission(os.path.join(scratch, "ok"))))
    assert mc.FAIL not in levels.values(), levels


def test_the_copied_artifact_run_fails_on_the_rules_that_caught_it(scratch):
    source = _write_mission(os.path.join(scratch, "bad"), extraction="# Guardrails\n",
                            s1_verdict="revise", s1_tools=[])
    levels = _levels(mc.check_mission(source))
    assert levels["distinct"] == mc.FAIL
    assert levels["verdicts"] == mc.FAIL
    assert levels["tools_called"] == mc.FAIL


def test_an_unused_extra_tool_is_a_note_not_a_failure(scratch):
    """
    A step routed on query_data, read_file and list_files that did the whole
    job in SQL has done better work, not worse. Observed: a clean deployed run
    failed only because it never called read_file.
    """
    source = _write_mission(os.path.join(scratch, "extra"))
    manifest_path = os.path.join(source.directory, "mission.json")
    with open(manifest_path, encoding="utf-8") as fh:
        manifest = json.load(fh)
    manifest["steps"][0]["needs_tools"] = ["query_data", "read_file", "list_files"]
    with open(manifest_path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh)

    by_rule = {r.rule: r for r in mc.check_mission(source)}
    assert by_rule["tools_called"].level == mc.WARN
    assert "read_file" in by_rule["tools_called"].detail


def test_a_step_that_used_none_of_its_tools_fails(scratch):
    source = _write_mission(os.path.join(scratch, "none"), s1_tools=[])
    by_rule = {r.rule: r for r in mc.check_mission(source)}
    assert by_rule["tools_called"].level == mc.FAIL
    assert "called none of" in by_rule["tools_called"].detail


def test_a_harness_reconstructed_artifact_fails(scratch):
    source = _write_mission(os.path.join(scratch, "rebuilt"))
    trace_path = os.path.join(source.directory, "trace.jsonl")
    with open(trace_path, encoding="utf-8") as fh:
        spans = [json.loads(line) for line in fh if line.strip()]
    for span in spans:
        if span["kind"] == "step" and span["attrs"].get("step_id") == "s3":
            span["attrs"]["recovered"] = True
    with open(trace_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(json.dumps(s) for s in spans))

    by_rule = {r.rule: r for r in mc.check_mission(source)}
    assert by_rule["recovered"].level == mc.FAIL
    assert "s3" in by_rule["recovered"].detail


def test_unexecuted_sql_and_an_unverified_outcome_fail(scratch):
    # Recomputed from the trace: the report cites SQL no query_data call attempted.
    source = _write_mission(os.path.join(scratch, "sql"), outcome="unverified",
                            report="| Goals | 111,520 | `SELECT SUM(goals) FROM data` |\n")
    manifest_path = os.path.join(source.directory, "mission.json")
    with open(manifest_path, encoding="utf-8") as fh:
        manifest = json.load(fh)
    manifest["artifacts"] = ["extraction.md", "guardrails.md", "scouting_report.md"]
    with open(manifest_path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh)
    levels = _levels(mc.check_mission(source))
    assert levels["outcome"] == mc.FAIL
    assert levels["sql_provenance"] == mc.FAIL


def test_a_spec_checks_figures_forbidden_text_and_tool_success(scratch):
    source = _write_mission(os.path.join(scratch, "spec"),
                            report="Validated against scouting_answer_key.json: 111,520 goals\n",
                            s1_tools=[("query_data", False)])
    spec = {"figures_in": "scouting_report.md",
            "_truth": {"total_goals": 111520, "total_rows": 30000},
            "forbidden_text": ["scouting_answer_key"],
            "tools_succeeded": {"extraction.md": ["query_data"]},
            "tools_attempted": {"guardrails.md": ["fetch_url"]}}
    results = mc.check_mission(source, spec)
    by_rule = {r.rule: r for r in results}
    assert by_rule["figures"].level == mc.FAIL and "total_rows=30000" in by_rule["figures"].detail
    assert by_rule["forbidden_text"].level == mc.FAIL
    assert by_rule["tools_succeeded"].level == mc.FAIL
    assert by_rule["tools_attempted"].level == mc.PASS, "a refused attempt still counts as made"


def test_the_committed_spec_loads_with_its_answer_key():
    spec = mc.load_spec(os.path.join(mc.REPO, "evals", "fixtures", "scouting_rules.json"))
    assert set(spec["required_figures"]) <= set(spec["_truth"])
