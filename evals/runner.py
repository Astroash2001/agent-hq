"""
Run the golden suite and write the results.

    python -m evals.runner --suite all --replay
    python -m evals.runner --suite battery_comparison --record
    python -m evals.runner --suite all --models gemini/gemini-2.5-flash,gpt-4o --repeat 3

Each task runs through the real `orchestrator.handle_user_input`. Nothing is
mocked below that line: the planner plans, the DAG schedules, agents walk to
stations, artifacts move across the workspace, contracts and the critic run. The
only thing swapped out is the model transport, so what is measured is the
harness rather than a re-implementation of it.

Two properties this file exists to protect:

**The suite must not train the thing it measures.** `memory.record` writes an
episode after every step, and `suggest_agent` reroutes based on that history --
so a suite that ran against the real store would change the routing it is
measuring, and the second run of the same task would not be the first. The
episodic store is redirected to a scratch file for the duration and restored
afterwards, and the same goes for the mission output directory.

**A miss is never a live call.** See `replay.py`.
"""
import argparse
import asyncio
import contextlib
import glob
import json
import os
import shutil
import statistics
import sys
import tempfile
import time
from typing import Any, Dict, List, Optional

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))

from evals import judge as judge_mod                     # noqa: E402
from evals import replay as replay_mod                   # noqa: E402

import tracing                                           # noqa: E402
from config import settings                              # noqa: E402
from memory import memory                                # noqa: E402

TASKS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tasks")
RESULTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")


# ------------------------------------------------------------------- tasks

def load_tasks(suite: str) -> List[Dict[str, Any]]:
    """Every task file, or the one named. `suite` is an id or 'all'/'smoke'."""
    paths = sorted(glob.glob(os.path.join(TASKS_DIR, "*.yaml")))
    tasks = []
    for path in paths:
        with open(path, "r", encoding="utf-8") as fh:
            task = yaml.safe_load(fh)
        task["_path"] = path
        task.setdefault("id", os.path.splitext(os.path.basename(path))[0])
        tasks.append(task)

    if suite in ("all", ""):
        return tasks
    if suite == "smoke":
        # What CI runs on every push. Deliberately small: the point of the smoke
        # suite is that a broken harness is caught in a minute, not that
        # everything is measured.
        return [t for t in tasks if "smoke" in (t.get("tags") or [])]
    return [t for t in tasks if t["id"] == suite]


# --------------------------------------------------------------- isolation

@contextlib.contextmanager
def isolated_state(model: Optional[str]):
    """
    Run against scratch memory and a scratch mission directory.

    Restores everything on the way out, including on a raise -- a suite that
    left `settings.memory_path` pointing at a deleted temp file would poison
    every later run in the same process.
    """
    scratch = tempfile.mkdtemp(prefix="agenthq_evals_")
    saved = {
        "memory_path": settings.memory_path,
        "missions_path": settings.missions_path,
        "default_model": settings.default_model,
        "llm_strict": settings.llm_strict,
        "tracing_enabled": settings.tracing_enabled,
    }
    saved_store_path = memory.path
    saved_episodes = list(memory.episodes)

    settings.memory_path = os.path.join(scratch, "memory.json")
    settings.missions_path = os.path.join(scratch, "missions")
    settings.tracing_enabled = True
    # A failed model call must surface as a failed task, not slide onto the
    # simulator and be scored as though a model answered.
    settings.llm_strict = True
    if model:
        settings.default_model = model

    # The store is a module-level singleton bound at import, so pointing the
    # setting somewhere else is not enough -- the live object has to be moved
    # too, or it keeps writing to the real file it opened at startup.
    memory.path = settings.memory_path
    memory.load()

    try:
        yield scratch
    finally:
        for key, value in saved.items():
            setattr(settings, key, value)
        memory.path = saved_store_path
        memory.episodes = saved_episodes
        memory._reindex()
        shutil.rmtree(scratch, ignore_errors=True)


_CREDENTIAL_FIELDS = ("gemini_api_key", "openai_api_key", "anthropic_api_key",
                      "groq_api_key", "llm_api_key")


@contextlib.contextmanager
def credentials_for(mode: str):
    """
    The credentials each mode is allowed to see.

    **replay** gets a dummy key. `llm_configured()` gates the whole LLM path:
    with no credential the agents run the built-in simulator and the planner
    falls back to keyword buckets, so a replay run would exercise a different
    code path from the one recorded and the cassettes would never be touched.
    The key is never used -- the transport is patched above it -- but it has to
    be present for the harness to take the branch under test.

    **simulate** -- a replay with no cassette to replay -- gets *none*, and this
    is enforced here rather than assumed. It used to be assumed: the harness is
    documented as running offline with no key, and it did, only because
    `env_file=".env"` was relative and `python -m evals.runner` from the repo
    root never found `backend/.env`. Fixing that path turned the documented
    offline command into one that quietly called a provider for every task
    without a cassette. A promise that depends on the working directory is not
    a promise.

    **record** and **live** are the two modes that are meant to reach a
    provider, and get the real thing.
    """
    if mode not in ("replay", "simulate"):
        yield
        return

    saved = {field: getattr(settings, field) for field in _CREDENTIAL_FIELDS}
    for field in _CREDENTIAL_FIELDS:
        setattr(settings, field, "")
    if mode == "replay":
        settings.gemini_api_key = "replay-not-a-real-key"
    try:
        yield
    finally:
        for field, value in saved.items():
            setattr(settings, field, value)


# ------------------------------------------------------------- structure

def structural_checks(task: Dict[str, Any], manifest: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    The `expect` block, checked against what the run actually did.

    Cheap, deterministic and independent of any judge -- these still hold when
    the rubric scoring is skipped, which is what keeps an offline suite
    meaningful rather than merely green.
    """
    expect = task.get("expect") or {}
    steps = manifest.get("steps") or []
    checks: List[Dict[str, Any]] = []

    def note(name: str, ok: bool, detail: str):
        checks.append({"check": name, "ok": bool(ok), "detail": detail})

    if "min_steps" in expect:
        note("min_steps", len(steps) >= expect["min_steps"],
             f"{len(steps)} steps, wanted >= {expect['min_steps']}")
    if "max_steps" in expect:
        note("max_steps", len(steps) <= expect["max_steps"],
             f"{len(steps)} steps, wanted <= {expect['max_steps']}")

    if expect.get("agents_involved"):
        used = {s.get("agent_id") for s in steps}
        missing = [a for a in expect["agents_involved"] if a not in used]
        note("agents_involved", not missing,
             f"missing {missing}" if missing else f"all present in {sorted(used)}")

    if expect.get("artifacts"):
        import fnmatch
        produced = [a for a in (manifest.get("artifacts") or [])]
        unmatched = [p for p in expect["artifacts"]
                     if not any(fnmatch.fnmatch(a, p) for a in produced)]
        note("artifacts", not unmatched,
             f"no artifact matched {unmatched}" if unmatched else f"produced {produced}")

    if expect.get("requires_meeting"):
        meetings = [s for s in steps if s.get("kind") == "meeting"]
        note("requires_meeting", bool(meetings),
             f"{len(meetings)} meeting step(s)")

    completed = sum(1 for s in steps if s.get("status") == "completed")
    note("all_steps_completed", completed == len(steps) and bool(steps),
         f"{completed}/{len(steps)} completed")

    return checks


def budget_checks(task: Dict[str, Any], manifest: Dict[str, Any],
                  duration: float) -> List[Dict[str, Any]]:
    budget = task.get("budget") or {}
    checks = []
    if "max_cost_usd" in budget:
        cost = manifest.get("cost_usd", 0.0)
        checks.append({"check": "max_cost_usd", "ok": cost <= budget["max_cost_usd"],
                       "detail": f"${cost:.4f} of ${budget['max_cost_usd']:.2f}"})
    if "max_duration_sec" in budget:
        checks.append({"check": "max_duration_sec",
                       "ok": duration <= budget["max_duration_sec"],
                       "detail": f"{duration:.1f}s of {budget['max_duration_sec']}s"})
    return checks


def must_not_checks(task: Dict[str, Any], mission_dir: str) -> List[Dict[str, Any]]:
    """
    The `must_not` block, as a literal scan of the artifacts.

    Deliberately not delegated to the judge. "Contains no placeholder text" is a
    fact about the bytes, and a fact should not be decided by a model call that
    might be having an off day.
    """
    import contracts
    patterns = (task.get("expect") or {}).get("must_not") or []
    if not patterns:
        return []
    body = judge_mod.render_artifacts(mission_dir)
    hits = [p for p in contracts.STUB_PATTERNS
            if __import__("re").search(p, body, __import__("re").I)]
    return [{"check": "must_not_stub", "ok": not hits,
             "detail": f"matched {hits}" if hits else "no stub markers found"}]


# ------------------------------------------------------------------- run

def determinism_signature(row: Dict[str, Any]) -> Dict[str, Any]:
    """
    The part of a result that must not move between two identical runs.

    Verdicts, not measurements. A check's `ok` is in; its `detail` is not,
    because the duration check renders the wall clock into its own text
    ("10.9s of 120s") and two runs of the same task legitimately differ by a
    millisecond. Comparing the whole row made every run look non-deterministic
    over a number that is supposed to vary -- which would have taught everyone
    to ignore the one signal that catches a real drift.

        python -m evals.runner --compare evals/results/a.json evals/results/b.json
    """
    return {
        "task_id": row["task_id"],
        "mode": row["mode"],
        "model": row["model"],
        "error": row["error"],
        "steps": row["steps"],
        "artifacts": sorted(row.get("artifacts") or []),
        "structural": row["structural"],
        "checks": {c["check"]: c["ok"] for c in row.get("checks") or []},
        "quality": row["quality"],
        "cost_usd": row["cost_usd"],
        "spans": row["spans"],
        "rubric": [(s["n"], s["verdict"]) for s in row.get("rubric") or []],
    }


def compare_runs(path_a: str, path_b: str) -> int:
    """Diff two results files on verdicts. Returns a process exit code."""
    with open(path_a, "r", encoding="utf-8") as fh:
        a = {r["task_id"]: determinism_signature(r) for r in json.load(fh)["rows"]}
    with open(path_b, "r", encoding="utf-8") as fh:
        b = {r["task_id"]: determinism_signature(r) for r in json.load(fh)["rows"]}

    if set(a) != set(b):
        print(f"different task sets: only in A {sorted(set(a) - set(b))}, "
              f"only in B {sorted(set(b) - set(a))}")
        return 1

    drift = [t for t in sorted(a) if a[t] != b[t]]
    print(f"compared {len(a)} task(s): {len(a) - len(drift)} identical, "
          f"{len(drift)} drifted")
    for task in drift:
        for field in a[task]:
            if a[task][field] != b[task][field]:
                print(f"  {task}.{field}")
                print(f"    A={a[task][field]}")
                print(f"    B={b[task][field]}")
    print("DETERMINISTIC" if not drift else "NOT DETERMINISTIC")
    return 0 if not drift else 1


async def run_task(task: Dict[str, Any], mode: str, model: Optional[str],
                   run_index: int) -> Dict[str, Any]:
    """One task, once. Returns the row that goes into the results file."""
    from orchestrator import orchestrator

    task_id = task["id"]
    cassettes = replay_mod.Cassettes(task_id)

    # With no recording to replay and no key to record one, the harness runs its
    # built-in simulator. That is still a real end-to-end exercise of planning,
    # scheduling, the workspace and the contracts -- it simply cannot be scored
    # on content, and the row says so rather than implying a graded result.
    effective = mode
    if mode == "replay" and not cassettes.exists():
        effective = "simulate"

    started = time.time()
    error = None
    result: Dict[str, Any] = {}

    with isolated_state(model):
        # Keyed off the EFFECTIVE mode. Substituting the dummy key in `record`
        # too -- which is what this did -- meant every `--record` run
        # authenticated as "replay-not-a-real-key" and died on a 400, which is
        # why no cassette was ever written and the quality column stayed empty.
        with credentials_for(effective):
            transport = (replay_mod.installed(cassettes, effective)
                         if effective in ("replay", "record")
                         else contextlib.nullcontext())
            judgement = judge_mod.Judgement(
                skipped=True,
                reason_skipped=("simulate mode: no cassettes recorded for this task, "
                                "so no model produced or judged the output"))
            with transport:
                try:
                    result = await orchestrator.handle_user_input(task["goal"])
                except Exception as exc:                    # noqa: BLE001
                    # One task blowing up must not end the suite -- the whole
                    # point is the row that records which task failed and why.
                    error = f"{type(exc).__name__}: {exc}"

                duration = time.time() - started
                mission_dir = result.get("mission_dir") or ""

                # The judgement is taken INSIDE the transport, which is what
                # `judge.py`'s docstring always claimed and what the code did
                # not do: judging outside it meant a replayed run re-graded
                # live, so a scored run cost money every time and could not be
                # scored at all in CI, where there is no key. Its exchange is a
                # cassette like any other, and the artifacts it grades are
                # themselves reproduced from cassettes, so the prompt it sees
                # replays identically.
                if not error and effective in ("replay", "record", "live"):
                    judgement = await judge_mod.judge(
                        task["goal"], (task.get("expect") or {}).get("rubric") or [],
                        mission_dir)

        manifest = _read_manifest(mission_dir)

        checks = (structural_checks(task, manifest)
                  + budget_checks(task, manifest, duration)
                  + must_not_checks(task, mission_dir))
        spans = _count_spans(mission_dir)
        # Copied out before isolated_state deletes the scratch directory.
        artifacts = sorted(manifest.get("artifacts") or [])

    structural = (sum(1 for c in checks if c["ok"]) / len(checks)) if checks else 0.0
    return {
        "task_id": task_id,
        "run": run_index,
        "mode": effective,
        # Naming a model that was never called would be a lie the scorecard
        # then prints in its own column. In simulate mode nothing reached a
        # provider, and the row says exactly that.
        "model": "(simulator)" if effective == "simulate" else (model or settings.default_model),
        "goal": task["goal"],
        "tags": task.get("tags") or [],
        "error": error,
        "duration_sec": round(duration, 2),
        "cost_usd": manifest.get("cost_usd", 0.0),
        "tokens_estimated": manifest.get("tokens_estimated", True),
        "steps": len(manifest.get("steps") or []),
        "artifacts": artifacts,
        "structural": round(structural, 3),
        "checks": checks,
        "quality": None if judgement.skipped else judgement.quality,
        "quality_skipped": judgement.reason_skipped if judgement.skipped else "",
        "rubric": judgement.table(),
        "spans": spans,
        "cassette_hits": cassettes.hits,
        "cassette_misses": len(cassettes.misses),
        "cassettes_recorded": cassettes.recorded,
    }


def _read_manifest(mission_dir: str) -> Dict[str, Any]:
    path = os.path.join(mission_dir or "", "mission.json")
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _count_spans(mission_dir: str) -> int:
    path = os.path.join(mission_dir or "", tracing.TRACE_FILENAME)
    if not os.path.isfile(path):
        return 0
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return sum(1 for line in fh if line.strip())
    except OSError:
        return 0


async def run_suite(tasks: List[Dict[str, Any]], mode: str, models: List[Optional[str]],
                    repeat: int) -> Dict[str, Any]:
    rows = []
    for model in models:
        for task in tasks:
            for run_index in range(1, repeat + 1):
                label = f"{task['id']} [{model or settings.default_model}]"
                print(f"  running {label} ({run_index}/{repeat}) ...", flush=True)
                row = await run_task(task, mode, model, run_index)
                rows.append(row)
                verdict = "ERROR" if row["error"] else (
                    f"structural {row['structural']:.0%}"
                    + (f", quality {row['quality']:.0%}" if row["quality"] is not None
                       else ", unjudged"))
                print(f"    -> {verdict}  {row['duration_sec']}s  "
                      f"${row['cost_usd']:.4f}  mode={row['mode']}", flush=True)
    return {"rows": rows, "summary": summarise(rows)}


def summarise(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Per model: the aggregate that goes in the scorecard's summary row."""
    out = []
    for model in sorted({r["model"] for r in rows}):
        mine = [r for r in rows if r["model"] == model]
        judged = [r["quality"] for r in mine if r["quality"] is not None]
        out.append({
            "model": model,
            "tasks": len({r["task_id"] for r in mine}),
            "runs": len(mine),
            "errors": sum(1 for r in mine if r["error"]),
            "structural": round(statistics.fmean(r["structural"] for r in mine), 3),
            "quality": round(statistics.fmean(judged), 3) if judged else None,
            "cost_usd": round(sum(r["cost_usd"] for r in mine), 4),
            "p50_latency": round(statistics.median(r["duration_sec"] for r in mine), 2),
        })
    return out


# ------------------------------------------------------------------- cli

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="evals.runner")
    parser.add_argument("--suite", default="all",
                        help="'all', 'smoke', or a single task id")
    parser.add_argument("--model", default=None, help="override the default model")
    parser.add_argument("--models", default=None,
                        help="comma-separated list, to sweep")
    parser.add_argument("--repeat", type=int, default=1,
                        help="runs per task, for pass@k and variance")
    parser.add_argument("--record", action="store_true",
                        help="call the real provider and write cassettes")
    parser.add_argument("--replay", action="store_true",
                        help="serve from cassettes; a miss is an error (default)")
    parser.add_argument("--live", action="store_true",
                        help="call the provider without recording. Not deterministic.")
    parser.add_argument("--out", default=None, help="results file path")
    parser.add_argument("--compare", nargs=2, metavar=("A", "B"),
                        help="diff two results files on verdicts and exit")
    args = parser.parse_args(argv)

    if args.compare:
        return compare_runs(*args.compare)

    mode = "replay"
    if args.record:
        mode = "record"
    elif args.live:
        mode = "live"

    tasks = load_tasks(args.suite)
    if not tasks:
        print(f"No tasks matched --suite {args.suite!r} in {TASKS_DIR}")
        return 2

    models: List[Optional[str]] = [None]
    if args.models:
        models = [m.strip() for m in args.models.split(",") if m.strip()]
    elif args.model:
        models = [args.model]

    print(f"evals: {len(tasks)} task(s), mode={mode}, "
          f"models={[m or settings.default_model for m in models]}, repeat={args.repeat}")

    report = asyncio.run(run_suite(tasks, mode, models, args.repeat))

    os.makedirs(RESULTS_DIR, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    label = (models[0] or settings.default_model).replace("/", "_")
    path = args.out or os.path.join(RESULTS_DIR, f"{stamp}_{label}.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, ensure_ascii=False)
    print(f"\nwrote {path}")

    from evals import report as report_mod
    report_mod.write_scorecard(report)

    failed = [r for r in report["rows"] if r["error"]]
    if failed:
        print(f"\n{len(failed)} task(s) errored: "
              f"{', '.join(sorted({r['task_id'] for r in failed}))}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
