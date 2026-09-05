"""
The fallback planner's sense of proportion.

`heuristic_plan` is what runs with no key, when the planning call fails, or when
the model hands back something that will not validate -- which means it is also
what the whole offline eval suite measures. It used to answer every goal with
the same four-step fan-out, so "What does SSD stand for?" hired four
specialists, "hold a standup" produced four solo desks, and every mission ended
in a file called briefing.md whatever the user had asked for.

These pin the three questions it now asks about a goal before choosing a bucket.
The risk in all of them is the same and is worth naming: the checks are keyword
heuristics, and a heuristic that fires on the wrong goal plans too little work
for it. That is why the trivial test is the strictest one here -- under-planning
is invisible in the output, while over-planning merely costs money.
"""
import pytest

from planner import (_deliverable_name, _is_trivial, _is_vague, heuristic_plan)


ROSTER = ["researcher", "coder", "data_analyst", "creative"]


def _plan(goal):
    return heuristic_plan(goal, ROSTER)


# ------------------------------------------------------------------- trivial

@pytest.mark.parametrize("goal", [
    "What does the acronym SSD stand for?",
    "What does RAII mean?",
    "Define idempotence.",
    "What is the meaning of ACID?",
])
def test_a_question_that_wants_a_fact_is_one_step(goal):
    steps = _plan(goal)
    assert len(steps) == 1, f"{goal!r} fanned out to {len(steps)} steps"
    assert steps[0].produces == "answer.md"
    assert _is_trivial(goal)


@pytest.mark.parametrize("goal", [
    # Long enough to be real work, whatever it starts with.
    "What are the tradeoffs between sodium-ion and lithium-ion batteries for "
    "home storage, and which should a UK homeowner buy this year?",
    # A deliverable is named, so it is a project even though it is short.
    "Define our caching strategy and write it up as a report.",
    # No question, no definitional phrase.
    "Research where solid-state batteries stand commercially.",
])
def test_real_work_is_never_mistaken_for_a_definition(goal):
    assert not _is_trivial(goal)
    assert len(_plan(goal)) > 1


# --------------------------------------------------------------------- vague

def test_a_goal_with_no_subject_gets_two_steps_and_states_its_assumption():
    """
    "Make it better." has no subject. Four specialists would invent four
    different subjects; two steps establish one reading and answer it.
    """
    steps = _plan("Make it better.")
    assert len(steps) == 2
    assert _is_vague("Make it better.")
    assert "assumption" in steps[0].description.lower()
    assert steps[1].dependencies == ["s1"]


def test_a_short_but_concrete_goal_is_not_vague():
    assert not _is_vague("Compare grid storage options and recommend one.")


# ------------------------------------------------------------------ meetings

@pytest.mark.parametrize("goal", [
    "Research solar panel efficiency, then hold a standup to agree the angle.",
    "Research two battery technologies, discuss the findings as a team, then write it up.",
    "Look at the numbers and let's debate the approach before building anything.",
])
def test_a_goal_that_asks_for_a_room_gets_a_meeting(goal):
    steps = _plan(goal)
    meetings = [s for s in steps if s.kind == "meeting"]
    assert len(meetings) == 1, "the goal asked for people together"

    meeting = meetings[0]
    assert meeting.attendees, "a meeting of one is just work"
    assert meeting.agent_id not in meeting.attendees, "the chair does not attend itself"
    # There is no point meeting before there is anything to meet about.
    assert meeting.dependencies
    # And the write-up follows the decision rather than racing it.
    last = steps[-1]
    assert last.kind == "work" and meeting.id in last.dependencies


def test_an_ordinary_goal_does_not_call_a_meeting():
    assert not [s for s in _plan("Research heat pumps and write a guide.")
                if s.kind == "meeting"]


# --------------------------------------------------------------- deliverable

@pytest.mark.parametrize("goal,expected", [
    ("Compare two batteries and recommend one.", "report.md"),
    ("Produce an executive briefing with a recommendation.", "briefing.md"),
    ("Turn the findings into a homeowner's guide.", "guide.md"),
    ("Summarise where the efficiency ceiling is.", "summary.md"),
])
def test_the_final_artifact_is_named_after_what_was_asked_for(goal, expected):
    assert _deliverable_name(goal) == expected
    assert _plan(goal)[-1].produces == expected


def test_the_step_that_reads_the_deliverable_still_names_it_correctly():
    """
    Renaming the last artifact is only safe if `consumes` follows it. A step
    consuming "briefing.md" that nothing produces is a delivery that never
    happens, and the agent would start with an empty desk.
    """
    steps = _plan("Compare grid storage options, then produce an executive briefing.")
    produced = {s.produces for s in steps}
    for step in steps:
        for name in step.consumes:
            assert name in produced, f"{step.id} consumes {name}, which nothing produces"


# ------------------------------------------------------------------ unchanged

def test_a_code_goal_still_fans_out_the_way_it_did():
    steps = _plan("Implement and test a fast Python fibonacci cache.")
    assert [s.agent_id for s in steps] == ["researcher", "coder", "data_analyst", "creative"]
    # s2 and s3 both wait only on s1, which is what makes them a parallel wave.
    assert steps[1].dependencies == ["s1"] and steps[2].dependencies == ["s1"]


def test_a_plan_never_names_an_agent_that_is_not_loaded():
    steps = heuristic_plan("Implement and test a fast Python cache.", ["creative"])
    assert steps and all(s.agent_id == "creative" for s in steps)
    kept = {s.id for s in steps}
    for step in steps:
        assert all(d in kept for d in step.dependencies), "orphaned dependency"
