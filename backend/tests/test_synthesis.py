"""
The mission's conclusion, and the honesty of not having one.

The briefing used to close on "All N steps completed and every artifact filed.
System state is healthy and ready for the next command." That is a claim about
the scheduler wearing the clothes of a claim about the work: it printed
identically for a mission that answered the goal and one where four specialists
each filed a stub. `evals/judge.py` grades the artifact files and pointedly never
the briefing, and that sentence is why.

The case that matters most here is the skip. Without a credential there is no
conclusion, and a briefing with no conclusion must not read like a briefing whose
conclusion was that everything is fine.
"""
from types import SimpleNamespace

import pytest

import synthesis
from config import settings


class _Response:
    def __init__(self, text):
        self.choices = [SimpleNamespace(message=SimpleNamespace(content=text))]


def _result(text, cost=0.0004, model="gemini/gemini-2.5-flash-lite"):
    return SimpleNamespace(response=_Response(text), model=model,
                           cost_usd=cost, total_tokens=210)


@pytest.fixture
def live(monkeypatch):
    """A configured credential, so the skip paths are not what is under test."""
    monkeypatch.setattr(settings, "synthesis_enabled", True)
    monkeypatch.setattr(synthesis, "llm_configured", lambda: True)


# ------------------------------------------------------------------- the skips

@pytest.mark.asyncio
async def test_no_credential_means_no_conclusion(monkeypatch):
    """
    There is no offline conclusion. The simulator would be summarising its own
    invented text, and a finding invented by the same fallback that wrote the
    artifacts reads exactly like a real one.
    """
    monkeypatch.setattr(synthesis, "llm_configured", lambda: False)
    out = await synthesis.conclude("g", [("a.md", "body")])

    assert out.skipped is True
    assert out.text == ""
    assert "no model configured" in out.reason_skipped


@pytest.mark.asyncio
async def test_disabling_it_restores_the_briefing_exactly(monkeypatch):
    monkeypatch.setattr(settings, "synthesis_enabled", False)
    monkeypatch.setattr(synthesis, "llm_configured", lambda: True)
    out = await synthesis.conclude("g", [("a.md", "body")])

    assert out.skipped is True
    assert "disabled" in out.reason_skipped


@pytest.mark.asyncio
async def test_nothing_produced_means_nothing_concluded(live):
    """
    A model asked to conclude from nothing writes something that sounds like a
    conclusion. The empty is more honest, and the step-count line already says
    the mission produced nothing.
    """
    out = await synthesis.conclude("g", [("a.md", "   "), ("b.md", "")])

    assert out.skipped is True
    assert "no artifact content" in out.reason_skipped


@pytest.mark.asyncio
async def test_a_failed_call_never_takes_the_mission_down(live, monkeypatch):
    """
    The work is done and filed by the time this runs. A conclusion that can fail
    a finished mission costs more than it is worth.
    """
    async def _boom(*a, **k):
        raise RuntimeError("provider exploded")

    monkeypatch.setattr(synthesis.llm, "acompletion", _boom)
    out = await synthesis.conclude("g", [("a.md", "real body text here")])

    assert out.skipped is True
    assert "provider exploded" in out.reason_skipped


@pytest.mark.asyncio
async def test_an_empty_reply_is_a_skip_not_an_empty_conclusion(live, monkeypatch):
    async def _blank(*a, **k):
        return _result("   ")

    monkeypatch.setattr(synthesis.llm, "acompletion", _blank)
    out = await synthesis.conclude("g", [("a.md", "real body text here")])

    assert out.skipped is True
    assert out.reason_skipped == "reply was empty"


# ------------------------------------------------------------- the actual call

@pytest.mark.asyncio
async def test_a_conclusion_carries_its_text_and_its_price(live, monkeypatch):
    finding = ("Sodium-ion wins on cost below 200 Wh/kg: `compare.md` puts it "
               "at $61/kWh against $98/kWh for lithium. Lithium keeps the "
               "energy-density lead, so the recommendation holds only for "
               "stationary storage where mass does not matter.")

    async def _ok(*a, **k):
        return _result(finding)

    monkeypatch.setattr(synthesis.llm, "acompletion", _ok)
    out = await synthesis.conclude("Compare the chemistries",
                                   [("compare.md", "long body " * 40)])

    assert out.skipped is False
    assert out.text == finding
    assert out.cost_usd == pytest.approx(0.0004)
    assert out.model == "gemini/gemini-2.5-flash-lite"


@pytest.mark.asyncio
async def test_the_artifacts_and_the_failures_both_reach_the_prompt(live, monkeypatch):
    """
    Failed steps are named so the conclusion can say what is missing rather than
    concluding confidently around a hole.
    """
    seen = {}

    async def _capture(messages, **k):
        seen["user"] = messages[-1]["content"]
        seen["max_tokens"] = k.get("max_tokens")
        return _result("ok")

    monkeypatch.setattr(synthesis.llm, "acompletion", _capture)
    await synthesis.conclude(
        "Measure it",
        [("timing.md", "the measured figure is 1.4s")],
        failures=["s2 (coder): run the benchmark twice"],
    )

    assert "the measured figure is 1.4s" in seen["user"]
    assert "timing.md" in seen["user"]
    assert "s2 (coder): run the benchmark twice" in seen["user"]
    assert seen["max_tokens"] == settings.synthesis_max_tokens


# ------------------------------------------------------------------ the bounds

def test_one_huge_artifact_cannot_crowd_out_the_others():
    """
    Each artifact is trimmed to its own cap *before* the total is applied. A
    single 40KB report must not push out the four short ones that disagree with
    it -- the disagreement is the part a conclusion most needs to see.
    """
    block = synthesis._artifact_block([
        ("huge.md", "x" * 40000),
        ("short_a.md", "the sodium figure is 160"),
        ("short_b.md", "the sodium figure is 145"),
    ])

    assert "short_a.md" in block
    assert "short_b.md" in block
    assert "the sodium figure is 145" in block
    assert len(block) <= synthesis.MAX_TOTAL_CHARS + 500


def test_the_total_cap_holds_across_many_artifacts():
    """Nine steps must not cost nine times as much to conclude as one."""
    block = synthesis._artifact_block(
        [(f"a{i}.md", "y" * 3000) for i in range(20)])

    assert len(block) <= synthesis.MAX_TOTAL_CHARS + 500


def test_a_truncated_artifact_says_so():
    block = synthesis._artifact_block([("big.md", "z" * 5000)])

    assert "[... truncated]" in block
    assert block.count("z") == synthesis.MAX_ARTIFACT_CHARS


def test_empty_artifacts_are_dropped_not_sent_as_blanks():
    block = synthesis._artifact_block([("a.md", ""), ("b.md", "  \n "),
                                       ("c.md", "real")])

    assert "a.md" not in block
    assert "b.md" not in block
    assert "c.md" in block


def test_the_model_falls_back_to_the_small_one(monkeypatch):
    monkeypatch.setattr(settings, "synthesis_model", "")
    assert synthesis.model_name() == settings.fallback_model

    monkeypatch.setattr(settings, "synthesis_model", "some/other-model")
    assert synthesis.model_name() == "some/other-model"


def test_enabled_needs_both_the_switch_and_a_key(monkeypatch):
    monkeypatch.setattr(settings, "synthesis_enabled", True)
    monkeypatch.setattr(synthesis, "llm_configured", lambda: False)
    assert synthesis.enabled() is False

    monkeypatch.setattr(synthesis, "llm_configured", lambda: True)
    assert synthesis.enabled() is True

    monkeypatch.setattr(settings, "synthesis_enabled", False)
    assert synthesis.enabled() is False


# ------------------------------------------------------- what the briefing says

@pytest.mark.asyncio
async def test_the_briefing_leads_with_the_finding(monkeypatch):
    """
    The conclusion goes above the execution graph. A reader who opens a report
    wants the answer before the audit trail, and the rest of the file is the
    audit trail.
    """
    from orchestrator import Orchestrator
    from schemas import Plan, PlanStep
    from workspace import MissionWorkspace

    orch = Orchestrator()
    plan = Plan(goal="g", source="llm", steps=[
        PlanStep(id="s1", agent_id="researcher", description="look it up",
                 produces="a.md", status="completed")])
    ws = MissionWorkspace("m_lead")
    await ws.put(name="a.md", content="the figure is 1.4s",
                 author_id="researcher", author_name="Alice")

    async def _fake(goal, artifacts, failures=None):
        return synthesis.Conclusion(text="The figure is 1.4 seconds.",
                                    skipped=False, cost_usd=0.0002)

    monkeypatch.setattr(synthesis, "conclude", _fake)
    text = await orch._synthesize_results("Measure it", [], 1.0, plan, ws)

    assert "The figure is 1.4 seconds." in text
    assert text.index("The figure is 1.4 seconds.") < text.index("Execution Graph")


@pytest.mark.asyncio
async def test_a_briefing_with_no_conclusion_says_so(monkeypatch):
    """
    The old closing sentence -- "System state is healthy and ready for the next
    command" -- is the thing this whole module exists to delete. A skipped
    conclusion must leave a stated absence, not a reassuring noise.
    """
    from orchestrator import Orchestrator
    from schemas import Plan, PlanStep
    from workspace import MissionWorkspace

    orch = Orchestrator()
    plan = Plan(goal="g", source="heuristic", steps=[
        PlanStep(id="s1", agent_id="researcher", description="look it up",
                 produces="a.md", status="completed")])
    ws = MissionWorkspace("m_skip")

    async def _skipped(goal, artifacts, failures=None):
        return synthesis.Conclusion(skipped=True,
                                    reason_skipped="no model configured")

    monkeypatch.setattr(synthesis, "conclude", _skipped)
    text = await orch._synthesize_results("Measure it", [], 1.0, plan, ws)

    assert "System state is healthy" not in text
    assert "No conclusion was drawn" in text
    assert "no model configured" in text


@pytest.mark.asyncio
async def test_a_regranted_step_is_named_in_the_briefing(monkeypatch):
    """A step moved for tool access is a routing decision the reader should see,
    the same way a memory reroute already is."""
    from orchestrator import Orchestrator
    from schemas import Plan, PlanStep
    from workspace import MissionWorkspace

    orch = Orchestrator()
    plan = Plan(goal="g", source="llm", steps=[
        PlanStep(id="s1", agent_id="coder", description="time it",
                 produces="t.md", status="completed",
                 needs_tools=["terminal"], regranted_from="creative")])
    ws = MissionWorkspace("m_grant")

    async def _skipped(goal, artifacts, failures=None):
        return synthesis.Conclusion(skipped=True, reason_skipped="disabled")

    monkeypatch.setattr(synthesis, "conclude", _skipped)
    text = await orch._synthesize_results("Measure it", [], 1.0, plan, ws)

    assert "Reassigned for tool access" in text
    assert "creative → coder" in text
    assert "terminal" in text


# --------------------------------------------- the conclusion is checked too

@pytest.mark.asyncio
async def test_a_stub_conclusion_is_rejected_not_printed(live, monkeypatch):
    """
    The conclusion is the one part of the briefing that makes a claim, and it
    was the one part nothing checked -- the eval judge reads the artifact files
    and never the briefing, which is right but leaves this unguarded.
    """
    async def _stub(*a, **k):
        return _result("Task completed after 3 execution steps")

    monkeypatch.setattr(synthesis.llm, "acompletion", _stub)
    out = await synthesis.conclude("Compare them", [("a.md", "real body " * 40)])

    assert out.skipped is True
    assert "failed its own contract" in out.reason_skipped
    assert out.text == ""


@pytest.mark.asyncio
async def test_a_one_line_conclusion_is_rejected(live, monkeypatch):
    """A sentence that leads a briefing reads as a finding whether it is one or
    not."""
    async def _thin(*a, **k):
        return _result("Sodium-ion.")

    monkeypatch.setattr(synthesis.llm, "acompletion", _thin)
    out = await synthesis.conclude("Compare them", [("a.md", "real body " * 40)])

    assert out.skipped is True
    assert "contract" in out.reason_skipped


@pytest.mark.asyncio
async def test_a_short_but_real_answer_still_passes(live, monkeypatch):
    """
    The floor has to clear a genuinely brief answer -- a definition, a single
    measured number -- or the check costs more than it catches.
    """
    answer = ("SSD stands for Solid-State Drive: storage with no moving parts, "
              "using flash memory rather than spinning platters. `answer.md` "
              "gives the definition and nothing was left unaddressed.")
    assert len(answer) >= synthesis.MIN_CONCLUSION_CHARS

    async def _short(*a, **k):
        return _result(answer)

    monkeypatch.setattr(synthesis.llm, "acompletion", _short)
    out = await synthesis.conclude("What is an SSD?", [("a.md", "body " * 60)])

    assert out.skipped is False
    assert out.text == answer


@pytest.mark.asyncio
async def test_a_rejected_conclusion_still_reports_what_it_cost(live, monkeypatch):
    """The call was made and billed whether or not its answer was usable, and a
    cost that vanishes because the output was rejected understates the run."""
    async def _stub(*a, **k):
        return _result("Task completed after 3 execution steps", cost=0.0009)

    monkeypatch.setattr(synthesis.llm, "acompletion", _stub)
    out = await synthesis.conclude("g", [("a.md", "real body " * 40)])

    assert out.skipped is True
    assert out.cost_usd == pytest.approx(0.0009)
