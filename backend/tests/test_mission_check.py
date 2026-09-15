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
                            report="Validated against ucl_edge_truth.json: 111,520 goals\n",
                            s1_tools=[("query_data", False)])
    spec = {"figures_in": "scouting_report.md",
            "_truth": {"total_goals": 111520, "total_rows": 30000},
            "forbidden_text": ["ucl_edge_truth"],
            "tools_succeeded": {"extraction.md": ["query_data"]},
            "tools_attempted": {"guardrails.md": ["fetch_url"]}}
    results = mc.check_mission(source, spec)
    by_rule = {r.rule: r for r in results}
    assert by_rule["figures"].level == mc.FAIL and "total_rows=30000" in by_rule["figures"].detail
    assert by_rule["forbidden_text"].level == mc.FAIL
    assert by_rule["tools_succeeded"].level == mc.FAIL
    assert by_rule["tools_attempted"].level == mc.PASS, "a refused attempt still counts as made"


def test_the_committed_spec_loads_with_its_answer_key():
    spec = mc.load_spec(os.path.join(mc.REPO, "evals", "fixtures", "ucl_edge_spec.json"))
    assert set(spec["required_figures"]) <= set(spec["_truth"])
