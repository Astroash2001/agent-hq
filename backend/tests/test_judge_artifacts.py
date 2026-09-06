"""
What the judge is actually shown.

`render_artifacts` spent one budget in alphabetical order, so the first file
took what it wanted and later ones took what was left. Measured on
`approach_standup`: research_summary.md (11,541 chars) consumed 96% of the
12,000-char budget, the report got 459 characters, and standup_decision.md got
NONE -- and the judge then failed two rubric lines for a file it had never been
shown, on a run that had actually written it (10,676 bytes).

That is a measurement bug, not a model failure, and it made every multi-artifact
quality score partly a function of alphabetical order.
"""
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from evals import judge          # noqa: E402


@pytest.fixture
def mission():
    d = tempfile.mkdtemp(prefix="agenthq_judge_")

    def write(name, size, filler="x"):
        with open(os.path.join(d, name), "w", encoding="utf-8") as fh:
            fh.write(filler * size)

    return d, write


def test_a_long_first_artifact_cannot_starve_a_later_one(mission):
    """The regression, with the real sizes that produced it."""
    d, write = mission
    write("research_summary.md", 11541, "a")
    write("solar_panel_efficiency_report.md", 25000, "b")
    write("standup_decision.md", 10676, "c")

    block = judge.render_artifacts(d)

    for name in ("research_summary.md", "solar_panel_efficiency_report.md",
                 "standup_decision.md"):
        assert name in block, f"{name} was not shown to the judge at all"
    # Not merely named in a header -- its content has to be there.
    assert "ccc" in block, "standup_decision.md had a header but no body"
    assert "aaa" in block and "bbb" in block


def test_every_artifact_gets_at_least_the_floor(mission):
    d, write = mission
    for i in range(6):
        write(f"a{i}.md", 30000, chr(ord("a") + i))

    block = judge.render_artifacts(d)
    for i in range(6):
        marker = chr(ord("a") + i) * 3
        assert marker in block, f"a{i}.md contributed nothing"


def test_short_artifacts_hand_their_remainder_back(mission):
    """
    Three short files and one long one should still spend the whole budget --
    a fixed per-file share would show a quarter of it and waste the rest.
    """
    d, write = mission
    write("a.md", 100, "a")
    write("b.md", 100, "b")
    write("c.md", 100, "c")
    write("d_long.md", 40000, "d")

    block = judge.render_artifacts(d)
    d_shown = block.count("d")

    assert d_shown > judge.MAX_ARTIFACT_CHARS // 2, (
        f"the long file got only {d_shown} chars; the short files' unused "
        f"share was not handed back")


def test_the_total_budget_is_still_respected(mission):
    d, write = mission
    for i in range(4):
        write(f"f{i}.md", 50000, chr(ord("a") + i))

    block = judge.render_artifacts(d)
    # Headers and separators add a little; the bodies must not run away.
    assert len(block) <= judge.MAX_ARTIFACT_CHARS + 1000


def test_the_briefing_and_metadata_are_still_excluded(mission):
    """The judge grades the work, never the synthesised briefing."""
    d, write = mission
    write("report.md", 500, "r")
    write("mission.json", 500, "m")
    write("real.md", 500, "z")

    block = judge.render_artifacts(d)
    assert "report.md" not in block
    assert "mission.json" not in block
    assert "real.md" in block


def test_an_empty_directory_is_empty_not_an_error(mission):
    d, _ = mission
    assert judge.render_artifacts(d) == ""
