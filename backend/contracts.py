"""
What an artifact has to be before the next step is allowed to read it.

Two patches in the orchestrator exist because this layer did not.
`_best_artifact` was written after an agent's closing chat message got filed as
the deliverable, and `_run_step` appends an explicit "Deliverable: write your
full output to <name>" instruction because a researcher once filed a 39-byte
stub as the research the rest of the mission was built on. Both make it more
likely that a good artifact is produced. Neither ever *checked* one.

So this does. A spec is inferred from the promised filename and the step's own
description, and `check` says whether what arrived satisfies it -- before the
step is marked complete and before anything downstream consumes it.

Everything here is a pure function: no model call, no file access, no clock. The
expensive judgement lives in `critic.py`; this layer is the cheap one that runs
on every artifact, and it has to stay fast enough that running it is never a
decision.
"""
import re
from dataclasses import dataclass, field
from typing import List, Optional

# The stub that started all of this. A real GPT-5.5 run filed exactly this
# string as `solar_efficiency_research.md` while the 2.4KB report the agent had
# actually written went to a loose path in backend/ -- and the next step
# consumed the sentence. base_agent still produces it as a last resort, so it
# is the first thing any artifact is tested against.
STUB_PATTERNS: List[str] = [
    r"Task completed after \d+ execution steps",
    r"Task halted by emergency stop",
    r"Task completed successfully\.?\s*$",
    r"lorem ipsum",
]

# Something that looks like a source: a bare URL, a markdown link, or a
# numbered reference marker. Deliberately loose -- the question is whether the
# agent grounded its claims at all, not whether it used a citation style.
_CITATION_RE = re.compile(r"https?://\S+|\[\d+\]|\[[^\]]+\]\(https?://", re.I)
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+\S", re.M)

# Filenames that promise a finished piece of writing rather than working notes.
_REPORT_HINTS = ("report", "briefing", "summary", "recommendation", "brief")
_NOTES_HINTS = ("notes", "scratch", "draft", "log")


@dataclass
class ArtifactSpec:
    """The bar one artifact has to clear."""
    min_chars: int = 200
    required_sections: List[str] = field(default_factory=list)
    must_not_match: List[str] = field(default_factory=lambda: list(STUB_PATTERNS))
    require_citations: bool = False
    # A finished document has structure; a set of notes need not. Kept separate
    # from `required_sections`, which names specific headings -- this only asks
    # that the artifact has any heading at all.
    require_heading: bool = False


@dataclass
class CheckResult:
    """The verdict, and what specifically was wrong."""
    ok: bool
    failures: List[str] = field(default_factory=list)
    severity: str = "none"          # none | minor | major

    def as_guidance(self) -> str:
        """The failures as an instruction the agent can act on. An agent told
        only that its work was rejected will produce the same thing again."""
        if not self.failures:
            return ""
        lines = ["Your previous attempt was rejected before it could be used. "
                 "Fix all of the following and write the file again:"]
        lines += [f"- {f}" for f in self.failures]
        return "\n".join(lines)


def infer_spec(
    filename: str,
    description: str = "",
    *,
    citations_expected: bool = False
) -> ArtifactSpec:
    """
    The bar for this particular artifact.

    Deliberately permissive by default. A spec that rejects reasonable work is
    worse than no spec at all -- it spends a repair attempt, and then the step
    completes anyway with the same artifact, having paid for nothing.

    `citations_expected` is passed in rather than guessed at: whether sources
    were even obtainable depends on the agent's tool grants and on whether a
    search key is configured, and neither is knowable from a filename. A
    contract that demands citations from an agent with no way to fetch one is a
    contract that fails every run offline.
    """
    name = (filename or "").lower()
    text = (description or "").lower()
    spec = ArtifactSpec()

    if any(hint in name for hint in _REPORT_HINTS):
        # The mission's deliverable. Longer, and structured.
        spec.min_chars = 500
        spec.require_heading = True
    elif any(hint in name for hint in _NOTES_HINTS):
        # Working notes feed a later step; they are allowed to be terse.
        spec.min_chars = 120

    if citations_expected and ("research" in name or "research" in text
                               or "source" in text or "cite" in text):
        spec.require_citations = True

    return spec


def check(artifact: Optional[str], spec: ArtifactSpec) -> CheckResult:
    """
    Measure one artifact against its spec.

    `ok` is false if anything failed; `severity` says how badly, so a caller
    that wants to spend its repair budget only on the serious cases can. A stub
    or an empty artifact is major -- the work is simply not there. A thin or
    unstructured one is minor: it exists, it is just weaker than promised.
    """
    body = (artifact or "").strip()
    failures: List[str] = []
    major = False

    if not body:
        return CheckResult(ok=False, severity="major",
                           failures=["The artifact is empty. Nothing was written."])

    for pattern in spec.must_not_match:
        if re.search(pattern, body, re.I):
            major = True
            failures.append(
                f"The artifact is a placeholder, not the work: it matches "
                f"'{pattern}'. Write the actual content to the file."
            )

    if len(body) < spec.min_chars:
        # Well under the bar means nothing was really attempted; just under it
        # means the work is thin. Worth separating, because only the first is
        # worth a whole extra dispatch.
        if len(body) * 2 < spec.min_chars:
            major = True
        failures.append(
            f"The artifact is {len(body)} characters; this deliverable needs at "
            f"least {spec.min_chars}. Expand it with real detail, not filler."
        )

    if spec.require_heading and not _HEADING_RE.search(body):
        failures.append(
            "The artifact has no Markdown heading. Give it a title and section "
            "headings so the next step can navigate it."
        )

    for section in spec.required_sections:
        if section.lower() not in body.lower():
            failures.append(
                f"The required section '{section}' is missing. Add it as a "
                f"Markdown heading."
            )

    if spec.require_citations and not _CITATION_RE.search(body):
        failures.append(
            "No sources are cited. Include the URLs you used, so a reader can "
            "check the claims."
        )

    if not failures:
        return CheckResult(ok=True, severity="none")
    return CheckResult(ok=False, failures=failures,
                       severity="major" if major else "minor")
