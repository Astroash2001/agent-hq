"""
Grade a finished mission from what it left behind, the same way every time.

Reading a report to decide whether a run went well found a different problem
each run, because a report is written to look finished. This reads the evidence
underneath it instead -- `mission.json`, `trace.jsonl` and the artifact files --
and applies fixed rules, so "is it correct" has the same answer for the same run
whoever asks, and a regression shows up as a rule that used to pass.

It reads a mission directory on disk, or a deployment over HTTP through the
endpoints `api_missions.py` already serves, so one checker grades both a local
run and a hosted one:

    python -m evals.mission_check backend/data/missions/<mission_id>
    python -m evals.mission_check --latest 3
    python -m evals.mission_check --base-url https://agent-hq.onrender.com --latest 1
    python -m evals.mission_check --latest 5 --spec evals/fixtures/scouting_rules.json

Generic rules apply to every mission. A spec adds rules for one known task:
figures that must match an answer key, text that must not appear, tools a
step must have called successfully. Standard library only, so it runs anywhere
the repository is checked out, with no server dependencies installed.

Exit status is 1 when any mission has a FAIL, so a script or CI job can gate on it.
"""
import argparse
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DEFAULT_MISSIONS = os.path.join(REPO, "backend", "data", "missions")

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"

# The sandbox's refusal wording (tools/file_ops.refusal_for). A step that hit it
# tried to read something outside its mission: worth a look even though the
# sandbox held.
_SANDBOX_REFUSAL = "may not be read"


@dataclass
class Result:
    level: str
    rule: str
    detail: str = ""


# ------------------------------------------------------------------ sources

class LocalSource:
    """A mission directory on this machine."""

    def __init__(self, directory: str):
        self.directory = os.path.abspath(directory)
        self.mission_id = os.path.basename(self.directory.rstrip("\\/"))

    def _read(self, name: str) -> Optional[str]:
        path = os.path.join(self.directory, name)
        if not os.path.isfile(path):
            return None
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()

    def manifest(self) -> Optional[Dict[str, Any]]:
        text = self._read("mission.json")
        return json.loads(text) if text else None

    def trace_text(self) -> Optional[str]:
        return self._read("trace.jsonl")

    def artifact(self, name: str) -> Optional[str]:
        return self._read(name)


def _request(url: str, token: str = "") -> urllib.request.Request:
    """A GET carrying the API token as a header, when there is one."""
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return urllib.request.Request(url, headers=headers)


class RemoteSource:
    """A mission on a running AgentHQ server, read through its API."""

    def __init__(self, base_url: str, mission_id: str, timeout: float = 30.0, token: str = ""):
        self.base_url = base_url.rstrip("/")
        self.mission_id = mission_id
        self.timeout = timeout
        self.token = token

    def _get(self, path: str) -> Optional[str]:
        url = f"{self.base_url}{path}"
        try:
            with urllib.request.urlopen(_request(url, self.token), timeout=self.timeout) as response:
                return response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            raise

    def _mission_path(self, suffix: str) -> str:
        return f"/api/missions/{urllib.parse.quote(self.mission_id)}{suffix}"

    def manifest(self) -> Optional[Dict[str, Any]]:
        text = self.artifact("mission.json")
        return json.loads(text) if text else None

    def trace_text(self) -> Optional[str]:
        return self._get(self._mission_path("/trace"))

    def artifact(self, name: str) -> Optional[str]:
        return self._get(self._mission_path(f"/artifacts/{urllib.parse.quote(name)}"))


def latest_local(missions_dir: str, count: int) -> List[LocalSource]:
    """The newest `count` finished missions (those with a mission.json)."""
    if not os.path.isdir(missions_dir):
        return []
    dirs = [os.path.join(missions_dir, d) for d in os.listdir(missions_dir)]
    dirs = [d for d in dirs if os.path.isfile(os.path.join(d, "mission.json"))]
    dirs.sort(key=lambda d: os.path.getmtime(os.path.join(d, "mission.json")), reverse=True)
    return [LocalSource(d) for d in dirs[:count]]


def latest_remote(base_url: str, count: int, token: str = "") -> List[RemoteSource]:
    """The newest `count` missions the server lists that are no longer running."""
    url = f"{base_url.rstrip('/')}/api/missions?limit={max(count * 3, 10)}"
    with urllib.request.urlopen(_request(url, token), timeout=30) as response:
        listing = json.loads(response.read().decode("utf-8"))
    finished = [m for m in listing.get("missions", []) if m.get("status") != "running"]
    return [RemoteSource(base_url, m["id"], token=token) for m in finished[:count]]


# ------------------------------------------------------------------ trace

def parse_trace(text: Optional[str]) -> List[Dict[str, Any]]:
    spans = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            spans.append(json.loads(line))
        except ValueError:
            continue  # a line cut short by a killed process
    return spans


def step_views(spans: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """
    Per plan step: its step span's attributes and every tool call made under it.

    Keyed by step id. A tool span's step is found by walking parents, because
    tool spans sit under turn and attempt spans rather than directly under the
    step.
    """
    by_id = {s.get("span_id"): s for s in spans}
    views: Dict[str, Dict[str, Any]] = {}
    for span in spans:
        if span.get("kind") == "step":
            step_id = (span.get("attrs") or {}).get("step_id") or span.get("name", "").split(":")[0]
            views.setdefault(step_id, {"tools": []})["attrs"] = span.get("attrs") or {}

    for span in spans:
        if span.get("kind") != "tool":
            continue
        parent = by_id.get(span.get("parent_id"))
        while parent is not None and parent.get("kind") != "step":
            parent = by_id.get(parent.get("parent_id"))
        if parent is None:
            continue
        step_id = (parent.get("attrs") or {}).get("step_id") or parent.get("name", "").split(":")[0]
        views.setdefault(step_id, {"tools": [], "attrs": {}})["tools"].append(span)
    return views


# ------------------------------------------------------------------ rules

def _figure_forms(value: Any) -> List[str]:
    """The ways a truth value may legitimately be written in a report."""
    if isinstance(value, bool):
        return [str(value)]
    if isinstance(value, int):
        return [str(value), f"{value:,}"]
    if isinstance(value, float):
        return [repr(value)]
    return [str(value)]


# How the report names an artifact it carries: the deliverable, then the rest.
_EMBEDDED = re.compile(r"^## (?:\U0001F4C4|\U0001F4CE) (\S+)\s*\n(.*?)(?=\n---\s*\n|\n## |\Z)",
                       re.S | re.M)


def embedded_artifacts(source) -> Dict[str, str]:
    """
    The artifacts carried inside `report.md`, by name.

    A finished mission keeps one file: the report holds every artifact in full,
    so the separate copies are removed. Everything the rules check -- the
    promised files, their figures, whether two steps filed the same content --
    is still there, one level in.
    """
    report = source.artifact("report.md") or ""
    return {name: body.strip() for name, body in _EMBEDDED.findall(report)}


def check_mission(source, spec: Optional[Dict[str, Any]] = None) -> List[Result]:
    results: List[Result] = []
    manifest = source.manifest()
    if manifest is None:
        return [Result(FAIL, "manifest", "no mission.json -- the mission never finished")]

    steps = manifest.get("steps") or []
    spans = parse_trace(source.trace_text())
    views = step_views(spans)
    carried = embedded_artifacts(source)

    def artifact(name: str) -> str:
        """One artifact's text, from its own file or from inside the report."""
        return source.artifact(name) or carried.get(name) or ""

    # 1. The status the mission gave itself.
    outcome = manifest.get("outcome")
    results.append(Result(PASS if outcome == "completed" else FAIL, "outcome",
                          f"outcome is '{outcome}'"))

    # 2. Every step's final review verdict.
    if not spans:
        results.append(Result(WARN, "verdicts", "no trace, verdicts cannot be read"))
    else:
        rejected = [f"{sid} ({v.get('attrs', {}).get('quality')})"
                    for sid, v in sorted(views.items())
                    if v.get("attrs", {}).get("verdict") == "revise"]
        results.append(Result(FAIL if rejected else PASS, "verdicts",
                              ("final verdict 'revise' on " + ", ".join(rejected))
                              if rejected else "every step accepted"))

        # An artifact the harness wrote because its step did not. Recorded on
        # the step span; older traces predate the flag and simply lack it.
        rebuilt = [sid for sid, v in sorted(views.items())
                   if v.get("attrs", {}).get("recovered")]
        results.append(Result(FAIL if rebuilt else PASS, "recovered",
                              ("artifact reconstructed by the harness, not written by the step: "
                               + ", ".join(rebuilt)) if rebuilt
                              else "every artifact was written by its own step"))

    # 3. Every promised artifact exists and is not empty.
    bodies: Dict[str, str] = {}
    missing = []
    for step in steps:
        name = step.get("produces")
        if not name:
            continue
        body = artifact(name)
        if not body or not body.strip():
            missing.append(f"{step.get('id')}: {name}")
        else:
            bodies[name] = body
    results.append(Result(FAIL if missing else PASS, "artifacts",
                          ("missing or empty: " + ", ".join(missing)) if missing
                          else f"{len(bodies)} promised artifact(s) present"))

    # 4. Two steps' artifacts are never the same file.
    by_hash: Dict[str, List[str]] = {}
    for name, body in bodies.items():
        by_hash.setdefault(hashlib.sha256(body.encode("utf-8")).hexdigest(), []).append(name)
    dupes = [" = ".join(names) for names in by_hash.values() if len(names) > 1]
    results.append(Result(FAIL if dupes else PASS, "distinct",
                          ("identical artifacts: " + "; ".join(dupes)) if dupes
                          else "no two steps filed the same content"))

    # 5. A step that needed a tool called it itself.
    #
    # `needs_tools` is what the step was routed on, not a checklist it owes.
    # A step given read_file, list_files and query_data that does the whole job
    # in SQL has done better work, not worse -- so using NONE of them fails, and
    # skipping some is a note.
    if spans:
        unused, partial = [], []
        for step in steps:
            needed = step.get("needs_tools") or []
            if not needed:
                continue
            called = {t.get("name") for t in views.get(step.get("id"), {}).get("tools", [])}
            lacking = [t for t in needed if t not in called]
            if lacking and len(lacking) == len(needed):
                unused.append(f"{step.get('id')} called none of {', '.join(needed)}")
            elif lacking:
                partial.append(f"{step.get('id')} did not need {', '.join(lacking)}")
        if unused:
            results.append(Result(FAIL, "tools_called", "; ".join(unused)))
        elif partial:
            results.append(Result(WARN, "tools_called",
                                  "declared but unused: " + "; ".join(partial)))
        else:
            results.append(Result(PASS, "tools_called",
                                  "every step used the tools it was routed on"))

        tool_spans = [t for v in views.values() for t in v.get("tools", [])]
        if tool_spans and not any("args" in (t.get("attrs") or {}) for t in tool_spans):
            results.append(Result(WARN, "trace_detail",
                                  "trace predates tool-argument logging; path checks skipped"))
        else:
            probes = [f"{sid}: {t['name']}({(t.get('attrs') or {}).get('args')})"
                      for sid, v in sorted(views.items()) for t in v.get("tools", [])
                      if _SANDBOX_REFUSAL in str((t.get("attrs") or {}).get("tool_error", ""))]
            results.append(Result(WARN if probes else PASS, "sandbox",
                                  ("reads refused by the sandbox: " + "; ".join(probes[:5]))
                                  if probes else "no step reached for files outside its mission"))

    # 6. SQL shown in artifacts that nothing ran. Recomputed from the trace when
    # it records query arguments, so a mission is graded by the current rules
    # rather than whichever version of the check was running when it finished.
    rechecked = _recheck_provenance(source, manifest, views)
    if rechecked is not None:
        results.append(Result(FAIL if rechecked else PASS, "sql_provenance",
                              (f"{len(rechecked)} query(ies) shown but never attempted: "
                               + "; ".join(rechecked[:3])) if rechecked
                              else "every query shown was attempted"))
    elif "unexecuted_sql" not in manifest:
        results.append(Result(WARN, "sql_provenance", "manifest predates the SQL provenance check"))
    elif manifest["unexecuted_sql"] is None:
        results.append(Result(WARN, "sql_provenance", "not checked (resumed mission)"))
    else:
        flagged = manifest["unexecuted_sql"]
        results.append(Result(FAIL if flagged else PASS, "sql_provenance",
                              (f"{len(flagged)} query(ies) shown but never run: "
                               + "; ".join(flagged[:3])) if flagged
                              else "every query shown was executed"))

    ungrounded = manifest.get("ungrounded_figures") or []
    results.append(Result(WARN if ungrounded else PASS, "grounding",
                          ("conclusion figures in no artifact: " + ", ".join(map(str, ungrounded)))
                          if ungrounded else "conclusion figures all appear in artifacts"))

    budget = manifest.get("token_budget")
    spent = manifest.get("total_tokens")
    if budget and spent and spent > budget:
        results.append(Result(WARN, "budget", f"{spent:,} tokens against a {budget:,} budget"))

    if spec:
        results.extend(_check_spec(source, spec, steps, views, bodies, artifact))
    return results


def _recheck_provenance(source, manifest, views) -> Optional[List[str]]:
    """
    `provenance.unexecuted_sql` over this mission's artifacts and the queries
    its trace shows were attempted, or None when the trace cannot say (it
    predates argument logging) or the mission was resumed.
    """
    tool_spans = [t for v in views.values() for t in v.get("tools", [])]
    queries = [t for t in tool_spans if t.get("name") == "query_data"]
    if manifest.get("unexecuted_sql", []) is None:
        return None                       # resumed: earlier queries unknown
    if queries and not all("args" in (t.get("attrs") or {}) for t in queries):
        return None
    sys.path.insert(0, os.path.join(REPO, "backend"))
    try:
        import provenance  # noqa: E402  (pure standard library)
    except ImportError:
        return None
    attempted = []
    for span in queries:
        attrs = span.get("attrs") or {}
        reached = (attrs.get("success") or attrs.get("refused")
                   or str(attrs.get("tool_error", "")).startswith("SQL error"))
        if reached:
            attempted.append((attrs.get("args") or {}).get("query", ""))
    carried = embedded_artifacts(source)
    bodies = []
    for name in manifest.get("artifacts") or []:
        body = source.artifact(name) or carried.get(name)
        if body:
            bodies.append((name, body))
    return provenance.unexecuted_sql(bodies, attempted)


def _check_spec(source, spec, steps, views, bodies, artifact) -> List[Result]:
    results: List[Result] = []

    target = spec.get("figures_in")
    truth = spec.get("_truth") or {}
    if target and truth:
        text = bodies.get(target) or artifact(target)
        absent = [f"{key}={truth[key]}" for key in spec.get("required_figures", truth.keys())
                  if key in truth and not any(f in text for f in _figure_forms(truth[key]))]
        results.append(Result(FAIL if absent else PASS, "figures",
                              (f"{target} lacks: " + ", ".join(absent)) if absent
                              else f"{target} carries every required figure"))

    forbidden = spec.get("forbidden_text") or []
    if forbidden:
        hits = [f"{name} mentions '{word}'" for name, body in bodies.items()
                for word in forbidden if word.lower() in body.lower()]
        results.append(Result(FAIL if hits else PASS, "forbidden_text",
                              "; ".join(hits) if hits else "no artifact cites forbidden sources"))

    by_produces = {s.get("produces"): s.get("id") for s in steps}
    for produces, tools in (spec.get("tools_succeeded") or {}).items():
        step_id = by_produces.get(produces)
        if step_id is None:
            results.append(Result(FAIL, "tools_succeeded", f"no step produced {produces}"))
            continue
        ok = {t.get("name") for t in views.get(step_id, {}).get("tools", [])
              if (t.get("attrs") or {}).get("success")}
        lacking = [t for t in tools if t not in ok]
        results.append(Result(FAIL if lacking else PASS, "tools_succeeded",
                              (f"{step_id} ({produces}) had no successful "
                               + ", ".join(lacking)) if lacking
                              else f"{step_id} ({produces}) ran {', '.join(tools)} successfully"))

    for produces, tools in (spec.get("tools_attempted") or {}).items():
        step_id = by_produces.get(produces)
        called = {t.get("name") for t in views.get(step_id, {}).get("tools", [])} if step_id else set()
        lacking = [t for t in tools if t not in called]
        results.append(Result(FAIL if (lacking or step_id is None) else PASS, "tools_attempted",
                              (f"{step_id or '?'} ({produces}) never attempted "
                               + ", ".join(lacking or tools)) if (lacking or step_id is None)
                              else f"{step_id} ({produces}) attempted {', '.join(tools)} itself"))
    return results


def load_spec(path: Optional[str]) -> Optional[Dict[str, Any]]:
    if not path:
        return None
    with open(path, encoding="utf-8") as fh:
        spec = json.load(fh)
    # `answer_key` names the file of correct values; `truth` is its older name.
    key_file = spec.get("answer_key") or spec.get("truth")
    if key_file:
        truth_path = os.path.join(os.path.dirname(os.path.abspath(path)), key_file)
        with open(truth_path, encoding="utf-8") as fh:
            spec["_truth"] = json.load(fh)
    return spec


# ------------------------------------------------------------------ cli

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Grade finished AgentHQ missions against fixed rules.")
    parser.add_argument("missions", nargs="*",
                        help="mission directories, or mission ids with --base-url")
    parser.add_argument("--base-url", help="read missions from a running server instead of disk")
    parser.add_argument("--token", default=os.environ.get("AGENTHQ_API_TOKEN", ""),
                        help="API token for --base-url (default: $AGENTHQ_API_TOKEN)")
    parser.add_argument("--latest", type=int, default=0, help="check the newest N finished missions")
    parser.add_argument("--missions-dir", default=DEFAULT_MISSIONS,
                        help="where --latest looks locally")
    parser.add_argument("--spec", help="JSON spec with task-specific rules")
    parser.add_argument("--json", dest="json_out", help="also write results to this file")
    args = parser.parse_args(argv)

    if args.base_url:
        sources = [RemoteSource(args.base_url, m, token=args.token) for m in args.missions]
        if args.latest:
            sources += latest_remote(args.base_url, args.latest, args.token)
    else:
        sources = [LocalSource(m if os.path.isdir(m) else os.path.join(args.missions_dir, m))
                   for m in args.missions]
        if args.latest:
            sources += latest_local(args.missions_dir, args.latest)
    if not sources:
        parser.error("name at least one mission, or pass --latest N")

    spec = load_spec(args.spec)
    report = []
    clean = 0
    for source in sources:
        try:
            results = check_mission(source, spec)
        except (OSError, ValueError, urllib.error.URLError) as exc:
            results = [Result(FAIL, "read", f"could not read mission: {exc}")]
        failed = any(r.level == FAIL for r in results)
        clean += 0 if failed else 1
        print(f"\n{source.mission_id}  --  {'FAIL' if failed else 'CLEAN'}")
        for r in results:
            print(f"  {r.level:4}  {r.rule:16} {r.detail}")
        report.append({"mission_id": source.mission_id, "clean": not failed,
                       "results": [r.__dict__ for r in results]})

    print(f"\n{clean}/{len(sources)} mission(s) clean")
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2)
    return 0 if clean == len(sources) else 1


if __name__ == "__main__":
    sys.exit(main())
