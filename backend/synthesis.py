"""
The mission's conclusion, drawn rather than assembled.

`_synthesize_results` concatenates the plan, the handoffs and the artifact list
into a briefing, and then closed it with a fixed sentence: "All N steps completed
and every artifact filed. System state is healthy and ready for the next
command." That sentence is true of the *scheduler* and says nothing about the
goal. It printed identically for a mission that answered the question and one
where four specialists each filed a stub -- which is exactly why `evals/judge.py`
grades the artifact files and pointedly never the briefing.

This asks a small model the one question the assembly cannot: given the goal and
what the specialists actually produced, what is the answer? The result goes at
the top of the briefing, because a reader who opens a report wants the finding
before the audit trail.

Cheap and bounded by construction, like `critic.py`: one call, no tools, no ReAct
loop, `synthesis_model` defaulting to the small model, artifacts truncated on the
way in and the reply capped on the way out.

Skipped entirely when no credential is configured. There is no offline
conclusion: the simulator would be summarising its own invented text, and a
finding invented by the same fallback that wrote the artifacts is worse than no
finding, because it reads like one. Skipped, the briefing keeps the deterministic
step-count line it always had.
"""
import logging
import re
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

import contracts
import llm
import token_ledger
import tracing
from config import llm_configured, settings

logger = logging.getLogger("AgentHQ.Synthesis")

# Per artifact, and across all of them. A conclusion is drawn from the shape and
# the findings of the work, not from every word of it -- and a mission with nine
# steps must not cost nine times as much to conclude as a mission with one.
MAX_ARTIFACT_CHARS = 3000
MAX_TOTAL_CHARS = 12000

# Below this a "conclusion" is a sentence, and a sentence that leads a briefing
# reads as a finding whether or not it is one. Low enough that a genuinely
# short answer -- a definition, a single measured number -- still passes.
MIN_CONCLUSION_CHARS = 120

# A number as it appears in prose: 61, 1.4, 31,256, 0.5. The surrounding units
# and currency are deliberately NOT captured -- "$61/kWh" and "61 USD per kWh"
# are the same claim written twice, and a check that insisted on the decoration
# would flag the honest ones.
_NUMBER_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")

# Numbers not worth checking, because they are almost never a claim about the
# work: a year, and anything small enough to be a list position, a step count,
# a number of sources or an ordinary English "three of the four".
_YEAR_RANGE = (1900, 2100)
_SMALL = 12

SYNTHESIS_SYSTEM_PROMPT = """You are the orchestrator of a multi-agent system, \
writing the conclusion of a mission briefing. You are given the goal the user \
asked for and the artifacts the specialists produced.

Answer the goal. Lead with the finding, not with a description of the process.

- Open with one plain sentence that states the result in the reader's own \
terms -- "Your best XI is:", "The cheapest option is X", "There were 30,000 \
rows". Then, when the mission produced a set of figures, names or picks, put \
them in a compact markdown table or short list. A reader who stops after that \
block should still have the answer.
- Never open with what the mission did, how many steps it took, or which file \
holds what. The reader wants the answer; the process is recorded below you.
- Keep it short enough to finish. Write 2-5 short paragraphs, or a table \
followed by a paragraph or two. Markdown.
- Ground every claim in the artifacts you were given. Quote or name the file when \
a specific number or fact carries the answer.
- Where the artifacts disagree, say so and say which reading you take.
- Where the goal was not answered -- an artifact is a stub, a step failed, a \
question went unaddressed -- say that plainly and say what is missing. This is \
the most useful thing you can write, so do not paper over it.
- Long artifacts reach you as excerpts marked [EXCERPT]. An excerpt is shortened \
for you; the file is not. Never describe an artifact as truncated or incomplete \
because its excerpt is, and do not claim something is missing from a file when \
it may be in the part you were not shown -- say you could not see it instead.
- Do not invent facts that are not in the artifacts. Do not restate the goal back \
at the reader. Do not describe the plan, the agents or how many steps ran; that \
is already in the briefing below you.
- No heading. Your text is placed under a heading already."""


@dataclass
class Conclusion:
    """One drawn conclusion, or an honest account of why there is none."""
    text: str = ""
    cost_usd: float = 0.0
    model: str = ""
    # True when no model was actually asked. The caller must not present the
    # fallback prose as a conclusion the system reached.
    skipped: bool = True
    reason_skipped: str = ""
    # Figures the conclusion asserts that no artifact contains. Reported beside
    # the conclusion rather than used to reject it -- see `ungrounded`.
    ungrounded: List[str] = field(default_factory=list)


def enabled() -> bool:
    """Whether a conclusion would actually call a model."""
    return bool(settings.synthesis_enabled and llm_configured())


def model_name() -> str:
    """The model that concludes. Resolved here rather than as a config default,
    because a pydantic field cannot reference a sibling field."""
    return (settings.synthesis_model or "").strip() or settings.fallback_model


def _artifact_block(artifacts: Sequence[Tuple[str, str]]) -> str:
    """
    Render the artifacts for the prompt, newest-truncated to fit the cap.

    Each is trimmed to MAX_ARTIFACT_CHARS first, so a single 40KB report cannot
    crowd out the four short ones that disagree with it -- the disagreement is
    the part a conclusion most needs to see.
    """
    # Both ends of each artifact, not the head: a report's findings and its
    # limitations sit at the end. And the marker says the *excerpt* was
    # shortened. "[... truncated]" read as a fact about the file, and a
    # conclusion reported an 8KB extraction as "truncated before the required
    # results" when the results were in the half it was not shown.
    from workspace import condense

    blocks: List[str] = []
    budget = MAX_TOTAL_CHARS
    for name, body in artifacts:
        body = (body or "").strip()
        if not body:
            continue
        limit = min(MAX_ARTIFACT_CHARS, budget)
        if limit < 200:
            break
        clipped = condense(body, limit)
        if len(body) > limit:
            clipped = (f"[EXCERPT: {name} is {len(body):,} characters in full; this "
                       f"summary copy is shortened. The file itself is complete.]\n"
                       f"{clipped}")
        blocks.append(f"--- {name} ---\n{clipped}")
        budget -= len(clipped)
        if budget <= 0:
            break
    return "\n\n".join(blocks)


def _whole_sentences(text: str) -> str:
    """
    Drop a final fragment the token ceiling cut off.

    A conclusion once ended "...rating 9.5; goals per" mid-word, and that is
    what a reader sees first. Better a paragraph short than a sentence severed;
    a reply that ends cleanly is returned untouched.
    """
    body = (text or "").rstrip()
    if not body or body[-1] in ".!?:)|`" or body.endswith("---"):
        return body
    cut = max(body.rfind(". "), body.rfind(".\n"), body.rfind("!"), body.rfind("?"))
    # A whole trailing table row is worth keeping even without punctuation.
    if body.rstrip().endswith("|"):
        return body
    if cut > len(body) * 0.4:
        return body[:cut + 1]
    return body


def figures(text: str) -> List[str]:
    """
    The numbers a piece of prose asserts, normalised for comparison.

    Thousands separators are stripped so "31,256" and "31256" are one figure,
    and a trailing ".0" goes the same way. Years and small integers are dropped:
    a year is context rather than a finding, and a number under thirteen is far
    more often a list position or a count of sources than a measurement.
    """
    out: List[str] = []
    for raw in _NUMBER_RE.findall(text or ""):
        cleaned = raw.replace(",", "")
        try:
            value = float(cleaned)
        except ValueError:
            continue
        if value.is_integer():
            n = int(value)
            if abs(n) <= _SMALL:
                continue
            if _YEAR_RANGE[0] <= n <= _YEAR_RANGE[1]:
                continue
            cleaned = str(n)
        if cleaned not in out:
            out.append(cleaned)
    return out


def ungrounded(text: str, artifacts: Sequence[Tuple[str, str]]) -> List[str]:
    """
    Figures the conclusion asserts that appear in none of the artifacts.

    The conclusion is the one part of the briefing that makes a claim, and until
    this it was the one part nothing measured -- `evals/judge.py` grades the
    artifact files and pointedly never the briefing, which is right, and leaves
    exactly this unguarded. A model that misreads $61/kWh as $45/kWh writes the
    second as confidently as the first.

    Reported, never rejected, and that is the important half. A conclusion may
    legitimately state a figure no artifact contains -- $98 minus $61 is $37,
    and arithmetic on grounded numbers is the sort of thing a good summary does.
    Rejecting on this would throw away good work to catch a bad number; naming
    the figure lets a reader check it in seconds.

    Matched on the digits alone, in a corpus with its own separators stripped,
    so "$61/kWh" grounds against "61 USD per kWh" and a claim is not flagged
    over how its units were written.
    """
    claimed = figures(text)
    if not claimed:
        return []
    corpus = " ".join(body or "" for _, body in artifacts).replace(",", "")
    present = set(_NUMBER_RE.findall(corpus))
    # A figure written 61.0 in one place and 61 in the other is one figure.
    normalised = set()
    for token in present:
        normalised.add(token)
        try:
            value = float(token)
        except ValueError:
            continue
        if value.is_integer():
            normalised.add(str(int(value)))
        normalised.add(str(value))
    return [f for f in claimed if f not in normalised]


async def conclude(
    goal: str,
    artifacts: Sequence[Tuple[str, str]],
    failures: Optional[Sequence[str]] = None,
) -> Conclusion:
    """
    Draw the mission's conclusion from what it produced.

    Never raises. The work is already done and filed by the time this runs, so a
    failure here is a briefing that closes the way it always used to, not a
    mission that goes down -- the same reasoning `critic.critique` documents, and
    the same reason this does not respect `llm_strict`.
    """
    if not settings.synthesis_enabled:
        return Conclusion(reason_skipped="synthesis disabled")
    if not llm_configured():
        return Conclusion(reason_skipped="no model configured")

    block = _artifact_block(artifacts)
    if not block:
        # Nothing was produced. The step-count line already says so, and a model
        # asked to conclude from nothing will write something that sounds like a
        # conclusion, which is worse than the honest empty.
        return Conclusion(reason_skipped="no artifact content to conclude from")

    failed_note = ""
    if failures:
        failed_note = ("\n\nSteps that failed and produced nothing:\n"
                       + "\n".join(f"- {f}" for f in failures))

    user = (f"Goal:\n{goal}\n\n"
            f"Artifacts produced:\n{block}{failed_note}\n\n"
            f"Write the conclusion.")

    try:
        async with tracing.span("synthesis", model_name()) as span:
            with token_ledger.source("synthesis"):
                result = await llm.acompletion(
                    [{"role": "system", "content": SYNTHESIS_SYSTEM_PROMPT},
                     {"role": "user", "content": user}],
                    model=model_name(),
                    temperature=0.2,
                    max_tokens=settings.synthesis_max_tokens,
                )
            text = _whole_sentences((result.response.choices[0].message.content or "").strip())
            span.set(cost_usd=result.cost_usd,
                     total_tokens=result.total_tokens,
                     model=result.model,
                     chars=len(text))
    except Exception as exc:
        logger.warning("[synthesis] conclusion skipped: %s: %s", type(exc).__name__, exc)
        return Conclusion(reason_skipped=f"{type(exc).__name__}: {exc}"[:200])

    if not text:
        return Conclusion(reason_skipped="reply was empty")

    # The conclusion is the one part of the briefing that makes a claim, and
    # until now it was the one part nothing checked -- the eval judge reads the
    # artifact files and pointedly never the briefing, which is right but leaves
    # this unguarded. Contracts are free and deterministic, so at minimum a
    # conclusion that is a stub, or a sentence long, does not get presented as a
    # finding. What this cannot check is whether the finding is *true*; that
    # needs the artifacts it was drawn from, and is named in [[TODO]].
    check = contracts.check(text, contracts.ArtifactSpec(
        min_chars=MIN_CONCLUSION_CHARS,
        require_heading=False,
    ))
    if not check.ok:
        logger.warning("[synthesis] conclusion rejected: %s",
                       "; ".join(check.failures))
        return Conclusion(
            cost_usd=result.cost_usd, model=result.model,
            reason_skipped="the drawn conclusion failed its own contract: "
                           + "; ".join(check.failures))

    loose = ungrounded(text, artifacts)
    if loose:
        logger.warning("[synthesis] conclusion cites figures no artifact "
                       "contains: %s", ", ".join(loose))

    return Conclusion(text=text, cost_usd=result.cost_usd, model=result.model,
                      skipped=False, ungrounded=loose)
