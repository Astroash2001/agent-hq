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
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

import contracts
import llm
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

SYNTHESIS_SYSTEM_PROMPT = """You are the orchestrator of a multi-agent system, \
writing the conclusion of a mission briefing. You are given the goal the user \
asked for and the artifacts the specialists produced.

Answer the goal. Lead with the finding, not with a description of the process.

- Write 2-5 short paragraphs, or a short paragraph followed by bullets. Markdown.
- Ground every claim in the artifacts you were given. Quote or name the file when \
a specific number or fact carries the answer.
- Where the artifacts disagree, say so and say which reading you take.
- Where the goal was not answered -- an artifact is a stub, a step failed, a \
question went unaddressed -- say that plainly and say what is missing. This is \
the most useful thing you can write, so do not paper over it.
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
    blocks: List[str] = []
    budget = MAX_TOTAL_CHARS
    for name, body in artifacts:
        body = (body or "").strip()
        if not body:
            continue
        clipped = body[:MAX_ARTIFACT_CHARS]
        if len(body) > MAX_ARTIFACT_CHARS:
            clipped += "\n[... truncated]"
        if len(clipped) > budget:
            clipped = clipped[:budget] + "\n[... truncated]"
        if not clipped.strip():
            break
        blocks.append(f"--- {name} ---\n{clipped}")
        budget -= len(clipped)
        if budget <= 0:
            break
    return "\n\n".join(blocks)


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
            result = await llm.acompletion(
                [{"role": "system", "content": SYNTHESIS_SYSTEM_PROMPT},
                 {"role": "user", "content": user}],
                model=model_name(),
                temperature=0.2,
                max_tokens=settings.synthesis_max_tokens,
            )
            text = (result.response.choices[0].message.content or "").strip()
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

    return Conclusion(text=text, cost_usd=result.cost_usd, model=result.model,
                      skipped=False)
