"""
Artifact contracts.

The orchestrator already carried two patches for a missing contract:
`_best_artifact`, written after a closing chat message got filed as the
deliverable, and the "Deliverable: write your full output to <name>"
instruction, written after a researcher filed a 39-byte stub as the research the
rest of the mission was built on. Both make a good artifact likelier. Neither
checked one. These pin down the layer that checks.
"""
import pytest

import contracts
from contracts import ArtifactSpec, check, infer_spec


# ------------------------------------------------------------------ the stub

def test_the_original_stub_is_caught():
    """The exact string a real GPT-5.5 run filed as its research artifact."""
    result = check("Task completed after 4 execution steps.", ArtifactSpec())
    assert result.ok is False
    assert result.severity == "major"
    assert any("placeholder" in f for f in result.failures)


@pytest.mark.parametrize("body", [
    "Task completed after 1 execution step",
    "task completed after 12 EXECUTION STEPS.",
    "Task halted by emergency stop.",
    "Task completed successfully.",
])
def test_the_other_known_stubs_are_caught(body):
    assert check(body, ArtifactSpec()).ok is False


def test_an_empty_artifact_is_a_major_failure():
    for body in ("", "   \n\t ", None):
        result = check(body, ArtifactSpec())
        assert result.ok is False
        assert result.severity == "major"


def test_real_work_passes():
    body = "# Findings\n\n" + ("Sodium-ion cells run about 160 Wh/kg. " * 20)
    result = check(body, ArtifactSpec(min_chars=200, require_heading=True))
    assert result.ok is True
    assert result.failures == []
    assert result.severity == "none"


# ------------------------------------------------------------------- length

def test_a_short_artifact_fails_min_chars_and_says_by_how_much():
    result = check("Too short.", ArtifactSpec(min_chars=500))
    assert result.ok is False
    assert any("500" in f and "10 characters" in f for f in result.failures)


def test_barely_short_is_minor_and_far_short_is_major():
    """Only one of these is worth spending a whole extra dispatch on."""
    spec = ArtifactSpec(min_chars=500)
    nearly = check("x" * 480, spec)
    nowhere = check("x" * 40, spec)
    assert nearly.severity == "minor"
    assert nowhere.severity == "major"


# ------------------------------------------------------------------ structure

def test_a_missing_required_section_is_reported_by_name():
    spec = ArtifactSpec(min_chars=10, required_sections=["Recommendation", "Risks"])
    result = check("# Report\n\nSome analysis, and a Risks section follows.", spec)
    assert result.ok is False
    assert any("Recommendation" in f for f in result.failures)
    assert not any("Risks" in f for f in result.failures), "Risks is present"


def test_a_report_needs_a_heading():
    spec = ArtifactSpec(min_chars=10, require_heading=True)
    assert check("just prose, no heading anywhere", spec).ok is False
    assert check("# Title\n\nprose", spec).ok is True
    assert check("### Deep heading\n\nprose", spec).ok is True


@pytest.mark.parametrize("body,cited", [
    ("See https://example.com/spec for the figures.", True),
    ("As reported [1] in the survey.", True),
    ("[the spec](https://example.com) says so.", True),
    ("Sodium-ion is cheaper. Everyone knows this.", False),
])
def test_citations_are_detected_loosely(body, cited):
    spec = ArtifactSpec(min_chars=1, require_citations=True)
    assert check(body, spec).ok is cited


# --------------------------------------------------------------- inference

def test_a_report_demands_more_than_notes():
    report = infer_spec("battery_report.md", "write the final report")
    notes = infer_spec("research_notes.md", "gather sources")
    assert report.min_chars > notes.min_chars
    assert report.require_heading is True
    assert notes.require_heading is False


def test_an_unremarkable_name_gets_the_permissive_default():
    spec = infer_spec("output.md", "do the thing")
    assert spec.min_chars == 200
    assert spec.require_heading is False
    assert spec.required_sections == []


def test_citations_are_only_demanded_when_they_were_obtainable():
    """A contract that demands sources from an agent with no search tool fails
    every offline run for a reason the agent cannot do anything about."""
    without = infer_spec("research_notes.md", "research the market",
                         citations_expected=False)
    with_search = infer_spec("research_notes.md", "research the market",
                             citations_expected=True)
    assert without.require_citations is False
    assert with_search.require_citations is True


def test_every_inferred_spec_still_rejects_the_stub():
    for name in ("report.md", "notes.md", "whatever.md", ""):
        spec = infer_spec(name, "")
        assert check("Task completed after 3 execution steps.", spec).ok is False


# ---------------------------------------------------------------- guidance

def test_guidance_tells_the_agent_what_to_fix():
    """An agent told only that its work was rejected produces the same thing
    again."""
    result = check("x", ArtifactSpec(min_chars=500, require_heading=True))
    guidance = result.as_guidance()
    assert "500" in guidance
    assert "heading" in guidance.lower()
    assert guidance.startswith("Your previous attempt was rejected")


def test_a_passing_check_has_no_guidance():
    assert check("#  T\n" + "y" * 500, ArtifactSpec()).as_guidance() == ""


def test_the_check_is_pure():
    """No clock, no file, no model -- this runs on every artifact, so running
    it must never be a decision."""
    body = "# R\n" + "z" * 400
    spec = infer_spec("report.md", "write it")
    first = check(body, spec)
    second = check(body, spec)
    assert (first.ok, first.failures, first.severity) == (
        second.ok, second.failures, second.severity)


# -------------------------------------------------------- structured data tests

def test_json_with_null_addresses_is_rejected():
    """Regression test: Charlie emitting records with null address when address was requested."""
    import json
    spec = infer_spec(
        "parsed_stores.json",
        "For each store produce a record: {city, store_name, address, phone (if present)}"
    )
    records = [
        {"city": "Ahmedabad", "store_name": "Shivranjani", "address": None, "phone": None},
        {"city": "Bengaluru", "store_name": "Koramangala", "address": None, "phone": None},
    ]
    result = check(json.dumps(records), spec)
    assert result.ok is False
    assert result.severity == "major"
    assert any("address" in f and "null" in f for f in result.failures)


def test_json_with_real_addresses_passes():
    import json
    spec = infer_spec(
        "parsed_stores.json",
        "For each store produce a record: {city, store_name, address, phone (if present)}"
    )
    records = [
        {"city": "Ahmedabad", "store_name": "Shivranjani", "address": "123 Main St", "phone": None},
        {"city": "Bengaluru", "store_name": "Koramangala", "address": "456 100ft Rd", "phone": None},
    ]
    result = check(json.dumps(records), spec)
    assert result.ok is True


def test_csv_with_predominantly_na_column_is_rejected():
    spec = infer_spec("bluestone_stores.csv", "produce CSV with columns city,store_name,address,phone")
    csv_body = (
        "city,store_name,address,phone\n"
        "Bengaluru,Koramangala,N/A,N/A\n"
        "Bengaluru,Jayanagar,N/A,N/A\n"
    )
    result = check(csv_body, spec)
    assert result.ok is False
    assert result.severity == "major"
    assert any("address" in f for f in result.failures)


def test_markdown_with_pervasive_na_placeholders_is_rejected():
    spec = infer_spec("bluestone_stores.md", "list stores by city")
    md_body = (
        "# Stores\n\n"
        "- Koramangala — N/A — N/A\n"
        "- Jayanagar — N/A — N/A\n"
        "- Indiranagar — N/A — N/A\n"
        "- Marathahalli — N/A — N/A\n"
    )
    result = check(md_body, spec)
    assert result.ok is False
    assert result.severity == "major"
    assert any("N/A" in f for f in result.failures)

