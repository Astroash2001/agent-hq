"""
Rubric scoring for a finished mission.

Each rubric line is scored independently -- `pass`, `fail` or `partial`, with a
one-sentence reason -- rather than asking one call for an overall mark. A single
"how good was this out of ten" is unattributable: you cannot tell which
requirement it docked points for, so you cannot fix anything from it.

**It grades the artifact files, not the briefing.** `_synthesize_results` is
string assembly around a template -- headings, emoji, a per-step table. Grading
its output grades the template, and it would score well on a mission where every
specialist produced nothing, because the template renders identically either
way. The artifacts are the work.

The judge runs through `llm.acompletion`, so it goes through the same cassette
transport as everything else and a scored run replays exactly.
"""
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "backend"))

import llm                                          # noqa: E402
from config import llm_configured, settings         # noqa: E402
from planner import _extract_json                   # noqa: E402

logger = logging.getLogger("evals.judge")

# Per rubric line. `pass` is 1, `partial` a half mark, `fail` nothing -- so a
# mission that half-answers every requirement scores 0.5 rather than 0, which is
# the honest reading and keeps the metric from being all-or-nothing.
_MARKS = {"pass": 1.0, "partial": 0.5, "fail": 0.0}

# How much of the artifact set the judge sees. Long enough to carry the
# substance of a multi-step mission, short enough that judging does not cost
# more than the run being judged.
MAX_ARTIFACT_CHARS = 12000

# No artifact is shown less than this, however many there are. Below a page or
# so a rubric line about that file cannot be judged at all, and a judge that
# scores it anyway is guessing rather than grading.
MIN_ARTIFACT_SHARE = 1500

JUDGE_SYSTEM_PROMPT = """You are grading the output of a multi-agent system \
against a fixed rubric. You are given the user's goal, the artifact files the \
system produced, and a numbered list of rubric criteria.

Score EACH criterion independently. Judge only what is present in the \
artifacts -- never credit something you assume happened, and never penalise a \
criterion for a fault that belongs to a different one.

Reply with JSON only. No prose, no markdown fence.
Shape: {"scores": [{"n": 1, "verdict": "pass"|"partial"|"fail", \
"reason": "one sentence"}]}

- "pass": the artifacts clearly satisfy the criterion.
- "partial": partly satisfied -- the right idea, incompletely delivered.
- "fail": absent, or contradicted by what is there.
- One entry per criterion, in order, using the numbers given.
- `reason` is one sentence, concrete, quoting the artifacts where it helps."""


@dataclass
class RubricScore:
    n: int
    criterion: str
    verdict: str = "fail"
    reason: str = ""


@dataclass
class Judgement:
    scores: List[RubricScore] = field(default_factory=list)
    quality: float = 0.0
    skipped: bool = False
    reason_skipped: str = ""
    cost_usd: float = 0.0

    def table(self) -> List[Dict[str, Any]]:
        return [{"n": s.n, "criterion": s.criterion, "verdict": s.verdict,
                 "reason": s.reason} for s in self.scores]


def render_artifacts(mission_dir: str, skip: Optional[List[str]] = None) -> str:
    """
    The artifact files as one block for the judge.

    `report.md` and `mission.json` are excluded by default: the first is the
    synthesised briefing this module deliberately does not grade, and the second
    is metadata about the run rather than the work.
    """
    skip = skip if skip is not None else ["report.md", "mission.json", "trace.jsonl"]
    if not mission_dir or not os.path.isdir(mission_dir):
        return ""

    names = [n for n in sorted(os.listdir(mission_dir))
             if n not in skip and os.path.isfile(os.path.join(mission_dir, n))]
    if not names:
        return ""

    # Every artifact gets a share before any artifact gets seconds.
    #
    # This used to spend one budget in alphabetical order, so the first file
    # took what it wanted and later ones took what was left. Measured on
    # `approach_standup`: research_summary.md (11,541 chars) consumed 96% of the
    # 12,000, the report got 459 characters, and standup_decision.md got NONE --
    # and the judge then failed two rubric lines for a file it had never been
    # shown. Every multi-artifact score was partly measuring alphabetical order.
    #
    # Same fix, and the same reasoning, as `synthesis._artifact_block`: one long
    # document must not crowd out the short ones, because a rubric line is
    # usually about whether a *particular* artifact says a particular thing.
    share = max(MIN_ARTIFACT_SHARE, MAX_ARTIFACT_CHARS // len(names))

    bodies = []
    for name in names:
        try:
            with open(os.path.join(mission_dir, name), "r",
                      encoding="utf-8", errors="replace") as fh:
                bodies.append((name, fh.read()))
        except OSError as exc:
            logger.warning("could not read artifact %s: %s", name, exc)

    # A file under its share hands the remainder back, so three short artifacts
    # and one long one still spend the whole budget rather than a quarter of it.
    spare = sum(max(0, share - len(body)) for _, body in bodies)

    blocks: List[str] = []
    for name, body in bodies:
        allowance = share
        if len(body) > share and spare > 0:
            take = min(spare, len(body) - share)
            allowance += take
            spare -= take
        clipped = body[:allowance]
        suffix = "\n... [truncated]" if len(body) > len(clipped) else ""
        blocks.append(f"--- {name} ({len(body)} chars) ---\n{clipped}{suffix}")

    return "\n\n".join(blocks)


async def judge(goal: str, rubric: List[str], mission_dir: str) -> Judgement:
    """
    Score one mission's artifacts against its rubric.

    Never raises. A judge that can take the suite down turns one flaky provider
    into a red scorecard that says nothing about the code, so a failure is a
    *skipped* judgement and the structural checks still stand on their own.
    """
    if not rubric:
        return Judgement(skipped=True, reason_skipped="no rubric")
    if not llm_configured():
        return Judgement(skipped=True, reason_skipped="no model configured")

    artifacts = render_artifacts(mission_dir)
    if not artifacts.strip():
        # Nothing was produced. That is a real result, not a missing one: every
        # criterion fails, and it costs no call to say so.
        return Judgement(
            scores=[RubricScore(n=i + 1, criterion=c, verdict="fail",
                                reason="The mission produced no artifacts.")
                    for i, c in enumerate(rubric)],
            quality=0.0)

    numbered = "\n".join(f"{i + 1}. {c}" for i, c in enumerate(rubric))
    user = (f"User goal:\n{goal}\n\n"
            f"Artifacts produced:\n{artifacts}\n\n"
            f"Rubric criteria:\n{numbered}\n\n"
            f"Return the JSON scores.")

    try:
        result = await llm.acompletion(
            [{"role": "system", "content": JUDGE_SYSTEM_PROMPT},
             {"role": "user", "content": user}],
            model=judge_model(),
            temperature=0.0,
        )
        payload = _extract_json(result.response.choices[0].message.content or "")
    except Exception as exc:
        logger.warning("judgement skipped: %s: %s", type(exc).__name__, exc)
        return Judgement(skipped=True,
                         reason_skipped=f"{type(exc).__name__}: {exc}"[:200])

    if not payload:
        return Judgement(skipped=True, reason_skipped="reply was not JSON")

    return _coerce(payload, rubric, getattr(result, "cost_usd", 0.0))


def judge_model() -> str:
    """Fixed and separate from the models under test. Grading a model with
    itself measures its self-consistency, not its output."""
    return (settings.judge_model or "").strip() or settings.fallback_model


def _coerce(payload: Dict[str, Any], rubric: List[str], cost: float) -> Judgement:
    """
    Map a model-authored payload onto the rubric.

    A criterion the judge did not return is a **fail**, not a skip. The opposite
    default would let a truncated or lazy reply quietly raise the score by
    omitting the requirements it could not satisfy.
    """
    by_n: Dict[int, Dict[str, Any]] = {}
    for row in payload.get("scores") or []:
        if not isinstance(row, dict):
            continue
        try:
            by_n[int(row.get("n"))] = row
        except (TypeError, ValueError):
            continue

    scores: List[RubricScore] = []
    for i, criterion in enumerate(rubric):
        row = by_n.get(i + 1, {})
        verdict = str(row.get("verdict", "")).strip().lower()
        if verdict not in _MARKS:
            verdict = "fail"
        scores.append(RubricScore(
            n=i + 1, criterion=criterion, verdict=verdict,
            reason=str(row.get("reason", "") or "")[:300]
            or "The judge returned no score for this criterion."))

    quality = sum(_MARKS[s.verdict] for s in scores) / len(scores)
    return Judgement(scores=scores, quality=round(quality, 3), cost_usd=cost)
