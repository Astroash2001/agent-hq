"""
Cost, latency and trace telemetry.

The HUD gauge used to be arithmetic rather than measurement: the simulator
invented token counts, and every model in the roster was priced at two
hardcoded Gemini-flash constants. These pin down the three things that fixed
it -- a pricing table that admits what it does not know, a trace file that
survives the process that wrote it, and a per-step breakdown in mission.json.
"""
import asyncio
import json
import os
import shutil
import tempfile

import pytest

import pricing
import tracing
from config import settings
from event_bus import event_bus
from schemas import Event, EventType, Plan, PlanStep


# --------------------------------------------------------------- pricing

def test_a_known_model_prices_from_the_table():
    # gemini-2.5-flash is 0.30 in / 2.50 out per 1M tokens.
    got = pricing.price("gemini/gemini-2.5-flash", 1_000_000, 1_000_000)
    assert got == pytest.approx(2.80)
    assert pricing.price("gemini/gemini-2.5-flash", 1000, 500) == pytest.approx(
        (1000 * 0.30 + 500 * 2.50) / 1_000_000)


def test_an_unknown_model_is_unpriced_not_free():
    """A zero from `price` has to be readable as "not known", or an unpriced
    model silently costs nothing forever."""
    assert pricing.is_priced("acme/never-heard-of-it") is False
    assert pricing.price("acme/never-heard-of-it", 10_000, 10_000) == 0.0


def test_a_shared_stem_is_not_a_price():
    """`gpt-5.5` once priced off the `gpt-5` row, because the prefix match had
    no boundary check -- a model the table has never seen, reported as costed."""
    assert pricing.is_priced("gpt-5") is True
    assert pricing.is_priced("gpt-5.5") is False


@pytest.mark.parametrize("model,expected", [
    ("gemini/gemini-2.5-flash-lite", "gemini-2.5-flash-lite"),  # longest wins
    ("openai/gemini-2.5-flash", "gemini-2.5-flash"),            # gateway prefix
    ("gpt-4o-2024-08-06", "gpt-4o"),                            # dated
    ("claude-sonnet-4-5-20250929", "claude-sonnet-4-5"),
])
def test_model_strings_resolve_to_one_row(model, expected):
    assert pricing._resolve(model) == expected


# ----------------------------------------------------------------- spans

@pytest.fixture
def trace_dir(monkeypatch):
    """
    A scratch directory for one mission's trace.

    tempfile rather than pytest's tmp_path, for the reason
    test_mission_output.py already records: on this machine pytest cannot
    create its basetemp under the system temp dir (WinError 5).
    """
    monkeypatch.setattr(settings, "tracing_enabled", True)
    root = tempfile.mkdtemp(prefix="agenthq_trace_")
    try:
        yield root
    finally:
        tracing.end_mission()
        shutil.rmtree(root, ignore_errors=True)


@pytest.mark.asyncio
async def test_every_line_of_the_trace_parses_as_json(trace_dir):
    tracing.start_mission("m_trace", trace_dir)
    async with tracing.span("mission", "goal"):
        async with tracing.span("step", "s1", agent_id="researcher"):
            async with tracing.span("llm", "gemini/gemini-2.5-flash",
                                    prompt_tokens=10, completion_tokens=5):
                pass
    path = tracing.end_mission()

    assert path and os.path.isfile(path)
    with open(path, encoding="utf-8") as fh:
        lines = [ln for ln in fh.read().splitlines() if ln.strip()]
    assert len(lines) == 3
    for line in lines:
        json.loads(line)      # NDJSON: a partial file is still readable


@pytest.mark.asyncio
async def test_spans_form_one_tree_rooted_at_the_mission(trace_dir):
    trace = tracing.start_mission("m_tree", trace_dir)
    async with tracing.span("mission", "goal"):
        async with tracing.span("step", "s1"):
            async with tracing.span("turn", "t1"):
                pass
        async with tracing.span("step", "s2"):
            pass
    tracing.end_mission()

    by_id = {s.span_id: s for s in trace.spans}
    roots = [s for s in trace.spans if s.parent_id is None]
    assert len(roots) == 1 and roots[0].kind == "mission"
    for node in trace.spans:
        if node.parent_id is not None:
            assert node.parent_id in by_id, "span points at a parent that is not here"
    # And the tree is the shape the run was: the turn hangs off s1, not s2.
    turn = next(s for s in trace.spans if s.kind == "turn")
    assert by_id[turn.parent_id].name == "s1"


@pytest.mark.asyncio
async def test_parallel_steps_are_siblings_not_each_others_children(trace_dir):
    """asyncio copies the context per task, which is the whole reason parentage
    can live in a ContextVar instead of every signature."""
    trace = tracing.start_mission("m_par", trace_dir)

    async def one(name):
        async with tracing.span("step", name):
            await asyncio.sleep(0.01)

    async with tracing.span("mission", "goal"):
        await asyncio.gather(one("s1"), one("s2"), one("s3"))
    tracing.end_mission()

    mission = next(s for s in trace.spans if s.kind == "mission")
    steps = [s for s in trace.spans if s.kind == "step"]
    assert len(steps) == 3
    assert all(s.parent_id == mission.span_id for s in steps)


@pytest.mark.asyncio
async def test_the_file_is_ordered_by_start_not_by_close(trace_dir):
    trace = tracing.start_mission("m_order", trace_dir)

    async def one(name, hold):
        async with tracing.span("step", name):
            await asyncio.sleep(hold)

    async with tracing.span("mission", "goal"):
        # s1 starts first and finishes last, so append order is not start order.
        await asyncio.gather(one("s1", 0.05), one("s2", 0.01))
    path = tracing.end_mission()

    with open(path, encoding="utf-8") as fh:
        rows = [json.loads(ln) for ln in fh if ln.strip()]
    starts = [r["t_start"] for r in rows]
    assert starts == sorted(starts)
    assert [r["name"] for r in rows if r["kind"] == "step"] == ["s1", "s2"]


@pytest.mark.asyncio
async def test_a_failing_span_is_recorded_and_still_raises(trace_dir):
    """Recording an error must never turn a failed step into a quiet success."""
    trace = tracing.start_mission("m_err", trace_dir)
    with pytest.raises(RuntimeError, match="tool exploded"):
        async with tracing.span("mission", "goal"):
            async with tracing.span("tool", "terminal"):
                raise RuntimeError("tool exploded")
    tracing.end_mission()

    tool = next(s for s in trace.spans if s.kind == "tool")
    assert tool.status == "error"
    assert "RuntimeError" in tool.error and "tool exploded" in tool.error


@pytest.mark.asyncio
async def test_tracing_can_be_switched_off(trace_dir, monkeypatch):
    monkeypatch.setattr(settings, "tracing_enabled", False)
    assert tracing.start_mission("m_off", trace_dir) is None
    # The call sites must not need a guard: the span still yields something
    # they can set attributes on, it is simply never recorded.
    async with tracing.span("mission", "goal") as node:
        node.set(cost_usd=1.23)
    assert tracing.end_mission() is None
    assert os.listdir(trace_dir) == []


@pytest.mark.asyncio
async def test_losing_the_trace_does_not_lose_the_run(monkeypatch):
    monkeypatch.setattr(settings, "tracing_enabled", True)
    trace = tracing.start_mission("m_bad", "\x00 impossible")
    assert trace.path is None
    async with tracing.span("mission", "goal"):
        pass
    assert tracing.end_mission() is None
    assert len(trace.spans) == 1, "spans are still kept in memory"


def test_attributes_are_clipped(monkeypatch):
    monkeypatch.setattr(settings, "trace_max_attr_chars", 20)
    node = tracing.Span(span_id="a", parent_id=None, kind="tool", name="x",
                        t_start=0.0)
    node.set(body="y" * 500)
    assert len(node.attrs["body"]) == 23      # 20 + the ellipsis


# ------------------------------------------------------- the whole mission

@pytest.mark.asyncio
async def test_a_mission_writes_a_trace_and_a_per_step_breakdown(monkeypatch):
    """
    End to end on the no-key path, which is the one that used to lie loudest:
    the gauge showed invented numbers and nothing in the payload said so.
    """
    from orchestrator import orchestrator

    monkeypatch.setattr(settings, "tracing_enabled", True)

    completed = []
    token_updates = []

    async def tap(event: Event):
        if event.type == EventType.TASK_COMPLETED:
            completed.append(event.data)
        elif event.type == EventType.TOKEN_UPDATE:
            token_updates.append(event.data)

    event_bus.subscribe(EventType.TASK_COMPLETED, tap)
    event_bus.subscribe(EventType.TOKEN_UPDATE, tap)
    try:
        result = await orchestrator.handle_user_input(
            "Investigate and summarise sodium-ion battery chemistry")
        await asyncio.sleep(0.1)
    finally:
        event_bus.unsubscribe(EventType.TASK_COMPLETED, tap)
        event_bus.unsubscribe(EventType.TOKEN_UPDATE, tap)

    mission_dir = result["mission_dir"]
    assert mission_dir and os.path.isdir(mission_dir)

    # --- the trace ---
    trace_path = os.path.join(mission_dir, tracing.TRACE_FILENAME)
    assert os.path.isfile(trace_path)
    with open(trace_path, encoding="utf-8") as fh:
        spans = [json.loads(ln) for ln in fh if ln.strip()]
    assert spans, "a mission produced no spans"
    kinds = {s["kind"] for s in spans}
    assert {"mission", "step", "turn"} <= kinds
    roots = [s for s in spans if s["parent_id"] is None]
    assert len(roots) == 1 and roots[0]["kind"] == "mission"

    # --- the manifest ---
    with open(os.path.join(mission_dir, "mission.json"), encoding="utf-8") as fh:
        manifest = json.load(fh)
    assert len(manifest["per_step"]) == len(manifest["steps"])
    assert {r["step_id"] for r in manifest["per_step"]} == {
        s["id"] for s in manifest["steps"]}
    for row in manifest["per_step"]:
        assert {"agent_id", "cost_usd", "duration_sec", "prompt_tokens",
                "completion_tokens", "status"} <= set(row)

    # --- the simulator says it is guessing ---
    assert manifest["tokens_estimated"] is True
    assert manifest["cost_usd"] == 0.0, "the simulator called no model"
    assert completed and completed[0]["tokens_estimated"] is True
    assert token_updates and any(u.get("estimated") for u in token_updates)


def test_unpriced_models_are_named_in_the_manifest():
    """A model with no row contributes nothing to the total, so the total has
    to say which model it could not price."""
    from orchestrator import orchestrator

    plan = Plan(goal="g", steps=[
        PlanStep(id="s1", agent_id="researcher", description="d"),
        PlanStep(id="s2", agent_id="creative", description="d"),
    ])
    orchestrator._step_telemetry["t_unpriced"] = {
        "s1": {"step_id": "s1", "agent_id": "researcher",
               "model": "acme/mystery-7b", "cost_usd": 0.0,
               "prompt_tokens": 900, "completion_tokens": 100},
        "s2": {"step_id": "s2", "agent_id": "creative",
               "model": "gemini/gemini-2.5-flash", "cost_usd": 0.002,
               "prompt_tokens": 1000, "completion_tokens": 500},
    }
    try:
        got = orchestrator._mission_telemetry("t_unpriced", plan)
    finally:
        orchestrator._step_telemetry.pop("t_unpriced", None)

    assert got["unpriced_models"] == ["acme/mystery-7b"]
    assert got["cost_usd"] == pytest.approx(0.002)
    assert got["mission_tokens"] == 2500
    assert len(got["per_step"]) == 2


# ------------------------------------------------------------ the keyed path

class _Usage:
    def __init__(self, prompt, completion):
        self.prompt_tokens = prompt
        self.completion_tokens = completion


class _Response:
    """The shape litellm returns, reduced to what telemetry reads off it."""
    def __init__(self, prompt=0, completion=0, usage=True):
        self.usage = _Usage(prompt, completion) if usage else None


@pytest.mark.asyncio
async def test_a_real_call_reports_what_it_actually_cost(monkeypatch):
    """The figure has to be the provider's tokens at the answering model's
    rate -- not 100/50 defaults at Gemini flash rates, which is what the two
    call sites were doing between them."""
    import litellm

    import llm

    async def _fake(**kwargs):
        return _Response(prompt=1200, completion=800)

    monkeypatch.setattr(litellm, "acompletion", _fake)
    monkeypatch.setattr(settings, "default_model", "gemini/gemini-2.5-flash")

    got = await llm.acompletion([{"role": "user", "content": "hi"}])

    assert got.prompt_tokens == 1200
    assert got.completion_tokens == 800
    assert got.total_tokens == 2000
    assert got.model == "gemini/gemini-2.5-flash"
    assert got.attempts == 1 and got.fell_back is False
    # 1200 * 0.30/1M + 800 * 2.50/1M
    assert got.cost_usd == pytest.approx((1200 * 0.30 + 800 * 2.50) / 1_000_000)


@pytest.mark.asyncio
async def test_a_response_with_no_usage_reports_zero_not_a_guess(monkeypatch):
    import litellm

    import llm

    async def _fake(**kwargs):
        return _Response(usage=False)

    monkeypatch.setattr(litellm, "acompletion", _fake)
    got = await llm.acompletion([{"role": "user", "content": "hi"}])
    assert (got.prompt_tokens, got.completion_tokens) == (0, 0)
    assert got.cost_usd == 0.0


@pytest.mark.asyncio
async def test_the_model_that_answered_is_the_one_that_is_priced(monkeypatch):
    """A fallback crosses providers, so pricing the model that was *asked* can
    be the wrong table row entirely."""
    import litellm

    import llm

    monkeypatch.setattr(settings, "default_model", "gpt-4o")
    monkeypatch.setattr(settings, "fallback_model", "gemini/gemini-2.5-flash-lite")
    monkeypatch.setattr(settings, "llm_fallback_enabled", True)
    monkeypatch.setattr(settings, "llm_max_retries", 1)

    calls = []

    async def _fake(**kwargs):
        calls.append(kwargs["model"])
        if kwargs["model"] == "gpt-4o":
            raise RuntimeError("500 server error")
        return _Response(prompt=1000, completion=1000)

    monkeypatch.setattr(litellm, "acompletion", _fake)
    got = await llm.acompletion([{"role": "user", "content": "hi"}])

    assert calls == ["gpt-4o", "gemini/gemini-2.5-flash-lite"]
    assert got.model == "gemini/gemini-2.5-flash-lite"
    assert got.fell_back is True
    # The lite rate (0.10 / 0.40), not gpt-4o's (2.50 / 10.00).
    assert got.cost_usd == pytest.approx((1000 * 0.10 + 1000 * 0.40) / 1_000_000)
