"""
Hiring a specialist the office does not have.

`planner.regrant_steps` has always been able to see this coming. It moves a
step to whoever holds the tools the step says it needs, and when nobody does it
says so and gives up:

    # Nobody holds the full set. Left where it is, loudly: the plan is
    # not repairable, and a silent reassignment to someone who is also
    # going to fail hides where it went wrong.

That is the right call when the roster is fixed. This module is what to do
instead when it is not: read the gap, hire someone for it, and let the step run.

Three rules, and the first two are the whole safety argument.

**A tool must exist.** The catalogue is `tool_registry.catalogue()`, which is
the authority on what the office can actually do, and a contractor is granted
from it and nothing else. Granting a name with no function behind it does not
create a capability; it creates an agent that calls a tool, gets nothing, and
writes a confident report around the hole.
This project has already met that failure and named it: *"the agent runs,
cannot call the tool, and writes a plausible report with an invented number in
it, which is the worst available outcome."* So a gap naming a tool nobody
implements is not hired for. It fails, saying what is missing -- which is a
request to go and build that tool, not something to route around.

**Exactly what the step declared.** Not a preset, not everything -- the tools
in `needs_tools`, which is what the plan said the work requires. That is both
tighter and more capable than a fixed allowance: nothing is pre-banned, and
nobody accumulates a tool they are not using. The permanent five hold their
tools for every mission forever; a contractor holds two for twenty minutes.

**Mission-scoped.** The hire lives in the mission that made it, is persisted
with that mission so a resume can rebuild it, and is never written to
`agents_dir`. A roster that grows by itself is one nobody curated, and every
future planner prompt would pay to list it.
"""
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

import llm
from config import settings
from schemas import AgentManifest, PlanStep
from tool_registry import tool_registry

logger = logging.getLogger("AgentHQ.Foundry")

# Borrowed art. There are five characters in the atlas and the packs that made
# them are gigabytes and not in the repository, so a contractor takes an
# existing sprite, hue-rotated so two hires never look alike, with a visitor
# badge drawn over it by the renderer.
_SPRITE_POOL = ("bob", "eve", "alice", "charlie")
_HUES = (200, 45, 300, 120)

# Names for the deterministic path. Chosen to be unlike the permanent five so
# nobody reading a transcript has to work out which are staff.
_NAMES = ("Nina", "Omar", "Priya", "Quinn", "Rosa", "Theo")

# What a tool is for, in a couple of words, for the offline fallback role.
# Anything not listed falls back to its own name.
_CAPABILITY = {
    "web_search": "web research",
    "fetch_url": "page retrieval",
    "read_file": "file reading",
    "write_file": "document authoring",
    "list_files": "workspace inspection",
    "search_workspace": "cross-document search",
    "query_data": "data querying",
    "calculate": "exact calculation",
    "code_analysis": "code inspection",
    "terminal": "command execution",
}

_SLUG_RE = re.compile(r"[^a-z0-9_]+")


@dataclass
class Gap:
    """One step the roster cannot staff."""
    step_id: str
    description: str
    # Tools that exist in the registry but which no single agent holds together.
    needed: List[str] = field(default_factory=list)
    # Tools named by the plan that no one implements. Not hireable.
    missing: List[str] = field(default_factory=list)

    @property
    def hireable(self) -> bool:
        return bool(self.needed) and not self.missing


def find_gaps(
    steps: Sequence[PlanStep],
    agent_tools: Dict[str, List[str]],
    catalogue: Optional[Dict[str, Any]] = None,
) -> List[Gap]:
    """
    Steps whose declared tools no single agent holds.

    Split into `needed` and `missing` on one question: does the tool exist?
    `regrant_steps` cannot ask it -- it only knows what the roster holds, so a
    tool nobody has been granted and a tool nobody implements look identical
    from there, and it treats both as invented. With the registry's catalogue
    the two come apart, and they call for opposite responses: hire for the
    first, fail loudly on the second.
    """
    catalogue = tool_registry.catalogue() if catalogue is None else catalogue
    held = {aid: set(tools or []) for aid, tools in agent_tools.items()}

    gaps: List[Gap] = []
    for step in steps:
        declared = [t for t in (step.needs_tools or [])]
        if not declared:
            continue
        missing = [t for t in declared if t not in catalogue]
        real = [t for t in declared if t in catalogue]
        if not real and not missing:
            continue
        if not missing and any(set(real) <= tools for tools in held.values()):
            continue    # somebody can already do it
        gaps.append(Gap(
            step_id=step.id,
            description=step.description,
            needed=sorted(set(real)),
            missing=sorted(set(missing)),
        ))
    return gaps


def _slug(text: str, fallback: str) -> str:
    out = _SLUG_RE.sub("_", (text or "").strip().lower()).strip("_")
    return out[:40] or fallback


def _unique_id(base: str, taken: Sequence[str]) -> str:
    if base not in taken:
        return base
    for n in range(2, 50):
        candidate = f"{base}_{n}"
        if candidate not in taken:
            return candidate
    return f"{base}_{len(taken)}"


def _fallback_manifest(gap: Gap, taken: Sequence[str], index: int) -> AgentManifest:
    """
    A contractor designed without a model.

    Runs when there is no credential, or when the design call fails or comes
    back malformed. It has to exist: the no-key path is the configuration most
    likely to be someone's first look at this project, and a feature that only
    works with a key would be invisible there. Deterministic, so the same gap
    hires the same specialist twice.
    """
    skills = [_CAPABILITY.get(t, t.replace("_", " ")) for t in gap.needed]
    role = " & ".join(s.title() for s in skills[:3]) or "Specialist"
    name = _NAMES[index % len(_NAMES)]
    return AgentManifest(
        id=_unique_id(_slug(role, "contractor"), taken),
        name=name,
        role=role,
        plate_verb="Contracting",
        plate_icon="🪪",
        avatar_emoji="🧩",
        color="#a78bfa",
        system_prompt=(
            f"You are a contract specialist brought in for one task because no "
            f"one in the office holds the tools it needs. Your speciality is "
            f"{role.lower()}. You have exactly these tools: {', '.join(gap.needed)}. "
            f"Use them to do the work you are given and write your findings to "
            f"the file you are asked for. If a tool fails, say so plainly in "
            f"your output rather than working around it -- a stated gap is worth "
            f"more than a confident guess."
        ),
        tools=list(gap.needed),
    )


def _design_prompt(goal: str, gap: Gap) -> List[Dict[str, str]]:
    return [
        {"role": "system", "content": (
            "You write the job description for a contract specialist joining an "
            "office of AI agents for a single task. Reply with JSON only, no "
            "prose and no code fence, with exactly these keys:\n"
            '  {"name": "<a short human first name>",\n'
            '   "role": "<3-6 words, the speciality>",\n'
            '   "plate_verb": "<1-2 words, what they are doing, e.g. Fetching>",\n'
            '   "system_prompt": "<2-4 sentences, second person, telling them '
            'their speciality and how to work>"}\n'
            "The system_prompt must instruct them to report a tool failure "
            "plainly rather than working around it. Do not mention tools they "
            "were not given. Do not invent tools."
        )},
        {"role": "user", "content": (
            f"Mission goal: {goal}\n\n"
            f"The step nobody can staff: {gap.description}\n\n"
            f"The tools they will hold, and the only ones they will have: "
            f"{', '.join(gap.needed)}"
        )},
    ]


async def design(goal: str, gap: Gap, taken: Sequence[str], index: int) -> AgentManifest:
    """
    A manifest for the contractor this gap needs.

    Bounded the way `critic` and `synthesis` are: one call, no tools, the small
    model, a low token ceiling. The model writes the identity -- the name, the
    speciality, how to work -- and touches nothing that matters for safety. The
    tool list is `gap.needed`, set here and never read back from the reply, so
    a model that names `terminal` in its JSON does not get `terminal`.

    Never raises. A design that fails falls back to the deterministic one,
    because failing to think of a good name is not a reason to fail the step.
    """
    fallback = _fallback_manifest(gap, taken, index)
    if not settings.foundry_design_enabled:
        return fallback

    model = settings.foundry_model or settings.fallback_model
    try:
        result = await llm.acompletion(
            _design_prompt(goal, gap),
            model=model,
            temperature=0.3,
            max_tokens=settings.foundry_max_tokens,
        )
        content = (result.response.choices[0].message.content or "").strip()
    except Exception as exc:                                   # noqa: BLE001
        logger.info("foundry design call failed (%s); using the offline design", exc)
        return fallback

    # A fenced reply is the commonest malformation and is cheap to survive.
    if content.startswith("```"):
        content = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", content).strip()

    try:
        raw = json.loads(content)
    except (ValueError, TypeError):
        logger.info("foundry design was not JSON; using the offline design")
        return fallback
    if not isinstance(raw, dict):
        return fallback

    name = str(raw.get("name") or fallback.name).strip()[:24]
    role = str(raw.get("role") or fallback.role).strip()[:60]
    prompt = str(raw.get("system_prompt") or fallback.system_prompt).strip()
    if len(prompt) < 40:
        # Too short to be a brief. The deterministic one says more.
        prompt = fallback.system_prompt

    return AgentManifest(
        id=_unique_id(_slug(role, "contractor"), taken),
        name=name or fallback.name,
        role=role,
        plate_verb=str(raw.get("plate_verb") or "Contracting").strip()[:18] or "Contracting",
        plate_icon="🪪",
        avatar_emoji="🧩",
        color="#a78bfa",
        system_prompt=prompt,
        # Never from the reply. The one field the model does not get a say in.
        tools=list(gap.needed),
    )


async def hire(
    goal: str,
    gaps: Sequence[Gap],
    existing_ids: Sequence[str],
    mission_id: str,
    seats_available: int,
) -> tuple:
    """
    Design a contractor for each hireable gap.

    Returns `(manifests, refusals)`. A refusal is a gap that was not hired for
    and the reason, which the caller surfaces -- an unstaffed step that fails
    silently is the thing this whole module exists to stop.
    """
    manifests: List[AgentManifest] = []
    refusals: List[str] = []
    taken = list(existing_ids)

    budget = min(settings.max_spawned_agents, max(0, seats_available))

    for index, gap in enumerate(gaps):
        if gap.missing:
            refusals.append(
                f"step {gap.step_id} needs {', '.join(gap.missing)}, which no tool "
                f"provides. Add a tool for it in backend/tools/, or change the plan. "
                f"Hiring someone and granting them a tool that does not exist would "
                f"produce an agent that invents the answer.")
            continue
        if not gap.hireable:
            continue
        if len(manifests) >= budget:
            refusals.append(
                f"step {gap.step_id} needs {', '.join(gap.needed)} and nobody holds "
                f"them, but the office is full: {budget} contractor(s) allowed "
                f"({settings.max_spawned_agents} by policy, {seats_available} free "
                f"desk(s)).")
            continue

        manifest = await design(goal, gap, taken, index)
        manifest.spawned = True
        manifest.spawned_for = gap.step_id
        manifest.spawned_reason = ", ".join(gap.needed)
        manifest.mission_id = mission_id
        manifest.sprite = _SPRITE_POOL[len(manifests) % len(_SPRITE_POOL)]
        manifest.sprite_hue = _HUES[len(manifests) % len(_HUES)]
        taken.append(manifest.id)
        manifests.append(manifest)
        logger.info("foundry hired %s (%s) for step %s, holding %s",
                    manifest.id, manifest.name, gap.step_id, manifest.tools)

    return manifests, refusals
