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


def validate_plan(raw: Dict[str, Any], valid_agent_ids: List[str]) -> List[PlanStep]:
    """
    Turn a raw planner payload into runnable steps, or raise.

    Repairs what is safely repairable (renumbers ids, drops dangling
    dependencies, trims to the step cap) and rejects what is not (no steps, no
    recognisable agent).
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
            attendees=attendees
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

    # Pass 3: a step may only consume artifacts its dependencies actually produce,
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

    async def plan(self, goal: str, roster: List[Dict[str, Any]]) -> Plan:
        """
        Plan `goal` against `roster`.

        A malformed reply is retried once and then falls back to the heuristic
        buckets. A failed CALL (auth, routing, timeout) raises when
        settings.llm_strict is on, because a silent fallback there turns a
        misconfigured gateway into a mission that looks like it worked.
        """
        valid_ids = [a["id"] for a in roster]

        if settings.planner_enabled and self._has_key():
            for attempt in (1, 2):
                try:
                    raw = await self._call_planner_llm(goal, roster, attempt)
                    if raw is None:
                        raise PlanValidationError("no JSON object in reply")
                    steps = validate_plan(raw, valid_ids)
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
        steps = heuristic_plan(goal, valid_ids)
        return Plan(goal=goal, steps=steps, source="heuristic")

    @staticmethod
    def _has_key() -> bool:
        return llm_configured()

    async def _call_planner_llm(
        self,
        goal: str,
        roster: List[Dict[str, Any]],
        attempt: int
    ) -> Optional[Dict[str, Any]]:
        """One planning call. Returns the parsed payload or None."""
        # Prepended rather than folded into the template: PLANNER_SYSTEM_PROMPT
        # is %-formatted, and a date carrying a stray % would break it. The
        # planner needs the date as much as an agent does -- it is the step
        # that decides whether a goal is about current events at all.
        system = date_context() + "\n\n" + PLANNER_SYSTEM_PROMPT % (
            settings.planner_max_steps, settings.meeting_capacity)
        user = (
            f"Available specialists:\n{_roster_block(roster)}\n\n"
            f"User goal:\n{goal}\n\n"
            f"Return the JSON plan."
        )
        if attempt > 1:
            user += ("\n\nYour previous reply was not valid. Return ONLY a JSON object "
                     "with a 'steps' array. Every agent_id must come from the roster above.")

        # Through llm.acompletion so the planner gets the same retry and
        # fallback as an agent step. It gates the entire mission -- a single
        # transient 429 here used to mean no plan at all.
        response = await llm.acompletion(
            [{"role": "system", "content": system},
             {"role": "user", "content": user}],
            temperature=settings.planner_temperature,
            timeout=settings.planner_timeout,
            on_retry=self._on_retry,
        )
        content = response.choices[0].message.content or ""
        return _extract_json(content)


def heuristic_plan(goal: str, valid_agent_ids: List[str]) -> List[PlanStep]:
    """
    The original four intent buckets, kept as the no-key / failure fallback.

    Unlike the version this replaced, the buckets declare real dependencies, so
    the middle of each plan fans out and the scheduler has something to run in
    parallel. Steps naming an agent that is not loaded are dropped.
    """
    g = goal.lower()

    def step(sid, agent, desc, deps=(), produces="", consumes=(), why=""):
        return PlanStep(
            id=sid, agent_id=agent, description=desc,
            dependencies=list(deps), produces=produces or f"{agent}_output.md",
            consumes=list(consumes), rationale=why
        )

    if any(w in g for w in ["code", "bug", "python", "fix", "script", "test", "build", "api", "fibonacci"]):
        raw = [
            step("s1", "researcher", f"Gather technical specifications and best-practice patterns for: {goal}",
                 produces="research_notes.md", why="needs sources before anyone writes code"),
            step("s2", "coder", f"Implement the code, run AST verification and execute tests for: {goal}",
                 deps=["s1"], produces="implementation.md", consumes=["research_notes.md"],
                 why="owns the terminal and AST tooling"),
            step("s3", "data_analyst", f"Inspect the workspace and check inputs, edge cases and data shapes for: {goal}",
                 deps=["s1"], produces="analysis.md", consumes=["research_notes.md"],
                 why="can validate in parallel with implementation"),
            step("s4", "creative", "Write the documentation, walkthrough and usage examples from the work so far",
                 deps=["s2", "s3"], produces="briefing.md",
                 consumes=["implementation.md", "analysis.md"], why="final prose pass"),
        ]
    elif any(w in g for w in ["job", "resume", "interview", "hire", "career", "salary"]):
        raw = [
            step("s1", "researcher", f"Scan role requirements, skill criteria and interview topics for: {goal}",
                 produces="market_scan.md", why="market research first"),
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
                 produces="analysis.md", why="owns the data tooling"),
            step("s2", "researcher", f"Find the documented conventions and pitfalls relevant to: {goal}",
                 produces="research_notes.md", why="context, independent of the data pass"),
            step("s3", "creative", "Format the analytical report with key insights and a recommendation",
                 deps=["s1", "s2"], produces="briefing.md",
                 consumes=["analysis.md", "research_notes.md"], why="fan-in write-up"),
        ]
    else:
        raw = [
            step("s1", "researcher", f"Investigate the core concepts and documentation for: {goal}",
                 produces="research_notes.md", why="establish the ground truth"),
            step("s2", "coder", f"Prototype a validation script and probe the edge cases for: {goal}",
                 deps=["s1"], produces="implementation.md", consumes=["research_notes.md"],
                 why="empirical check"),
            step("s3", "data_analyst", f"Quantify the trade-offs and sanity-check the numbers behind: {goal}",
                 deps=["s1"], produces="analysis.md", consumes=["research_notes.md"],
                 why="runs alongside the prototype"),
            step("s4", "creative", "Summarise the findings, action items and next steps into an executive briefing",
                 deps=["s2", "s3"], produces="briefing.md",
                 consumes=["implementation.md", "analysis.md"], why="final synthesis"),
        ]

    steps = [s for s in raw if s.agent_id in valid_agent_ids]
    if not steps:
        # Nothing in the bucket is loaded: give the work to whoever exists.
        fallback = valid_agent_ids[0] if valid_agent_ids else "researcher"
        return [step("s1", fallback, goal, produces="briefing.md")]

    # Dropping a step can orphan a dependency; re-point what survived.
    kept = {s.id for s in steps}
    for s in steps:
        s.dependencies = [d for d in s.dependencies if d in kept]
        upstream = {x.produces for x in steps if x.id in s.dependencies}
        s.consumes = [c for c in s.consumes if c in upstream]
    return steps


planner = Planner()
