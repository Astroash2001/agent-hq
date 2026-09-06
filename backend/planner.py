"""
LLM task planner.

Replaces the keyword buckets that used to live in `orchestrator._decompose_task`
with a real planning call that returns a validated dependency graph. The buckets
survive as `heuristic_plan` and are still the fallback whenever there is no API
key, the call fails, or the model returns something that will not validate --
the same "always works without a key" property `base_agent` has.

A plan is a list of `PlanStep`s. Each step names the agent that runs it, the
steps it waits on, the artifact it produces and the artifacts it consumes. Those
last two are what `workspace.py` uses to feed one specialist's output into the
next one's context, and what the office draws as objects moving between desks.
"""
import json
import logging
import re
from typing import Any, Dict, List, Optional

import llm
from config import date_context, settings, llm_configured
from event_bus import event_bus
from schemas import Event, EventType, Plan, PlanStep

logger = logging.getLogger("AgentHQ.Planner")


PLANNER_SYSTEM_PROMPT = """You are the planning module of AgentHQ, a multi-agent \
harness. You are given a user goal and a roster of specialist agents. Break the \
goal into the SMALLEST number of subtasks that genuinely need different \
specialists.

Rules:
- Reply with JSON only. No prose, no markdown fence.
- Shape: {"steps": [{"id": "s1", "agent_id": "...", "description": "...", \
"dependencies": [], "produces": "name.md", "consumes": [], "rationale": "..."}]}
- `agent_id` MUST be one of the roster ids given below.
- `id` values are s1, s2, s3... in order.
- `dependencies` lists the ids of steps that must finish first. Use an EMPTY \
list for any step that can start immediately -- independent steps run in \
parallel, so do not chain steps that do not actually depend on each other.
- `produces` is a short snake_case artifact filename this step writes.
- `consumes` lists artifact filenames produced by the steps in `dependencies`.
- `description` is an instruction addressed to that specialist, concrete enough \
to act on without seeing the original goal.
- `rationale` is one short clause explaining why this specialist.
- `needs_tools` lists the tools this step cannot be done without, chosen from the \
tools listed against the agents below. Leave it empty when the step is reasoning \
or writing and needs no tool. Name a tool only when the work is impossible \
without it -- measuring a runtime needs `terminal`, reporting a current fact needs \
`web_search`. The agent you pick must hold every tool you name.
- Use at most %d steps. Prefer fewer. A trivial goal is one step.

MEETINGS
You may make any step a meeting by adding "kind": "meeting" and "attendees": ["agent_id", ...]. A meeting gathers people at the conference table instead of one specialist working alone at their desk.

- `agent_id` on a meeting is whoever CHAIRS it and writes the decision down. `attendees` are the others who join. Do not repeat the chair in `attendees`.
- At the table, and nowhere else, the chair can call `review_artifacts` to read what everyone brought, and `record_decision` to write down what was agreed.
- The table seats %d people in total, chair included.
- Call one when the goal genuinely needs people together: reconciling two findings that may conflict, agreeing an approach before work starts, or reviewing several outputs as a group. A meeting must depend on the steps whose work it discusses -- there is no point meeting before there is anything to meet about.
- Do NOT call one to simply pass a file from one agent to the next. That already happens automatically through `produces` and `consumes`.
- If the user explicitly asks for a meeting, discussion, review or standup, give them one."""


def _roster_block(roster: List[Dict[str, Any]]) -> str:
    """Render the agent roster the planner is allowed to choose from."""
    lines = []
    for a in roster:
        tools = ", ".join(a.get("tools") or []) or "none"
        stats = a.get("stats")
        suffix = ""
        if stats and stats.get("runs"):
            suffix = (f"  [history: {stats['runs']} runs, "
                      f"{int(stats['success_rate'] * 100)}% success]")
        lines.append(f"- {a['id']} ({a['name']}) -- {a['role']}. tools: {tools}{suffix}")
    return "\n".join(lines)


def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    """Pull a JSON object out of a model response, fenced or not."""
    if not text:
        return None
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    if not text.startswith("{"):
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1:
            return None
        text = text[start:end + 1]
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        logger.debug(f"Planner JSON decode failed: {e}")
        return None


class PlanValidationError(ValueError):
    """Raised when a model-authored plan cannot be repaired into a runnable DAG."""


def regrant_steps(
    steps: List[PlanStep],
    valid_agent_ids: List[str],
    agent_tools: Optional[Dict[str, List[str]]] = None,
) -> List[PlanStep]:
    """
    Move any step to an agent that holds the tools the step says it needs.

    The planner prompt has always listed each agent's tools; nothing ever
    checked that the model read them. A step declaring `needs_tools:
    ["terminal"]` assigned to a specialist holding only write_file does not fail
    cleanly -- the agent runs, cannot call the tool, and writes a plausible
    report with an invented number in it, which is the worst available outcome.

    Repaired rather than rejected: the plan is right about the work and wrong
    only about who does it, so moving the step is a smaller change than throwing
    away the graph.

    Shared by both planners. `heuristic_plan` does not go through
    `validate_plan`, so without this being callable on its own the fallback
    planner was the one path where a tool mismatch still went unchecked -- and
    the fallback is what runs when there is no key, which is the configuration
    most likely to be someone's first look at this.

    Mutates `steps` and returns them, for use as an expression.
    """
    if not agent_tools:
        return steps

    granted = {aid: set(tools or []) for aid, tools in agent_tools.items()}
    real_tools = set().union(*granted.values()) if granted else set()
    for step in steps:
        # A tool nobody in the office holds is, operationally, one the model
        # invented: it is ignored rather than treated as a requirement, or every
        # step naming one goes to whichever agent sorts first.
        needed = {t for t in step.needs_tools if t in real_tools}
        if not needed or needed <= granted.get(step.agent_id, set()):
            continue
        candidates = [aid for aid in valid_agent_ids
                      if needed <= granted.get(aid, set())]
        if not candidates:
            # Nobody holds the full set. Left where it is, loudly: the plan is
            # not repairable, and a silent reassignment to someone who is also
            # going to fail hides where it went wrong.
            logger.warning(
                "Step %s needs %s and no agent holds all of them; left with %s",
                step.id, sorted(needed), step.agent_id)
            continue
        # Prefer whoever the plan already trusts, so a multi-step plan does not
        # scatter across the roster for one tool.
        in_plan = [c for c in candidates if c in {s.agent_id for s in steps}]
        chosen = (in_plan or candidates)[0]
        logger.info("Step %s regranted %s -> %s (needs %s)",
                    step.id, step.agent_id, chosen, sorted(needed))
        step.regranted_from = step.agent_id
        step.agent_id = chosen

    return steps


def validate_plan(
    raw: Dict[str, Any],
    valid_agent_ids: List[str],
    agent_tools: Optional[Dict[str, List[str]]] = None,
) -> List[PlanStep]:
    """
    Turn a raw planner payload into runnable steps, or raise.

    Repairs what is safely repairable (renumbers ids, drops dangling
    dependencies, trims to the step cap, moves a step to an agent that holds the
    tools it declared it needs) and rejects what is not (no steps, no
    recognisable agent).

    `agent_tools` maps agent id to the tools that agent actually holds. Omitted,
    the tool pass is skipped entirely and this behaves as it did before -- the
    planner prompt lists tools but nothing downstream had ever checked the model
    took any notice, which is how a step needing `terminal` could be handed to a
    specialist holding only `write_file`.
    """
    steps_raw = raw.get("steps")
    if not isinstance(steps_raw, list) or not steps_raw:
        raise PlanValidationError("payload has no 'steps' list")

    steps_raw = steps_raw[:settings.planner_max_steps]

    # Pass 1: build steps with canonical ids, remembering what the model called them.
    alias: Dict[str, str] = {}
    steps: List[PlanStep] = []
    for i, item in enumerate(steps_raw):
        if not isinstance(item, dict):
            continue
        agent_id = str(item.get("agent_id", "")).strip()
        description = str(item.get("description", "")).strip()
        if agent_id not in valid_agent_ids or not description:
            logger.debug(f"Planner step {i} dropped (agent '{agent_id}' unknown or empty description)")
            continue

        canonical = f"s{len(steps) + 1}"
        original = str(item.get("id", canonical)).strip() or canonical
        alias[original] = canonical

        produces = str(item.get("produces") or "").strip()
        if not produces:
            produces = f"{agent_id}_output.md"

        # Meetings. The model is free to call one; these are the guardrails on
        # what it may hand back. An attendee it invented, or itself twice, or a
        # room of nine people would all break the floor, so they are repaired
        # here rather than trusted.
        kind = "meeting" if str(item.get("kind", "")).strip().lower() == "meeting" else "work"
        attendees: List[str] = []
        if kind == "meeting":
            for a in (item.get("attendees") or []):
                a = str(a).strip()
                if a in valid_agent_ids and a != agent_id and a not in attendees:
                    attendees.append(a)
            # The chair occupies a seat too, so the guest list is capacity - 1.
            attendees = attendees[:max(0, settings.meeting_capacity - 1)]
            if not attendees:
                # A meeting of one is just work. Demote rather than reject, so a
                # near-miss does not cost the whole plan.
                kind = "work"

        steps.append(PlanStep(
            id=canonical,
            agent_id=agent_id,
            description=description,
            dependencies=[str(d) for d in (item.get("dependencies") or []) if d],
            produces=produces,
            consumes=[str(c) for c in (item.get("consumes") or []) if c],
            rationale=str(item.get("rationale") or "").strip(),
            kind=kind,
            attendees=attendees,
            needs_tools=[str(t).strip() for t in (item.get("needs_tools") or [])
                         if str(t).strip()],
        ))

    if not steps:
        raise PlanValidationError("no step named a known agent")

    # Pass 2: remap dependencies onto canonical ids and drop anything dangling or
    # forward-pointing. Only backward references survive, which is also what
    # guarantees the graph is acyclic.
    known: Dict[str, int] = {s.id: idx for idx, s in enumerate(steps)}
    for step in steps:
        idx = known[step.id]
        deps = []
        for dep in step.dependencies:
            mapped = alias.get(dep, dep)
            if mapped in known and known[mapped] < idx and mapped not in deps:
                deps.append(mapped)
        step.dependencies = deps

    # Pass 3: a step goes to someone who can actually do it.
    regrant_steps(steps, valid_agent_ids, agent_tools)

    # Pass 4: a step may only consume artifacts its dependencies actually produce,
    # and it always receives all of them even if the model forgot to ask.
    for step in steps:
        upstream = [steps[known[d]].produces for d in step.dependencies]
        step.consumes = [c for c in step.consumes if c in upstream]
        for artifact in upstream:
            if artifact not in step.consumes:
                step.consumes.append(artifact)

    return steps


class Planner:
    """Produces an execution DAG for a user goal."""

    def __init__(self):
        self.last_source: str = "heuristic"

    @staticmethod
    async def _on_retry(note: str, kind: str) -> None:
        """Narrate a retry to the terminal. Planning gates the whole mission,
        so a silent 28s rate-limit wait is indistinguishable from a hang."""
        await event_bus.emit(Event(
            type=EventType.TERMINAL_LOG,
            data={"message": f"Planner: {note}",
                  "level": "ERROR" if kind == "fallback" else "INFO"}
        ))

    async def plan(self, goal: str, roster: List[Dict[str, Any]],
                   attachments: Optional[List[str]] = None) -> Plan:
        """
        Plan `goal` against `roster`.

        A malformed reply is retried once and then falls back to the heuristic
        buckets. A failed CALL (auth, routing, timeout) raises when
        settings.llm_strict is on, because a silent fallback there turns a
        misconfigured gateway into a mission that looks like it worked.
        """
        valid_ids = [a["id"] for a in roster]
        agent_tools = {a["id"]: list(a.get("tools") or []) for a in roster}
        attachments = list(attachments or [])

        if settings.planner_enabled and self._has_key():
            for attempt in (1, 2):
                try:
                    raw = await self._call_planner_llm(
                        goal, roster, attempt, attachments)
                    if raw is None:
                        raise PlanValidationError("no JSON object in reply")
                    steps = validate_plan(raw, valid_ids, agent_tools)
                    self.last_source = "llm"
                    logger.info(f"Planner produced {len(steps)} LLM steps for: {goal[:60]}")
                    return Plan(goal=goal, steps=steps, source="llm")
                except PlanValidationError as e:
                    # The model answered but the plan was malformed -- a real
                    # planning failure, worth a retry then the buckets.
                    logger.warning(f"Planner attempt {attempt} unusable ({e}); "
                                   f"{'retrying' if attempt == 1 else 'falling back to heuristic'}")
                except Exception as e:
                    # The call itself failed: credentials, routing, timeout.
                    # Retrying cannot fix that, and silently returning keyword
                    # buckets makes a broken configuration look like a working
                    # mission -- which is exactly how a bad gateway setup hides.
                    logger.error(f"Planner LLM call failed: {type(e).__name__}: {e}")
                    if settings.llm_strict:
                        raise
                    logger.warning("Falling back to heuristic buckets.")
                    break

        self.last_source = "heuristic"
        # Regranted like an LLM plan. The buckets name their agents by hand and
        # are usually right, but "usually right and never checked" is what the
        # tool-grant pass exists to stop -- and this is the path that runs when
        # there is no key, which is the configuration most likely to be somebody's
        # first look at the harness.
        steps = regrant_steps(heuristic_plan(goal, valid_ids), valid_ids, agent_tools)
        return Plan(goal=goal, steps=steps, source="heuristic")

    @staticmethod
    def _has_key() -> bool:
        return llm_configured()

    async def _call_planner_llm(
        self,
        goal: str,
        roster: List[Dict[str, Any]],
        attempt: int,
        attachments: Optional[List[str]] = None
    ) -> Optional[Dict[str, Any]]:
        """One planning call. Returns the parsed payload or None."""
        # Prepended rather than folded into the template: PLANNER_SYSTEM_PROMPT
        # is %-formatted, and a date carrying a stray % would break it. The
        # planner needs the date as much as an agent does -- it is the step
        # that decides whether a goal is about current events at all.
        system = date_context() + "\n\n" + PLANNER_SYSTEM_PROMPT % (
            settings.planner_max_steps, settings.meeting_capacity)
        # Attachments are already artifacts in the workspace by the time the
        # planner runs, so it is told they exist rather than told to fetch
        # them. Naming one in `consumes` is all it takes to put the file on a
        # specialist's desk -- the same machinery that hands one step's output
        # to the next, which is why attachments needed no new plumbing.
        attached_block = ""
        if attachments:
            attached_block = (
                "\nFiles the user attached to this goal, already in the "
                "workspace:\n"
                + "\n".join(f"- {name}" for name in attachments)
                + "\nThe goal is probably ABOUT these. Put them in the "
                  "`consumes` list of whichever step needs to read them; a "
                  "step that consumes one gets its full text on the desk.\n")

        user = (
            f"Available specialists:\n{_roster_block(roster)}\n"
            f"{attached_block}\n"
            f"User goal:\n{goal}\n\n"
            f"Return the JSON plan."
        )
        if attempt > 1:
            user += ("\n\nYour previous reply was not valid. Return ONLY a JSON object "
                     "with a 'steps' array. Every agent_id must come from the roster above.")

        # Through llm.acompletion so the planner gets the same retry and
        # fallback as an agent step. It gates the entire mission -- a single
        # transient 429 here used to mean no plan at all.
        result = await llm.acompletion(
            [{"role": "system", "content": system},
             {"role": "user", "content": user}],
            temperature=settings.planner_temperature,
            timeout=settings.planner_timeout,
            on_retry=self._on_retry,
        )
        # `.response` is the provider object; the rest of the LLMResult is the
        # cost of this call, which the mission's trace already recorded as an
        # llm span. Planning is not free and the mission total should say so.
        content = result.response.choices[0].message.content or ""
        return _extract_json(content)


# --------------------------------------------------------------- goal shape

# The buckets below decide WHO does the work. These decide HOW MUCH work the
# goal is worth, which the buckets had no notion of at all: every goal, from
# "What does SSD stand for?" to a full research brief, fanned out to four
# specialists. The eval suite is what made that visible and is what these are
# measured by -- `adversarial_trivial` and `adversarial_ambiguous` exist for
# exactly this, and `approach_standup` and `battery_team_review` for the
# meeting case.

# A question that wants a fact back, not a project.
_TRIVIAL_RE = re.compile(
    r"\b(stands? for|acronym|abbreviation|initialism|define|definition of|"
    r"meaning of|what does .{1,40} mean)\b", re.I)

# Anything that turns a question into a piece of work, whatever its length.
_MULTIPART_RE = re.compile(
    r"\b(then|compare|contrast|report|brief|briefing|guide|plan|implement|"
    r"build|test|analyse|analyze|research|write up|recommend|and write)\b", re.I)

# The goal explicitly asks for people in a room, which is the one case the
# buckets must not answer with a chain of solo desks.
_MEETING_RE = re.compile(
    r"\b(standup|stand-up|meeting|huddle|kick-?off|retro(?:spective)?|"
    r"discuss|debate|as a team|round the table|together before)\b", re.I)


def _is_trivial(goal: str) -> bool:
    """
    True for a goal one agent can answer in a paragraph.

    Deliberately narrow. A false negative costs money -- a definition fanned out
    to four specialists -- and that is the failure this was written to fix. A
    false positive is worse: it answers a real research goal with one shallow
    step, and nothing downstream can tell it was under-planned. So it needs
    both a definitional phrase or a bare question AND no sign of a deliverable.
    """
    text = goal.strip()
    if len(text.split()) > 14 or _MULTIPART_RE.search(text):
        return False
    if _TRIVIAL_RE.search(text):
        return True
    return bool(re.match(r"^(what|who|when|where|which|how many|how much)\b", text, re.I)
                and text.endswith("?"))


def _is_vague(goal: str) -> bool:
    """
    True for a goal too thin to plan against -- "Make it better."

    A four-step fan-out on a goal with no subject is four specialists inventing
    four different subjects. Two steps is enough to establish what was meant and
    write something back, and the answer says what it assumed.
    """
    words = goal.strip().rstrip("?.!").split()
    return len(words) <= 5 and not _MULTIPART_RE.search(goal)


def _deliverable_name(goal: str) -> str:
    """
    Name the final artifact after what the user asked for.

    `briefing.md` was hardcoded, so a goal asking for a report produced a
    briefing and a goal asking for a guide produced a briefing. The name is the
    first thing anyone reads in the mission directory, and it costs nothing to
    make it the word they used.
    """
    g = goal.lower()
    if re.search(r"\b(brief|briefing|executive|memo)\b", g):
        return "briefing.md"
    if re.search(r"\bguide\b", g):
        return "guide.md"
    if re.search(r"\b(summary|summarise|summarize)\b", g):
        return "summary.md"
    return "report.md"


def heuristic_plan(goal: str, valid_agent_ids: List[str]) -> List[PlanStep]:
    """
    The original four intent buckets, kept as the no-key / failure fallback.

    Unlike the version this replaced, the buckets declare real dependencies, so
    the middle of each plan fans out and the scheduler has something to run in
    parallel. Steps naming an agent that is not loaded are dropped.

    The buckets now sit behind three questions about the goal itself -- is it
    trivial, is it too vague to plan, does it ask for a meeting -- because a
    router that only ever answers "four steps" is not a planner, it is a
    constant.
    """
    g = goal.lower()

    def step(sid, agent, desc, deps=(), produces="", consumes=(), why="",
             kind="work", attendees=(), needs=()):
        return PlanStep(
            id=sid, agent_id=agent, description=desc,
            dependencies=list(deps), produces=produces or f"{agent}_output.md",
            consumes=list(consumes), rationale=why,
            kind=kind, attendees=list(attendees), needs_tools=list(needs)
        )

    deliverable = _deliverable_name(goal)

    if _is_trivial(goal):
        raw = [
            step("s1", "creative",
                 f"Answer this directly and briefly, in your own knowledge: {goal}\n"
                 f"One short paragraph. Do not pad it into a report, do not add "
                 f"sections, and do not research it.",
                 produces="answer.md", why="a definition needs an answer, not a project"),
        ]
    elif _is_vague(goal):
        raw = [
            step("s1", "researcher",
                 f"The request is \"{goal}\", which does not say what it refers to. "
                 f"State the most reasonable reading, name the assumption you are "
                 f"making, and gather only what that reading needs.",
                 produces="research_notes.md",
                 why="a goal with no subject needs one established before anyone works"),
            step("s2", "creative",
                 "Write the answer to the reading established above, opening with "
                 "the assumption it rests on so the reader can correct it.",
                 deps=["s1"], produces=deliverable, consumes=["research_notes.md"],
                 why="one write-up against one stated assumption"),
        ]
    elif _MEETING_RE.search(g):
        # The goal asks for people in a room. The buckets never produced a
        # meeting step, so a harness that supports meetings answered "hold a
        # standup" with four solo desks.
        raw = [
            step("s1", "researcher", f"Research the first side of: {goal}",
                 produces="research_notes.md", why="one half of the evidence",
                 needs=["web_search"]),
            step("s2", "data_analyst",
                 f"Quantify and sanity-check the numbers behind: {goal}",
                 produces="analysis.md", why="the other half, gathered in parallel"),
            step("s3", "creative",
                 "Chair the discussion: read what everyone brought, get the "
                 "disagreements out, and record what the room agreed.",
                 deps=["s1", "s2"], produces="decision.md",
                 consumes=["research_notes.md", "analysis.md"],
                 kind="meeting", attendees=["researcher", "data_analyst"],
                 why="the goal asked for this to be settled together"),
            step("s4", "creative",
                 "Write the deliverable from the recorded decision and the work "
                 "behind it, following the angle the room agreed.",
                 deps=["s3"], produces=deliverable, consumes=["decision.md"],
                 why="write-up follows the decision, not the other way round"),
        ]
    elif any(w in g for w in ["code", "bug", "python", "fix", "script", "test", "build", "api", "fibonacci"]):
        raw = [
            step("s1", "researcher", f"Gather technical specifications and best-practice patterns for: {goal}",
                 produces="research_notes.md", why="needs sources before anyone writes code",
                 needs=["web_search"]),
            step("s2", "coder", f"Implement the code, run AST verification and execute tests for: {goal}",
                 deps=["s1"], produces="implementation.md", consumes=["research_notes.md"],
                 why="owns the terminal and AST tooling", needs=["terminal"]),
            step("s3", "data_analyst", f"Inspect the workspace and check inputs, edge cases and data shapes for: {goal}",
                 deps=["s1"], produces="analysis.md", consumes=["research_notes.md"],
                 why="can validate in parallel with implementation"),
            step("s4", "creative", "Write the documentation, walkthrough and usage examples from the work so far",
                 deps=["s2", "s3"], produces=deliverable,
                 consumes=["implementation.md", "analysis.md"], why="final prose pass"),
        ]
    elif any(w in g for w in ["job", "resume", "interview", "hire", "career", "salary"]):
        raw = [
            step("s1", "researcher", f"Scan role requirements, skill criteria and interview topics for: {goal}",
                 produces="market_scan.md", why="market research first",
                 needs=["web_search"]),
            step("s2", "coder", "Work through the technical and system-design interview questions this role implies",
                 deps=["s1"], produces="tech_prep.md", consumes=["market_scan.md"],
                 why="technical depth"),
            step("s3", "creative", "Draft tailored resume bullets and a personalised application pitch",
                 deps=["s1"], produces="pitch.md", consumes=["market_scan.md"],
                 why="writing, independent of the technical prep"),
        ]
    elif any(w in g for w in ["data", "sql", "file", "csv", "query", "database", "analytics"]):
        raw = [
            step("s1", "data_analyst", f"Inspect the data directory and validate the query schema for: {goal}",
                 produces="analysis.md", why="owns the data tooling",
                 needs=["list_files"]),
            step("s2", "researcher", f"Find the documented conventions and pitfalls relevant to: {goal}",
                 produces="research_notes.md", why="context, independent of the data pass",
                 needs=["web_search"]),
            step("s3", "creative", "Format the analytical report with key insights and a recommendation",
                 deps=["s1", "s2"], produces=deliverable,
                 consumes=["analysis.md", "research_notes.md"], why="fan-in write-up"),
        ]
    else:
        raw = [
            step("s1", "researcher", f"Investigate the core concepts and documentation for: {goal}",
                 produces="research_notes.md", why="establish the ground truth",
                 needs=["web_search"]),
            step("s2", "coder", f"Prototype a validation script and probe the edge cases for: {goal}",
                 deps=["s1"], produces="implementation.md", consumes=["research_notes.md"],
                 why="empirical check", needs=["terminal"]),
            step("s3", "data_analyst", f"Quantify the trade-offs and sanity-check the numbers behind: {goal}",
                 deps=["s1"], produces="analysis.md", consumes=["research_notes.md"],
                 why="runs alongside the prototype"),
            step("s4", "creative", "Summarise the findings, action items and next steps into an executive briefing",
                 deps=["s2", "s3"], produces=deliverable,
                 consumes=["implementation.md", "analysis.md"], why="final synthesis"),
        ]

    steps = [s for s in raw if s.agent_id in valid_agent_ids]
    if not steps:
        # Nothing in the bucket is loaded: give the work to whoever exists.
        fallback = valid_agent_ids[0] if valid_agent_ids else "researcher"
        return [step("s1", fallback, goal, produces=deliverable)]

    # Dropping a step can orphan a dependency; re-point what survived.
    kept = {s.id for s in steps}
    for s in steps:
        s.dependencies = [d for d in s.dependencies if d in kept]
        upstream = {x.produces for x in steps if x.id in s.dependencies}
        s.consumes = [c for c in s.consumes if c in upstream]
    return steps


planner = Planner()
