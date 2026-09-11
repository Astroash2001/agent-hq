"""
The eval harness itself.

An eval suite is only worth what its determinism is worth, so most of these are
about the cassette key and the replay transport rather than about scoring. If
the key is unstable the whole scorecard is noise; if a miss falls through to the
network the suite is flaky in a way that looks like a code regression.

Lives under backend/tests/ so there is one `pytest -q` for the whole repo. The
eval package is at the repo root, hence the sys.path insert.
"""
import asyncio
import json
import os
import shutil
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from config import settings                   # noqa: E402
from evals import judge as judge_mod          # noqa: E402
from evals import replay as replay_mod        # noqa: E402
from evals import report as report_mod        # noqa: E402
from evals import runner as runner_mod        # noqa: E402


# ------------------------------------------------------------- the key

def test_the_key_is_stable_across_dict_ordering():
    """Two structurally identical requests must hash identically however the
    dicts were built, or a cassette is missed for no reason."""
    a = replay_mod.cassette_key("m", [{"role": "user", "content": "hi"}], None, 0.0)
    b = replay_mod.cassette_key("m", [{"content": "hi", "role": "user"}], None, 0.0)
    assert a == b


def test_the_key_ignores_todays_date():
    """
    config.date_context() puts the date in every system prompt, so without this
    the whole suite would go red at midnight -- every cassette missed, for a
    change that has nothing to do with the code.
    """
    monday = [{"role": "system", "content": "Today's date is Monday, 01 June 2026.\n\nYou research things."}]
    friday = [{"role": "system", "content": "Today's date is Friday, 05 June 2026.\n\nYou research things."}]
    assert replay_mod.cassette_key("m", monday) == replay_mod.cassette_key("m", friday)


def test_the_key_ignores_how_long_a_recalled_episode_took():
    """
    `memory.render_hints` writes ", took 41.2s" into the hints block it pastes
    into an agent's prompt, and that number is wall clock. A step recorded at
    41.2s and replayed at 0.9s therefore asked a different question and missed
    its own cassette -- which is why a freshly recorded task could not replay
    at all. Found by recording one for the first time.
    """
    hint = ("Relevant things you have done before in this office:\n"
            "- \"profile the cache\" -- worked, took %ss. Notes: used cProfile\n"
            "Reuse what worked. Do not repeat what did not.")
    slow = [{"role": "system", "content": hint % "41.2"}]
    fast = [{"role": "system", "content": hint % "0.9"}]
    assert replay_mod.cassette_key("m", slow) == replay_mod.cassette_key("m", fast)


def _hints(*episodes):
    """A system prompt carrying the block `memory.render_hints` pastes in."""
    lines = ["Relevant things you have done before in this office:"]
    lines += [f'- "{e}" -- worked, took 4.1s. Notes: n' for e in episodes]
    lines.append("Reuse what worked. Do not repeat what did not.")
    return ("You are a Research Specialist.\n\n"
            + "\n".join(lines) + "\n\nGo.")


def test_the_key_ignores_which_episodes_were_recalled():
    """
    Two steps that run in parallel each record an episode when they finish, so
    whether the second sees the first one's depends on which finished first --
    and a replay, where every model call returns instantly, orders them
    differently from the recording. The recalled block then differs, the key
    differs, and a mission with any fan-out is unreplayable. Diagnosed on
    `battery_team_review`, whose two research steps run side by side.

    Same trade as the date line: the hint stays in the prompt because it is
    genuinely useful to the agent, and the hash is this layer's concern.
    """
    one = replay_mod.cassette_key("m", [{"role": "system", "content": _hints("a")}])
    two = replay_mod.cassette_key(
        "m", [{"role": "system", "content": _hints("a", "b")}])
    assert one == two, "a differently-populated recall block changed the key"


def test_recalling_nothing_is_still_a_different_request():
    """
    Normalising the block's contents is right; normalising away whether there
    was one at all is too coarse. An agent that remembered something and one
    that remembered nothing were asked genuinely different questions.
    """
    with_hints = replay_mod.cassette_key(
        "m", [{"role": "system", "content": _hints("a")}])
    without = replay_mod.cassette_key(
        "m", [{"role": "system",
              "content": "You are a Research Specialist.\n\nGo."}])
    assert with_hints != without


def test_the_key_still_changes_when_the_request_does():
    base = [{"role": "user", "content": "hi"}]
    key = replay_mod.cassette_key("m", base)
    assert replay_mod.cassette_key("m2", base) != key, "model must matter"
    assert replay_mod.cassette_key("m", [{"role": "user", "content": "ho"}]) != key
    assert replay_mod.cassette_key("m", base, tools=[{"a": 1}]) != key, "tools must matter"
    assert replay_mod.cassette_key("m", base, temperature=0.9) != key


# --------------------------------------------------------- the transport

@pytest.fixture
def cassettes():
    root = tempfile.mkdtemp(prefix="agenthq_cassettes_")
    yield replay_mod.Cassettes("t_demo", root=root)
    shutil.rmtree(root, ignore_errors=True)


def _entry(content="hello", tool_calls=None, model=None):
    # The model has to match what the replay resolves to, or `_check_model`
    # refuses to serve it -- which is the point of that check, and makes a
    # hardcoded model here a fixture that tests the guard instead of the
    # transport.
    return {
        "model": model or settings.default_model,
        "prompt_tokens": 120, "completion_tokens": 30,
        "cost_usd": 0.000111, "attempts": 1, "fell_back": False,
        "response": {
            "choices": [{"finish_reason": "stop",
                         "message": {"content": content,
                                     "tool_calls": tool_calls or []}}],
            "usage": {"prompt_tokens": 120, "completion_tokens": 30},
        },
    }


@pytest.mark.asyncio
async def test_a_recorded_exchange_replays_in_the_shape_the_harness_reads(cassettes):
    """base_agent reaches for choices[0].message.content, .tool_calls[].function
    and .finish_reason. The cassette stores that shape and nothing else."""
    messages = [{"role": "user", "content": "hi"}]
    key = replay_mod.cassette_key(None, messages)
    cassettes.save(key, _entry("the answer", [
        {"id": "call_1", "function": {"name": "web_search",
                                      "arguments": '{"query": "x"}'}}]))

    async def _never(*a, **k):
        raise AssertionError("replay must not call through")

    transport = replay_mod.make_transport(cassettes, "replay", _never)
    got = await transport(messages)

    assert got.response.choices[0].message.content == "the answer"
    assert got.response.choices[0].finish_reason == "stop"
    call = got.response.choices[0].message.tool_calls[0]
    assert call.function.name == "web_search"
    assert json.loads(call.function.arguments) == {"query": "x"}
    assert got.response.usage.prompt_tokens == 120
    assert (got.model, got.cost_usd, got.attempts) == (
        settings.default_model, 0.000111, 1)
    assert cassettes.hits == 1


@pytest.mark.asyncio
async def test_no_tool_calls_replays_as_none_not_an_empty_list(cassettes):
    """base_agent tests `if hasattr(message, 'tool_calls') and message.tool_calls`,
    so an empty list is fine -- but None is what a real provider sends and what
    the loop is written against."""
    messages = [{"role": "user", "content": "hi"}]
    cassettes.save(replay_mod.cassette_key(None, messages), _entry("done"))
    transport = replay_mod.make_transport(cassettes, "replay", None)
    got = await transport(messages)
    assert got.response.choices[0].message.tool_calls is None


@pytest.mark.asyncio
async def test_a_miss_is_fatal_and_never_calls_through(cassettes):
    """A silent fall-through to the network is how a deterministic suite becomes
    a flaky one that nobody trusts."""
    called = []

    async def _real(*a, **k):
        called.append(1)

    transport = replay_mod.make_transport(cassettes, "replay", _real)
    with pytest.raises(replay_mod.CassetteMiss) as exc:
        await transport([{"role": "user", "content": "unrecorded"}])

    assert called == [], "replay reached the network"
    # The error has to name the key and how to re-record, or it is a dead end.
    assert "t_demo" in str(exc.value)
    assert "--record" in str(exc.value)


@pytest.mark.asyncio
async def test_record_mode_calls_through_and_writes_a_cassette(cassettes, monkeypatch):
    import llm

    class _Msg:
        content = "recorded"
        tool_calls = None

    class _Choice:
        finish_reason = "stop"
        message = _Msg()

    class _Usage:
        prompt_tokens, completion_tokens = 11, 7

    class _Resp:
        choices = [_Choice()]
        usage = _Usage()

    async def _real(messages, **kwargs):
        return llm.LLMResult(response=_Resp(), model="gpt-4o", prompt_tokens=11,
                             completion_tokens=7, cost_usd=0.0001, attempts=1,
                             fell_back=False)

    transport = replay_mod.make_transport(cassettes, "record", _real)
    messages = [{"role": "user", "content": "record me"}]
    await transport(messages)

    assert cassettes.recorded == 1
    stored = cassettes.load(replay_mod.cassette_key(None, messages))
    assert stored["response"]["choices"][0]["message"]["content"] == "recorded"
    assert stored["model"] == "gpt-4o"

    # And it now replays without touching the provider -- under the model that
    # recorded it, which is what `_check_model` requires and what a real replay
    # of this suite does.
    monkeypatch.setattr(settings, "default_model", "gpt-4o")

    async def _never(*a, **k):
        raise AssertionError("should have been served from disk")

    again = replay_mod.make_transport(cassettes, "replay", _never)
    assert (await again(messages)).response.choices[0].message.content == "recorded"


def test_the_transport_is_always_restored(cassettes):
    """One failing task must not leave llm.acompletion patched for every task
    after it."""
    import llm
    original = llm.acompletion

    with pytest.raises(ValueError):
        with replay_mod.installed(cassettes, "replay"):
            assert llm.acompletion is not original
            raise ValueError("task blew up")

    assert llm.acompletion is original


# ---------------------------------------------------------------- judging

def test_a_criterion_the_judge_skipped_is_a_fail():
    """The opposite default would let a truncated reply raise its own score by
    omitting the requirements it could not satisfy."""
    rubric = ["first thing", "second thing", "third thing"]
    got = judge_mod._coerce({"scores": [{"n": 1, "verdict": "pass", "reason": "yes"}]},
                            rubric, 0.0)
    assert [s.verdict for s in got.scores] == ["pass", "fail", "fail"]
    assert got.quality == pytest.approx(1 / 3, abs=0.001)   # scores round to 3dp


def test_partial_is_worth_half():
    rubric = ["a", "b"]
    got = judge_mod._coerce({"scores": [
        {"n": 1, "verdict": "pass"}, {"n": 2, "verdict": "partial"}]}, rubric, 0.0)
    assert got.quality == 0.75


def test_a_nonsense_verdict_is_a_fail_not_a_crash():
    got = judge_mod._coerce({"scores": [{"n": 1, "verdict": "brilliant"}]}, ["a"], 0.0)
    assert got.scores[0].verdict == "fail"


@pytest.mark.asyncio
async def test_no_artifacts_fails_every_criterion_without_a_call(monkeypatch):
    """A mission that produced nothing is a real result, not a missing one --
    and it costs no model call to say so."""
    monkeypatch.setattr(judge_mod, "llm_configured", lambda: True)
    root = tempfile.mkdtemp(prefix="agenthq_judge_")
    try:
        got = await judge_mod.judge("goal", ["a", "b"], root)
        assert got.quality == 0.0
        assert all(s.verdict == "fail" for s in got.scores)
        assert got.skipped is False
    finally:
        shutil.rmtree(root, ignore_errors=True)


@pytest.mark.asyncio
async def test_no_credential_means_no_quality_score():
    got = await judge_mod.judge("goal", ["a"], "")
    assert got.skipped is True and got.quality == 0.0


def test_the_judge_never_reads_the_synthesised_briefing():
    """_synthesize_results is string assembly around a template. Grading it
    grades the template, and it renders identically on a mission where every
    specialist produced nothing."""
    root = tempfile.mkdtemp(prefix="agenthq_judge2_")
    try:
        for name, body in (("report.md", "THE BRIEFING"),
                           ("mission.json", "{}"),
                           ("trace.jsonl", "{}"),
                           ("research.md", "THE ACTUAL WORK")):
            with open(os.path.join(root, name), "w", encoding="utf-8") as fh:
                fh.write(body)
        rendered = judge_mod.render_artifacts(root)
        assert "THE ACTUAL WORK" in rendered
        assert "THE BRIEFING" not in rendered
        assert "mission.json" not in rendered
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ------------------------------------------------------------- structural

def _manifest(steps, artifacts, cost=0.0):
    return {"steps": steps, "artifacts": artifacts, "cost_usd": cost}


def test_structural_checks_read_what_the_run_actually_did():
    task = {"expect": {"min_steps": 2, "max_steps": 3,
                       "agents_involved": ["researcher", "creative"],
                       "artifacts": ["*report*.md"]}}
    manifest = _manifest(
        [{"agent_id": "researcher", "status": "completed", "kind": "work"},
         {"agent_id": "creative", "status": "completed", "kind": "work"}],
        ["battery_report.md"])
    by_name = {c["check"]: c for c in runner_mod.structural_checks(task, manifest)}
    assert by_name["min_steps"]["ok"] is True
    assert by_name["max_steps"]["ok"] is True
    assert by_name["agents_involved"]["ok"] is True
    assert by_name["artifacts"]["ok"] is True
    assert by_name["all_steps_completed"]["ok"] is True


def test_an_overplanned_trivial_goal_fails_max_steps():
    """This is the adversarial_trivial row: heuristic_plan has no notion of a
    trivial goal and fans a definition question out to its full bucket."""
    task = {"expect": {"max_steps": 2}}
    manifest = _manifest([{"agent_id": "a", "status": "completed"}] * 4, [])
    by_name = {c["check"]: c for c in runner_mod.structural_checks(task, manifest)}
    assert by_name["max_steps"]["ok"] is False
    assert "4 steps" in by_name["max_steps"]["detail"]


def test_a_missing_agent_is_named():
    task = {"expect": {"agents_involved": ["researcher", "coder"]}}
    manifest = _manifest([{"agent_id": "researcher", "status": "completed"}], [])
    by_name = {c["check"]: c for c in runner_mod.structural_checks(task, manifest)}
    assert by_name["agents_involved"]["ok"] is False
    assert "coder" in by_name["agents_involved"]["detail"]


def test_a_failed_step_fails_the_run():
    task = {"expect": {}}
    manifest = _manifest([{"agent_id": "a", "status": "completed"},
                          {"agent_id": "b", "status": "failed"}], [])
    by_name = {c["check"]: c for c in runner_mod.structural_checks(task, manifest)}
    assert by_name["all_steps_completed"]["ok"] is False


def test_a_meeting_requirement_is_checked():
    task = {"expect": {"requires_meeting": True}}
    without = _manifest([{"agent_id": "a", "status": "completed", "kind": "work"}], [])
    with_one = _manifest([{"agent_id": "a", "status": "completed", "kind": "meeting"}], [])
    assert {c["check"]: c for c in runner_mod.structural_checks(task, without)
            }["requires_meeting"]["ok"] is False
    assert {c["check"]: c for c in runner_mod.structural_checks(task, with_one)
            }["requires_meeting"]["ok"] is True


def test_budget_checks_use_the_real_cost():
    task = {"budget": {"max_cost_usd": 0.10, "max_duration_sec": 60}}
    over = runner_mod.budget_checks(task, _manifest([], [], cost=0.25), 30.0)
    by_name = {c["check"]: c for c in over}
    assert by_name["max_cost_usd"]["ok"] is False
    assert by_name["max_duration_sec"]["ok"] is True


def test_a_cost_budget_on_an_unpriced_model_is_not_scored():
    """
    `pricing.py` reports 0.0 for a model it has never heard of rather than
    guessing, so a run on an unpriced model reported "$0.0000 of $0.05, ok" and
    the suite claimed to have verified a budget it could not read. That is how
    the whole 3.5 pricing gap stayed invisible: the check that should have
    caught it was the one flattering it.

    Skipped rather than failed. A budget nobody could read is neither respected
    nor breached, and failing it would make a gap in the harness look like a
    fault in the plan.
    """
    task = {"budget": {"max_cost_usd": 0.10}}
    manifest = _manifest([], [], cost=0.0)
    manifest["unpriced_models"] = ["gemini/gemini-3.5-flash-lite"]

    check = {c["check"]: c for c in runner_mod.budget_checks(task, manifest, 5.0)}
    cost = check["max_cost_usd"]

    assert cost["skipped"] is True
    assert "gemini/gemini-3.5-flash-lite" in cost["detail"]
    assert "pricing.py" in cost["detail"]


def test_a_priced_model_is_scored_as_it_always_was():
    task = {"budget": {"max_cost_usd": 0.10}}
    manifest = _manifest([], [], cost=0.02)
    manifest["unpriced_models"] = []

    cost = {c["check"]: c for c in runner_mod.budget_checks(task, manifest, 5.0)}["max_cost_usd"]

    assert cost.get("skipped") is not True
    assert cost["ok"] is True
    assert "$0.0200" in cost["detail"]


def test_a_skipped_check_leaves_the_structural_denominator():
    """
    Counting it as a pass inflates the score with something never verified;
    counting it as a fail drags every row down for a missing pricing row. Out
    of the denominator is the only reading that says neither.
    """
    assert runner_mod.structural_score([
        {"check": "a", "ok": True},
        {"check": "b", "ok": False},
        {"check": "c", "ok": True, "skipped": True},
    ]) == 0.5, "the skipped check must not be one of the three"

    # A skipped check must not turn a clean run into a partial one either.
    assert runner_mod.structural_score([
        {"check": "a", "ok": True},
        {"check": "c", "ok": True, "skipped": True},
    ]) == 1.0

    assert runner_mod.structural_score([]) == 0.0
    assert runner_mod.structural_score([{"check": "c", "ok": True, "skipped": True}]) == 0.0


# ----------------------------------------------------------------- tasks

def test_every_committed_task_file_is_valid():
    """A malformed task file would be discovered as a suite-wide crash halfway
    through a paid recording run."""
    tasks = runner_mod.load_tasks("all")
    assert len(tasks) >= 12, "the brief calls for at least 12 golden tasks"

    ids = [t["id"] for t in tasks]
    assert len(ids) == len(set(ids)), "duplicate task ids"

    for task in tasks:
        assert task.get("goal"), f"{task['id']} has no goal"
        expect = task.get("expect") or {}
        assert expect.get("rubric"), f"{task['id']} has no rubric"
        assert isinstance(expect["rubric"], list)
        budget = task.get("budget") or {}
        assert budget.get("max_cost_usd", 0) > 0, f"{task['id']} has no cost budget"
        if "min_steps" in expect and "max_steps" in expect:
            assert expect["min_steps"] <= expect["max_steps"], task["id"]


def test_the_smoke_suite_is_small_and_real():
    """CI runs this on every push. Its job is to catch a broken harness in a
    minute, not to measure everything."""
    smoke = runner_mod.load_tasks("smoke")
    assert 0 < len(smoke) <= 4
    assert set(t["id"] for t in smoke) <= set(t["id"] for t in runner_mod.load_tasks("all"))


def test_the_four_required_shapes_are_all_present():
    tags = {tag for t in runner_mod.load_tasks("all") for tag in (t.get("tags") or [])}
    for required in ("research", "code", "meeting", "adversarial"):
        assert required in tags, f"no task tagged {required}"


# --------------------------------------------------------------- isolation

def test_the_suite_does_not_train_the_memory_it_measures():
    """memory.record writes an episode per step and suggest_agent reroutes on
    that history, so a suite running against the real store would change the
    routing it is measuring."""
    from config import settings
    from memory import memory

    real_path = memory.path
    real_setting = settings.memory_path

    with runner_mod.isolated_state(None) as scratch:
        assert memory.path != real_path
        assert scratch in memory.path
        assert settings.memory_path != real_setting
        assert settings.llm_strict is True, "a failed call must fail the task"

    assert memory.path == real_path
    assert settings.memory_path == real_setting


def test_isolation_is_restored_even_when_a_task_raises():
    from config import settings
    from memory import memory

    real_path = memory.path
    real_missions = settings.missions_path

    with pytest.raises(RuntimeError):
        with runner_mod.isolated_state(None):
            raise RuntimeError("task blew up")

    assert memory.path == real_path
    assert settings.missions_path == real_missions


# ---------------------------------------------------------------- report

def _row(task_id, structural, quality, error=None):
    return {"task_id": task_id, "model": "m", "mode": "replay", "run": 1,
            "structural": structural, "quality": quality, "cost_usd": 0.01,
            "duration_sec": 5.0, "error": error, "quality_skipped": ""}


def test_the_scorecard_reports_a_changed_verdict():
    now = {"rows": [_row("t1", 1.0, 0.9)], "summary": []}
    before = {"rows": [_row("t1", 0.5, 0.4)]}
    body = report_mod.render(now, previous=before)
    assert "fail → pass" in body
    assert "`t1`" in body


def test_sub_point_noise_is_not_reported_as_a_regression():
    now = {"rows": [_row("t1", 1.0, 0.900)], "summary": []}
    before = {"rows": [_row("t1", 1.0, 0.902)]}
    assert "(=)" in report_mod.render(now, previous=before)


def test_an_unjudged_row_is_marked_not_scored_as_zero():
    row = _row("t1", 1.0, None)
    row["quality_skipped"] = "no model configured"
    body = report_mod.render({"rows": [row], "summary": []}, previous={})
    assert "—" in body
    assert "must never be read as one" in body


def test_pass_at_k_counts_only_clean_runs():
    rows = [_row("t1", 1.0, 0.9), _row("t1", 0.8, 0.9),
            _row("t1", 1.0, 0.9, error="boom")]
    folded = report_mod._fold(rows)["t1::m"]
    assert folded["runs"] == 3
    assert folded["passes"] == 1, "a structural failure and an error both count out"


# ----------------------------------------------------------- determinism

def test_the_determinism_signature_is_verdicts_not_measurements():
    """
    Two runs of the same task legitimately differ by a millisecond, and the
    duration check renders the wall clock into its own detail text
    ("10.9s of 120s"). Comparing whole rows reported every run as
    non-deterministic over a number that is supposed to vary, which would have
    taught everyone to ignore the one signal that catches a real drift.
    """
    row_a = {"task_id": "t", "mode": "replay", "model": "m", "error": None,
             "steps": 3, "artifacts": ["b.md", "a.md"], "structural": 1.0,
             "checks": [{"check": "max_duration_sec", "ok": True,
                         "detail": "10.9s of 120s"}],
             "quality": 0.8, "cost_usd": 0.01, "spans": 20, "rubric": []}
    row_b = dict(row_a, checks=[{"check": "max_duration_sec", "ok": True,
                                 "detail": "10.8s of 120s"}])
    assert (runner_mod.determinism_signature(row_a)
            == runner_mod.determinism_signature(row_b))


def test_a_flipped_verdict_is_caught():
    base = {"task_id": "t", "mode": "replay", "model": "m", "error": None,
            "steps": 3, "artifacts": [], "structural": 1.0,
            "checks": [{"check": "max_steps", "ok": True, "detail": "x"}],
            "quality": 0.8, "cost_usd": 0.01, "spans": 20, "rubric": []}
    flipped = dict(base, checks=[{"check": "max_steps", "ok": False, "detail": "x"}])
    assert (runner_mod.determinism_signature(base)
            != runner_mod.determinism_signature(flipped))


def test_artifact_order_is_not_a_drift():
    """Parallel steps file artifacts in whatever order they finish."""
    a = {"task_id": "t", "mode": "replay", "model": "m", "error": None, "steps": 2,
         "artifacts": ["a.md", "b.md"], "structural": 1.0, "checks": [],
         "quality": None, "cost_usd": 0.0, "spans": 1, "rubric": []}
    b = dict(a, artifacts=["b.md", "a.md"])
    assert runner_mod.determinism_signature(a) == runner_mod.determinism_signature(b)


def test_a_changed_rubric_verdict_is_a_drift():
    a = {"task_id": "t", "mode": "replay", "model": "m", "error": None, "steps": 1,
         "artifacts": [], "structural": 1.0, "checks": [], "quality": 0.5,
         "cost_usd": 0.0, "spans": 1,
         "rubric": [{"n": 1, "verdict": "pass", "reason": "one wording"}]}
    b = dict(a, rubric=[{"n": 1, "verdict": "fail", "reason": "another wording"}])
    assert runner_mod.determinism_signature(a) != runner_mod.determinism_signature(b)


def test_the_committed_baseline_is_readable_and_deterministic_against_itself():
    """The committed results file is the delta baseline. If it will not parse,
    every future scorecard silently loses its regression column."""
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))), "evals", "results", "baseline_simulator.json")
    if not os.path.isfile(path):
        pytest.skip("no committed baseline yet")
    with open(path, "r", encoding="utf-8") as fh:
        report = json.load(fh)
    assert report["rows"] and report["summary"]
    assert runner_mod.compare_runs(path, path) == 0


# ------------------------------------------- one model's answers are not another's

def test_a_cassette_is_not_served_for_a_different_model(monkeypatch):
    """
    A NAMED call -- the critic, the synthesis pass and the judge all name
    `settings.fallback_model` -- keys on the name it asked for. Serving it a
    recording made by some other model prints the model that was *asked for*
    beside answers that model never produced, which is the one column the whole
    scorecard is about.
    """
    monkeypatch.setattr(settings, "default_model", "gemini/gemini-3.5-flash")
    entry = {"model": "gemini/gemini-3.5-flash-lite"}

    with pytest.raises(replay_mod.CassetteModelMismatch) as excinfo:
        replay_mod._check_model(entry, "gemini/gemini-3.5-flash", "abc123",
                                "code_cache_bug")

    message = str(excinfo.value)
    assert "gemini/gemini-3.5-flash-lite" in message
    assert "gemini/gemini-3.5-flash" in message
    assert "--record" in message, "the message must say how to fix it"


def test_an_unnamed_call_is_not_measured_against_the_environment(monkeypatch):
    """
    `base_agent` calls `acompletion(messages, tools=...)` with no model at all.
    `cassette_key` hashes the model exactly as the caller passed it, so a key
    built from None can only match a recording made by a call that also passed
    None -- the hit is already proof the two are the same call, and there is
    nothing left to check.

    What `settings.default_model` names is a provider a replay never reaches.
    Comparing against it made the README's offline `--replay` fail on all
    fourteen tasks on any machine whose DEFAULT_MODEL was not the recording
    model, which is the ordinary case: the env default follows whichever lane
    is cheapest this month, the committed cassettes do not move.
    """
    monkeypatch.setattr(settings, "default_model", "openai/minimax-m3-free")
    replay_mod._check_model({"model": "openai/gpt-5-mini"}, None, "k", "t")


def test_a_replay_reports_the_model_that_answered_not_the_one_configured():
    """
    The labelling hole the guard above exists to close, closed from the other
    end: an unnamed call is no longer refused, so the row must not take its
    model from the environment. `Cassettes.served_default` collects what the
    recordings say answered, and the row prints that.
    """
    cassettes = replay_mod.Cassettes("adversarial_trivial")
    if not cassettes.exists():
        pytest.skip("no committed cassettes")
    assert cassettes.served_default == set(), "nothing served yet"

    pinned = replay_mod.recording_manifest("adversarial_trivial")
    assert pinned.get("default_model"), "committed cassettes carry their models"


def test_recording_manifest_round_trips(monkeypatch):
    """
    The manifest is what removes the environment from replay. Named calls key
    on `settings.fallback_model`, so a machine configured differently misses
    every critic, synthesis and judge cassette and silently degrades a scored
    suite to an unjudged one -- no error, just an empty quality column.

    tempfile rather than pytest's tmp_path, for the reason the other suites
    give: pytest cannot create its tmp root on this machine.
    """
    monkeypatch.setattr(settings, "default_model", "openai/gpt-5-mini")
    monkeypatch.setattr(settings, "fallback_model", "openai/gpt-5-nano")
    root = tempfile.mkdtemp(prefix="agenthq_manifest_")
    try:
        replay_mod.write_recording_manifest("some_task", root=root)

        assert replay_mod.recording_manifest("some_task", root=root) == {
            "default_model": "openai/gpt-5-mini",
            "fallback_model": "openai/gpt-5-nano",
        }
        # A task recorded before manifests existed reads as "nothing pinned"
        # rather than raising -- the cassettes are still worth serving.
        assert replay_mod.recording_manifest("never_recorded", root=root) == {}
        # And the manifest is not itself a cassette.
        assert replay_mod.Cassettes("some_task", root=root).count() == 0
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_every_committed_cassette_carries_its_models():
    """A directory with no manifest replays against whatever the env says."""
    root = replay_mod.CASSETTE_ROOT
    if not os.path.isdir(root):
        pytest.skip("no committed cassettes")
    for task_id in sorted(os.listdir(root)):
        if not os.path.isdir(os.path.join(root, task_id)):
            continue
        pinned = replay_mod.recording_manifest(task_id)
        assert pinned.get("default_model"), f"{task_id} has no recording manifest"
        assert pinned.get("fallback_model"), f"{task_id} has no fallback pinned"


def test_a_latency_budget_is_not_scored_on_replay():
    """
    A duration budget replayed from disk measures this machine, not the system
    under test: the model calls return at disk speed and what is left on the
    clock is local CPU. Scoring it made the offline suite non-deterministic
    across machines -- `--compare` called two identical runs NOT DETERMINISTIC
    over exactly this check -- which is the one property the suite exists to
    have. It is still checked where the number means something.
    """
    task = {"budget": {"max_duration_sec": 5}}
    manifest = {"cost_usd": 0.0}

    replayed = runner_mod.budget_checks(task, manifest, 900.0, "replay")
    duration = [c for c in replayed if c["check"] == "max_duration_sec"]
    assert duration and duration[0]["skipped"] is True
    # Out of the denominator entirely, not counted as a pass.
    assert runner_mod.structural_score(duration) == 0.0

    for mode in ("live", "record"):
        checks = runner_mod.budget_checks(task, manifest, 900.0, mode)
        scored = [c for c in checks if c["check"] == "max_duration_sec"]
        assert scored and not scored[0].get("skipped")
        assert scored[0]["ok"] is False, f"{mode} still enforces the budget"


def test_the_recorded_model_replays_without_complaint(monkeypatch):
    monkeypatch.setattr(settings, "default_model", "gemini/gemini-3.5-flash")
    entry = {"model": "gemini/gemini-3.5-flash-lite"}

    # Explicitly asked for, the way `--model` sets it.
    replay_mod._check_model(entry, "gemini/gemini-3.5-flash-lite", "k", "t")
    # And resolved from the default.
    monkeypatch.setattr(settings, "default_model", "gemini/gemini-3.5-flash-lite")
    replay_mod._check_model(entry, None, "k", "t")


def test_a_cassette_with_no_recorded_model_is_still_served(monkeypatch):
    """
    Older cassettes predate the field. Refusing them would discard a recorded
    suite over missing metadata, which is a worse trade than serving them.
    """
    monkeypatch.setattr(settings, "default_model", "gemini/gemini-3.5-flash")
    replay_mod._check_model({}, None, "k", "t")
    replay_mod._check_model({"model": ""}, None, "k", "t")


# --------------------------------------------- tool results have to be recorded too

def test_observers_are_replayed_and_writers_really_run():
    """
    The rule is inverted from the obvious one, and it took three recording
    sweeps to get here. "Record the tools that reach outside" is not specific
    enough: `list_files` returns a listing of this repository and `read_file`
    will read `TODO.md`. Both were found sitting in a replayed prompt, and both
    change whenever anyone commits.
    """
    # Observes something -- has to be recorded, because nothing can promise the
    # same answer twice. `read_file` and `list_files` are on this list because
    # they read the repository, not just the mission workspace: a replayed
    # prompt was found carrying the whole of TODO.md, which changes on every
    # commit.
    for observes in ("web_search", "terminal", "list_files", "read_file",
                     "code_analysis", "review_artifacts", "fetch_url",
                     "query_data", "search_workspace", "a_tool_added_later"):
        assert replay_mod.is_external(observes) is True, observes

    # Exists for its side effect -- has to really run, because that effect is
    # the mission's output. Serving `write_file` a recorded "ok" leaves the
    # workspace empty and scores the run as having produced nothing.
    for writes in ("write_file", "record_decision"):
        assert replay_mod.is_external(writes) is False, writes


def test_a_tool_key_is_stable_and_argument_sensitive():
    a = replay_mod.tool_key("web_search", {"query": "ssd", "depth": 1})
    b = replay_mod.tool_key("web_search", {"depth": 1, "query": "ssd"})
    assert a == b, "dict ordering must not change the key"
    assert replay_mod.tool_key("web_search", {"query": "hdd"}) != a
    assert replay_mod.tool_key("other", {"query": "ssd", "depth": 1}) != a


@pytest.mark.asyncio
async def test_a_recorded_search_replays_and_never_reaches_the_network(cassettes):
    """
    The bug this closes: an agent's first turn calls web_search and the results
    land verbatim in the SECOND turn's prompt. When the web moves that prompt
    changes, its key changes, and a suite that replayed cleanly an hour earlier
    misses -- with nothing in the repo having changed.
    """
    live = []

    async def _real(tool_name, arguments, agent_id=None, task_id=None):
        live.append(tool_name)
        return {"success": True, "result": "fresh results"}

    record = replay_mod.make_tool_transport(cassettes, "record", _real)
    got = await record("web_search", {"query": "ssd"})
    assert got["result"] == "fresh results"
    assert live == ["web_search"]

    live.clear()
    replay = replay_mod.make_tool_transport(cassettes, "replay", _real)
    again = await replay("web_search", {"query": "ssd"})

    assert again["result"] == "fresh results"
    assert live == [], "replay reached the network"


@pytest.mark.asyncio
async def test_a_replayed_tool_result_keeps_its_key_order(cassettes):
    """
    The subtle one, and it cost a whole re-record sweep to find.

    `Cassettes.save` writes with sort_keys=True, so a result stored as a nested
    object comes back alphabetised. `base_agent` puts `json.dumps(tool_result)`
    straight into the next turn's prompt -- so the replayed prompt differed from
    the recorded one by key order alone, and that turn missed its own cassette
    while every visible value matched. Storing the exact JSON bytes keeps the
    insertion order, which json.loads preserves.
    """
    natural = {"query": "ssd", "answer": "Solid State Drive", "count": 3,
               "configured": True, "provider": "tavily"}

    async def _real(tool_name, arguments, agent_id=None, task_id=None):
        return dict(natural)

    await replay_mod.make_tool_transport(cassettes, "record", _real)(
        "web_search", {"query": "ssd"})

    async def _never(*a, **k):
        raise AssertionError("replay must not call through")

    served = await replay_mod.make_tool_transport(cassettes, "replay", _never)(
        "web_search", {"query": "ssd"})

    assert list(served) == list(natural), "key order changed"
    assert json.dumps(served) == json.dumps(natural), (
        "the prompt is built from json.dumps(tool_result); it must be byte "
        "identical to what was recorded")


@pytest.mark.asyncio
async def test_a_local_tool_always_really_runs(cassettes):
    """Even in replay: the artifact it writes is what gets scored."""
    ran = []

    async def _real(tool_name, arguments, agent_id=None, task_id=None):
        ran.append(tool_name)
        return {"success": True}

    replay = replay_mod.make_tool_transport(cassettes, "replay", _real)
    await replay("write_file", {"name": "a.md", "content": "x"})
    await replay("write_file", {"name": "a.md", "content": "x"})

    assert ran == ["write_file", "write_file"], "a writer must never be cached"


@pytest.mark.asyncio
async def test_an_unrecorded_search_is_fatal_in_replay(cassettes):
    async def _real(*a, **k):
        raise AssertionError("replay must not call through")

    replay = replay_mod.make_tool_transport(cassettes, "replay", _real)
    with pytest.raises(replay_mod.CassetteMiss) as exc:
        await replay("web_search", {"query": "never recorded"})

    assert "web_search" in str(exc.value)
    assert "--record" in str(exc.value)


def test_installed_patches_and_restores_both_symbols(cassettes):
    """
    The integration point, and the one most able to be silently wrong: the unit
    tests above drive `make_tool_transport` directly, which proves nothing about
    whether anything ever calls it. `base_agent` reaches
    `tool_registry.execute(tool_name=..., arguments=..., ...)` by attribute on
    the singleton, so the patch has to land there and be given back.
    """
    import llm
    from tool_registry import tool_registry

    before_llm = llm.acompletion
    before_tool = tool_registry.execute

    with replay_mod.installed(cassettes, "replay"):
        assert llm.acompletion is not before_llm, "llm.acompletion was not patched"
        assert tool_registry.execute is not before_tool, \
            "tool_registry.execute was not patched"

    assert llm.acompletion is before_llm
    assert tool_registry.execute is before_tool


def test_installed_restores_even_when_the_task_raises(cassettes):
    """One failing eval must not leave the transport patched for every task
    after it -- which would silently serve cassettes to the rest of the suite."""
    import llm
    from tool_registry import tool_registry

    before_llm, before_tool = llm.acompletion, tool_registry.execute

    with pytest.raises(RuntimeError):
        with replay_mod.installed(cassettes, "replay"):
            raise RuntimeError("task exploded")

    assert llm.acompletion is before_llm
    assert tool_registry.execute is before_tool


@pytest.mark.asyncio
async def test_the_patched_execute_accepts_the_call_base_agent_makes(cassettes):
    """
    `base_agent` passes every argument by keyword. A wrapper written
    positionally would still import, still patch, and fail only at the moment an
    agent used a tool -- inside a recording run that had already been paid for.
    """
    from tool_registry import tool_registry

    seen = {}

    async def _real(tool_name, arguments, agent_id=None, task_id=None):
        seen.update(tool=tool_name, args=arguments, agent=agent_id, task=task_id)
        return {"success": True}

    original = tool_registry.execute
    tool_registry.execute = _real
    try:
        with replay_mod.installed(cassettes, "record"):
            await tool_registry.execute(
                tool_name="write_file",
                arguments={"name": "a.md", "content": "x"},
                agent_id="coder",
                task_id="t1",
            )
    finally:
        tool_registry.execute = original

    assert seen == {"tool": "write_file", "args": {"name": "a.md", "content": "x"},
                    "agent": "coder", "task": "t1"}
