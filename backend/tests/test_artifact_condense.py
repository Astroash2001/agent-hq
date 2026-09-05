"""
What a downstream agent receives when the artifact is too long to paste whole.

This used to be `body[:2400]`, which keeps the introduction and throws away the
conclusion. For the documents this system actually produces -- reports, reviews,
comparisons -- the findings, the recommendation and the numbers are at the *end*,
so the hard cut discarded precisely the part the next step was waiting for, and
left nothing to say it had.

Not a model call, deliberately: summarising every handoff puts a round-trip and a
bill on an operation that runs once per edge of the DAG, and creates a place
where what a downstream agent reads is not what its author wrote.
"""
import pytest

from workspace import (HEAD_SHARE, MAX_INJECTED_CHARS, MIN_SPLIT_CHARS,
                       MissionWorkspace, condense)


def _report(head, filler_lines, tail):
    """A document shaped like the ones this system produces: the conclusion last."""
    middle = "\n".join(f"Supporting paragraph {i} with some detail in it."
                       for i in range(filler_lines))
    return f"{head}\n\n{middle}\n\n{tail}"


def test_a_short_artifact_is_untouched():
    body = "# Findings\n\nSodium-ion is cheaper below 200 Wh/kg."
    assert condense(body, MAX_INJECTED_CHARS) == body


def test_exactly_at_the_limit_is_untouched():
    body = "x" * 500
    assert condense(body, 500) == body


def test_the_conclusion_survives():
    """The regression. A hard head cut loses this line entirely."""
    body = _report("# Battery comparison", 400,
                   "## Recommendation\n\nChoose sodium-ion: $61/kWh at scale.")
    out = condense(body, MAX_INJECTED_CHARS)

    assert "Choose sodium-ion: $61/kWh at scale." in out
    assert "# Battery comparison" in out
    assert len(out) <= MAX_INJECTED_CHARS


def test_the_elision_says_how_much_is_missing():
    """
    An agent that knows a middle was dropped can ask for it or work around it.
    An agent handed a document that simply stops cannot tell the difference
    between a truncation and an author who gave up.
    """
    body = _report("# Head", 400, "# Tail")
    out = condense(body, MAX_INJECTED_CHARS)

    assert "elided from the middle" in out
    assert "characters" in out


def test_the_budget_is_respected_across_a_range_of_limits():
    body = _report("# Head", 800, "# Tail")
    for limit in (500, 800, 1200, 2400, 5000):
        out = condense(body, limit)
        assert len(out) <= limit, f"limit {limit} produced {len(out)} chars"


def test_the_head_gets_the_larger_share():
    """
    Both ends matter, but not equally: the opening establishes what the document
    is about, and a tail-heavy split hands the next agent a conclusion it has no
    context for.
    """
    body = _report("# Head", 800, "# Tail")
    out = condense(body, 2000)
    head, _, tail = out.partition("... [")
    tail = tail.partition("] ...")[2]

    assert len(head) > len(tail)
    assert len(head) > 2000 * HEAD_SHARE * 0.8


def test_a_tiny_limit_falls_back_to_a_head_cut():
    """
    Below MIN_SPLIT_CHARS there is no room to keep both ends and explain the gap,
    so the honest move is the old behaviour, still labelled.
    """
    body = _report("# Head", 400, "# Tail")
    out = condense(body, MIN_SPLIT_CHARS - 100)

    assert out.endswith("[truncated]")
    assert len(out) <= MIN_SPLIT_CHARS


def test_a_cut_lands_between_lines_not_mid_word():
    """
    Not a correctness problem, but an elision that reads as corruption makes an
    agent behave as though its input is corrupt.
    """
    body = "\n".join(f"Line {i} carries a complete sentence about batteries."
                     for i in range(300))
    out = condense(body, 2400)
    head = out.partition("\n\n... [")[0]

    assert head.endswith("."), f"head ended mid-line: {head[-40:]!r}"


def test_a_document_with_no_line_breaks_still_fits():
    """A single 20KB paragraph has no boundary to break on."""
    body = "word " * 4000
    out = condense(body, 2400)

    assert len(out) <= 2400
    assert "elided from the middle" in out


def test_empty_and_none_are_safe():
    assert condense("", 2400) == ""
    assert condense(None, 2400) == ""


@pytest.mark.asyncio
async def test_the_delivered_prompt_block_uses_it():
    """`render_context` is the caller that matters; the constant is its default."""
    ws = MissionWorkspace("m_condense")
    body = _report("# Head", 600, "## Recommendation\n\nShip it.")
    await ws.put(name="long.md", content=body, author_id="a", author_name="Alice")

    block = ws.render_context(["long.md"])

    assert "Ship it." in block
    assert "elided from the middle" in block
