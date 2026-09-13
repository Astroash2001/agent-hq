"""
The second opinion on a finished artifact.

`contracts.py` answers "is this the right shape" with regexes -- fast, free, and
blind to whether the work is any good. This answers "does this actually do what
the step was asked to do", which needs a model.

Cheap by construction, because criticism that costs more than the work is not
worth having. One call, no tools, no ReAct loop, temperature 0, and
`critic_model` defaults to `settings.fallback_model` -- the small model. The
artifact is truncated before it is sent, since a judgement about whether a
report answers its brief does not need the whole report.

Skipped entirely when no credential is configured. There is no offline judge:
the built-in simulator would be grading itself, and a verdict invented by the
same fallback that wrote the artifact is worse than no verdict, because it looks
like a measurement.
"""
import logging
from dataclasses import dataclass, field
from typing import List, Optional

import llm
import tracing
from config import llm_configured, settings
from planner import _extract_json

logger = logging.getLogger("AgentHQ.Critic")

# How much of the artifact the judge sees. A judgement about whether the work
# answers the brief is made in the first couple of pages; sending 20KB costs
# real money to reach the same verdict.
MAX_ARTIFACT_CHARS = 6000

CRITIC_SYSTEM_PROMPT = """You are a reviewer inside a multi-agent system. You \
are given the mission goal, the instruction one specialist was handed, and the \
artifact that specialist produced. Judge whether the artifact does what the \
instruction asked.

Reply with JSON only. No prose, no markdown fence.
Shape: {"verdict": "accept" | "revise", "score": 0.0-1.0, "reasons": ["..."], \
"guidance": "..."}

- "accept" means a colleague could build on this as it stands. Ordinary work \
that answers the brief is an accept -- you are not grading it against an ideal.
- "revise" is for work that is missing something the instruction explicitly \
asked for, is a placeholder rather than real content, states conclusions it \
does not support, contains pervasive 'null', 'None', or 'N/A' values where \
actual data (such as addresses, metrics, contact info, prices, or descriptions) \
was requested, or is an incomplete/truncated extraction when the goal or \
instruction requested a complete list, all items, or a specific total count \
(e.g., extracting only 50 or 60 records when hundreds exist or when the source \
page was truncated). If data was truncated during fetch, instruct the agent \
to use fetch_url with save_as or offset/query_data to extract the full dataset. \
Never accept lazy placeholder extraction or truncated subsets where complete data \
was expected.
- `score` is your confidence in the work, 0 to 1.
- `reasons` are short, specific and concrete. Quote the artifact where useful.
- `guidance` is a direct instruction to the specialist saying what to change. \
Write it only for a "revise"; leave it empty for an "accept". It is handed \
straight to the agent, so address it to them and be specific about what to add \
or fix."""


@dataclass
class Critique:
    """One judgement on one artifact."""
    verdict: str = "accept"             # accept | revise
    score: float = 1.0
    reasons: List[str] = field(default_factory=list)
    guidance: str = ""
    # True when no judgement was actually made -- critic off, no key, or the
    # call failed. The caller must not read `verdict` as an endorsement.
    skipped: bool = False
    reason_skipped: str = ""


def enabled() -> bool:
    """Whether a critique would actually call a model."""
    return bool(settings.critic_enabled and llm_configured())


def model_name() -> str:
    """The judge's model. Resolved here rather than as a config default,
    because a pydantic field cannot reference a sibling field."""
    return (settings.critic_model or "").strip() or settings.fallback_model


async def critique(
    goal: str,
    step_description: str,
    artifact_text: Optional[str]
) -> Critique:
    """
    Ask a small model whether this artifact answers its brief.

    Never raises. A judge that can take a mission down is a judge that costs
    more than it is worth -- a failed critique is a skipped critique, and the
    step proceeds exactly as it did before this module existed. That is also
    why a failure here does not respect `llm_strict`: strict mode exists so a
    misconfiguration cannot masquerade as a working run, and this call is not
    the run. The work is already done and filed by the time it happens.
    """
    if not settings.critic_enabled:
        return Critique(skipped=True, reason_skipped="critic disabled")
    if not llm_configured():
        return Critique(skipped=True, reason_skipped="no model configured")
    if not (artifact_text or "").strip():
        # Nothing to judge. contracts.check has already called this a failure;
        # spending a model call to agree is waste.
        return Critique(skipped=True, reason_skipped="artifact is empty")

    body = artifact_text[:MAX_ARTIFACT_CHARS]
    truncated = len(artifact_text) > MAX_ARTIFACT_CHARS
    user = (
        f"Mission goal:\n{goal}\n\n"
        f"Instruction given to the specialist:\n{step_description}\n\n"
        f"Artifact produced"
        f"{' (truncated to the first %d characters)' % MAX_ARTIFACT_CHARS if truncated else ''}:\n"
        f"---\n{body}\n---\n\n"
        f"Return the JSON judgement."
    )

    try:
        async with tracing.span("critic", model_name()) as span:
            result = await llm.acompletion(
                [{"role": "system", "content": CRITIC_SYSTEM_PROMPT},
                 {"role": "user", "content": user}],
                model=model_name(),
                temperature=0.0,
            )
            payload = _extract_json(result.response.choices[0].message.content or "")
            span.set(cost_usd=result.cost_usd,
                     verdict=(payload or {}).get("verdict", "unparsed"))
    except Exception as exc:
        logger.warning("[critic] judgement skipped: %s: %s", type(exc).__name__, exc)
        return Critique(skipped=True, reason_skipped=f"{type(exc).__name__}: {exc}"[:200])

    if not payload:
        logger.warning("[critic] reply was not JSON; treating as no judgement")
        return Critique(skipped=True, reason_skipped="reply was not JSON")

    return _coerce(payload)


def _coerce(payload: dict) -> Critique:
    """
    Turn a model-authored payload into a Critique, defaulting to accept.

    A malformed verdict must not become a revise. The repair loop costs a whole
    extra dispatch, so an unreadable judgement should let the work through
    rather than spend the budget on a misparse.
    """
    verdict = str(payload.get("verdict", "")).strip().lower()
    if verdict not in ("accept", "revise"):
        verdict = "accept"

    try:
        score = float(payload.get("score", 1.0))
    except (TypeError, ValueError):
        score = 1.0
    score = max(0.0, min(1.0, score))

    raw_reasons = payload.get("reasons") or []
    if isinstance(raw_reasons, str):
        raw_reasons = [raw_reasons]
    reasons = [str(r)[:300] for r in raw_reasons if str(r).strip()][:5]

    guidance = str(payload.get("guidance") or "").strip()[:1200]
    if verdict == "revise" and not guidance:
        # A revise the agent cannot act on is just a rejection. Give it
        # something, from the reasons the judge did supply.
        guidance = ("Revise the artifact to address the following: "
                    + "; ".join(reasons)) if reasons else ""
        if not guidance:
            verdict = "accept"

    return Critique(verdict=verdict, score=score, reasons=reasons, guidance=guidance)
