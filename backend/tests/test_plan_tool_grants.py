"""
A step goes to someone who can actually do it.

The planner prompt has always listed each agent's tools. Nothing ever checked
that the model read them, so a step needing `terminal` could be handed to a
specialist holding only `write_file` -- and that does not fail cleanly. The
agent runs, finds it has no such tool, and writes a plausible report with an
invented number in it. `evals/tasks/adversarial_missing_tool.yaml` is that exact
shape: "how long does this machine take to sum 1 to ten million, measured".

Repair rather than reject. The plan is right about the work and wrong only about
who does it, so moving the step is a smaller change than discarding the graph.
"""
import pytest

from planner import PlanValidationError, validate_plan

AGENT_IDS = ["researcher", "creative", "coder", "data_analyst"]

TOOLS = {
    "researcher": ["web_search", "read_file", "write_file"],
    "creative": ["read_file", "write_file"],
    "coder": ["terminal", "read_file", "write_file"],
    "data_analyst": ["terminal", "web_search", "read_file", "write_file"],
}


def _raw(*steps):
    return {"steps": list(steps)}


def test_a_step_moves_to_an_agent_holding_the_tool_it_needs():
    steps = validate_plan(_raw({
        "id": "s1", "agent_id": "creative", "description": "Measure the runtime",
        "needs_tools": ["terminal"], "produces": "timing.md",
    }), AGENT_IDS, TOOLS)

    assert steps[0].agent_id in ("coder", "data_analyst")
    assert steps[0].regranted_from == "creative"
    assert "terminal" in TOOLS[steps[0].agent_id]


def test_a_step_whose_agent_already_holds_the_tool_is_left_alone():
    steps = validate_plan(_raw({
        "id": "s1", "agent_id": "coder", "description": "Measure the runtime",
        "needs_tools": ["terminal"], "produces": "timing.md",
    }), AGENT_IDS, TOOLS)

    assert steps[0].agent_id == "coder"
    assert steps[0].regranted_from is None


def test_a_step_needing_two_tools_goes_to_the_agent_holding_both():
    """`coder` has terminal but no web_search; only data_analyst has the pair."""
    steps = validate_plan(_raw({
        "id": "s1", "agent_id": "creative",
        "description": "Look up the current figure and measure ours against it",
        "needs_tools": ["terminal", "web_search"], "produces": "compare.md",
    }), AGENT_IDS, TOOLS)

    assert steps[0].agent_id == "data_analyst"
    assert steps[0].regranted_from == "creative"


def test_an_unknown_tool_name_is_ignored():
    """
    A hallucinated tool is held by nobody, so treating it as a real requirement
    would send every step that mentions one to whichever agent sorts first.
    """
    steps = validate_plan(_raw({
        "id": "s1", "agent_id": "creative", "description": "Write it up",
        "needs_tools": ["quantum_debugger"], "produces": "out.md",
    }), AGENT_IDS, TOOLS)

    assert steps[0].agent_id == "creative"
    assert steps[0].regranted_from is None


def test_a_requirement_no_single_agent_can_meet_leaves_the_step_where_it_was():
    """
    Both tools exist and both are held -- just never by the same person. There
    is no assignment that works, so reassigning buys nothing and hides where the
    plan went wrong. The step stays put and the run shows it.
    """
    split = {"researcher": ["web_search"], "creative": ["write_file"],
             "coder": ["terminal"], "data_analyst": ["read_file"]}
    steps = validate_plan(_raw({
        "id": "s1", "agent_id": "creative", "description": "Measure and compare",
        "needs_tools": ["terminal", "web_search"], "produces": "timing.md",
    }), AGENT_IDS, split)

    assert steps[0].agent_id == "creative"
    assert steps[0].regranted_from is None


def test_a_requirement_nobody_holds_is_dropped_and_the_rest_still_honoured():
    """
    A tool no agent in the office holds is, operationally, the same as one the
    model invented: nobody can call it. Satisfying the part of the requirement
    that *is* reachable beats abandoning the whole step to an agent that can do
    neither half.
    """
    no_terminal = {"researcher": ["web_search"], "creative": ["write_file"],
                   "coder": ["read_file"], "data_analyst": ["read_file"]}
    steps = validate_plan(_raw({
        "id": "s1", "agent_id": "creative", "description": "Measure and compare",
        "needs_tools": ["terminal", "web_search"], "produces": "timing.md",
    }), AGENT_IDS, no_terminal)

    assert steps[0].agent_id == "researcher", "web_search was still reachable"
    assert steps[0].regranted_from == "creative"


def test_regranting_prefers_an_agent_the_plan_already_uses():
    """
    Two agents can do it; one is already in the plan. Scattering a three-step
    plan across the whole roster costs a context handoff for no reason.
    """
    steps = validate_plan(_raw(
        {"id": "s1", "agent_id": "data_analyst", "description": "Gather figures",
         "needs_tools": [], "produces": "figures.md"},
        {"id": "s2", "agent_id": "creative", "description": "Measure the runtime",
         "needs_tools": ["terminal"], "produces": "timing.md",
         "dependencies": ["s1"]},
    ), AGENT_IDS, TOOLS)

    assert steps[1].agent_id == "data_analyst", "coder is capable but unused"
    assert steps[1].regranted_from == "creative"


def test_omitting_the_tool_map_skips_the_pass_entirely():
    """
    The signature is backwards compatible on purpose: `heuristic_plan` and the
    older tests call `validate_plan` with two arguments and must be unaffected.
    """
    steps = validate_plan(_raw({
        "id": "s1", "agent_id": "creative", "description": "Measure the runtime",
        "needs_tools": ["terminal"], "produces": "timing.md",
    }), AGENT_IDS)

    assert steps[0].agent_id == "creative"
    assert steps[0].needs_tools == ["terminal"]


def test_needs_tools_defaults_to_empty_and_never_blocks_a_plan():
    """A planner that ignores the field entirely still produces a valid plan."""
    steps = validate_plan(_raw({
        "id": "s1", "agent_id": "creative", "description": "Write it up",
        "produces": "out.md",
    }), AGENT_IDS, TOOLS)

    assert steps[0].needs_tools == []
    assert steps[0].agent_id == "creative"


def test_the_tool_pass_does_not_disturb_dependency_repair():
    """
    Regranting happens between the dependency pass and the consumes pass, so a
    moved step must still receive its upstream artifact.
    """
    steps = validate_plan(_raw(
        {"id": "a", "agent_id": "researcher", "description": "Find the code",
         "produces": "code.md"},
        {"id": "b", "agent_id": "creative", "description": "Time it",
         "needs_tools": ["terminal"], "produces": "timing.md",
         "dependencies": ["a"]},
    ), AGENT_IDS, TOOLS)

    assert steps[1].regranted_from == "creative"
    assert steps[1].dependencies == ["s1"]
    assert "code.md" in steps[1].consumes


def test_an_unrecognisable_payload_still_raises_with_a_tool_map():
    with pytest.raises(PlanValidationError):
        validate_plan({"steps": []}, AGENT_IDS, TOOLS)
