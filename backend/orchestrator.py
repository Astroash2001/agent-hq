import asyncio
import glob
import json
import logging
import os
import shutil
import re
import time
import uuid
from typing import Any, Dict, List, Optional, Set

import attachments
import contracts
import critic
import debate
import llm
import foundry
import office as office_mod
import planner as planner_mod
import pricing
import provenance
import runstore
import step_guard
import synthesis
import tracing
from base_agent import BaseAgent
from config import llm_configured, settings
from event_bus import event_bus
from memory import memory
from office import office
from planner import planner
from schemas import (AgentManifest, AgentStatus, Artifact, DAGNode, Event,
                     EventType, Plan, PlanStep, Task)
from tools import file_ops
from workspace import MissionWorkspace

logger = logging.getLogger("AgentHQ.Orchestrator")


# Heads every Markdown artifact the harness reconstructs after a step failed to
# write its own (`_recover_missing_artifact`).
RECOVERY_BANNER = (
    "> ⚠️ **Reconstructed by the harness, not written by the assigned agent.** "
    "The step did not produce this file, so it was assembled from other "
    "workspace documents and has not been reviewed. Treat it as unverified.\n\n"
)


class StepDeadlineExceeded(Exception):
    """A step ran past `settings.step_timeout_sec` and was stopped.

    Named without the word "Timeout" on purpose: `llm.is_transient` treats that
    substring in a class name as retryable, and a step that has already spent
    the entire time budget is the opposite of worth trying again.
    """


class Orchestrator:
    """
    Boss Orchestrator Agent.

    Plans a goal into a dependency graph, re-points steps at whichever specialist
    the office's own history says is better at them, runs the independent ones at
    the same time, and passes each step's output forward as a real artifact on a
    shared workspace.
    """

    def __init__(self):
        self.agents: Dict[str, BaseAgent] = {}
        self.boss_agent: Optional[BaseAgent] = None
        self.active_tasks: Dict[str, Task] = {}
        self.current_dag: List[DAGNode] = []
        self.current_plan: Optional[Plan] = None
        self.workspaces: Dict[str, MissionWorkspace] = {}
        # Per-mission, per-step cost and latency, keyed task_id -> step_id.
        # Kept here rather than on the agents because an agent's token_usage is
        # a lifetime total that never resets, so it cannot answer what one step
        # of one mission cost.
        self._step_telemetry: Dict[str, Dict[str, Dict[str, Any]]] = {}
        # Roster lifetime total at the instant each mission started, so a
        # mission's own spend is a subtraction away. Snapshotting is the only
        # way to keep the in-flight granularity the budget gate needs: an
        # agent's token_usage rises turn by turn, while `_step_telemetry` only
        # gains a row once a step has finished.
        self._token_baseline: Dict[str, int] = {}
        # Missions the kill switch has halted. Flagging the roster is not enough
        # on its own: `BaseAgent.run` clears `_is_cancelled` on entry, so a step
        # dispatched after the HALT would reset the flag and carry on. The
        # scheduler reads this before it dispatches anything.
        self._halted: Set[str] = set()
        # The scheduler spends nearly all its time parked in `asyncio.wait` on
        # the steps in flight, so the flag above is only read between waits.
        # That is enough when the halt makes the agents return -- their futures
        # complete and the wait comes back -- but not when one is stuck in a
        # tool call or queueing for a station, which is exactly when someone
        # reaches for the kill switch. The wait blocks on this too.
        self._halt_events: Dict[str, asyncio.Event] = {}
        # Figures the last briefing's conclusion asserted that no artifact
        # contained. Set by `_synthesize_results`, read by the manifest.
        self._last_ungrounded: List[str] = []
        # One agent runs one subtask at a time even when the DAG offers it two.
        self._agent_locks: Dict[str, asyncio.Lock] = {}
        # The real parallelism cap. Rebuilt per mission so a settings change
        # takes effect, and created there rather than here so it never binds to
        # the import-time event loop.
        self._slots: Optional[asyncio.Semaphore] = None
        # Live and peak concurrent _run_step calls. The peak is the only honest
        # way to assert the cap actually caps -- a counter that is only ever
        # read at rest reads zero however broken the scheduler is.
        self._active_steps = 0
        self._peak_active_steps = 0
        # Contractors hired for the running mission. Held apart from
        # `self.agents` membership so dismissal knows exactly who to let go
        # without having to ask each manifest whether it was spawned.
        self._spawned_ids: set = set()
        self._load_agents()

    def _load_agents(self):
        """
        Discover and load all agent JSON manifests from the agents directory.

        Contractors are not on disk, so a reload drops them. That is correct --
        they belong to one mission -- but it has to also drop their desks, or
        the seat stays held by an agent that no longer exists and the office
        reports itself full for every mission after.
        """
        for agent_id in getattr(self, "_spawned_ids", set()):
            office.release_seat(agent_id)
        self._spawned_ids = set()
        self.agents.clear()
        agent_files = glob.glob(os.path.join(settings.agents_dir, "*.json"))

        for filepath in agent_files:
            try:
                with open(filepath, "r", encoding="utf-8-sig") as f:
                    data = json.load(f)
                manifest = AgentManifest(**data)
                agent_instance = BaseAgent(manifest)

                if manifest.id == "orchestrator":
                    self.boss_agent = agent_instance
                else:
                    self.agents[manifest.id] = agent_instance
            except Exception as e:
                logger.error(f"Failed to load agent manifest {filepath}: {e}")

    def _agent_lock(self, agent_id: str) -> asyncio.Lock:
        """
        Created lazily so no lock binds to the import-time event loop.

        Recreated when the one on hand is bound to another loop, for the reason
        `office._lock_for` documents: the orchestrator is a process-wide
        singleton, and a lock first contended in one `asyncio.run` raises "bound
        to a different event loop" when two steps queue for the same agent in
        the next. A server runs one loop, so there this never fires.
        """
        existing = self._agent_locks.get(agent_id)
        if existing is not None:
            bound = getattr(existing, "_loop", None)
            try:
                running = asyncio.get_running_loop()
            except RuntimeError:
                running = None
            if bound is None or running is None or bound is running:
                return existing
        self._agent_locks[agent_id] = asyncio.Lock()
        return self._agent_locks[agent_id]

    def _agent_by_id(self, agent_id: str):
        """
        Any agent by id, boss included.

        The boss is held in `boss_agent` rather than in `self.agents`, but
        `get_manifests` offers it to the planner alongside the specialists -- so
        the planner could always name it, and any step it did name would raise
        "unknown agent". Meetings made that reachable in practice, since the
        boss is the natural chair.
        """
        if self.boss_agent and agent_id == self.boss_agent.id:
            return self.boss_agent
        return self.agents.get(agent_id)

    def get_manifests(self) -> List[Dict[str, Any]]:
        """
        All agent manifests for frontend rendering.

        Each carries its lifetime record from memory, which is both what the
        planner sees when choosing specialists and what the dock can display.
        """
        if not self.agents:
            self._load_agents()

        manifests = []
        if self.boss_agent:
            payload = self.boss_agent.manifest.model_dump()
            payload["stats"] = memory.stats(self.boss_agent.id)
            manifests.append(payload)
        for agent in self.agents.values():
            payload = agent.manifest.model_dump()
            payload["stats"] = memory.stats(agent.id)
            manifests.append(payload)
        return manifests

    def cancel_all(self):
        """
        Emergency kill switch -- halt every running agent *and* every mission.

        Cancelling the agents alone stopped one turn, not the run. `cancel()`
        aborts the request in flight, but `BaseAgent.run` clears `_is_cancelled`
        at the top of every run, so the next step the scheduler dispatched came
        up with a clean flag and the mission carried on spending. Marking the
        missions halted is what stops the scheduler offering it that next step.
        """
        logger.warning("Emergency kill switch activated! Cancelling all agents.")
        self._halted.update(self.active_tasks.keys())
        for task_id in self.active_tasks:
            waker = self._halt_events.get(task_id)
            if waker is not None:
                waker.set()
        if self.boss_agent:
            self.boss_agent.cancel()
        for agent in self.agents.values():
            agent.cancel()

    # ------------------------------------------------------------------ mission

    async def handle_user_input(
        self,
        user_prompt: str,
        resume_from: Optional[str] = None,
        attachments: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        """
        Main entry point: plan the goal, run the graph, synthesise the briefing,
        and stream the whole thing to the office as it happens.

        Thin on purpose. The trace is opened here and closed in the `finally`,
        so a mission that raises halfway still leaves a readable trace.jsonl --
        which is then the only record of how far it got. The work itself is in
        `_run_mission`, running inside the mission span.

        `resume_from` picks up a mission that a killed process left unfinished.
        The goal, the plan and the list of steps that already completed come
        from the run store rather than from this call, so a resume cannot
        accidentally run a different mission under an old id.

        `attachments` names files the user dropped with the goal. They are
        copied into the workspace before planning, so from the planner's point
        of view they are simply artifacts that already exist -- which is what
        lets a step `consume` one without any new machinery.
        """
        if not self.agents:
            self._load_agents()

        store = runstore.get_store()
        resumed: Optional[Dict[str, Any]] = None

        if resume_from:
            resumed = store.get_mission(resume_from)
            if not resumed:
                raise KeyError(f"no mission '{resume_from}' in the run store")
            if not resumed.get("plan"):
                raise ValueError(
                    f"mission '{resume_from}' has no stored plan; it died before "
                    f"planning finished and there is nothing to resume")
            task_id = resume_from
            user_prompt = resumed["goal"]
        else:
            slug = self._slugify_goal(user_prompt)
            task_id = f"task_{slug}_{uuid.uuid4().hex[:6]}"

        workspace = MissionWorkspace(task_id)
        self.workspaces[task_id] = workspace
        self._step_telemetry.setdefault(task_id, {})
        # Taken before planning, so the planner's own tokens are charged to the
        # mission that asked for them. Assigned rather than set by default: a
        # resumed mission re-baselines, because its budget governs the work
        # still to be done and not what the process spent before it died.
        self._token_baseline[task_id] = self._tokens_spent()
        # A halt belongs to the mission that was running when the button was
        # pressed, not to the id. A resume of that mission starts unhalted --
        # including its wakeup, which `_execute_plan` would otherwise pick up
        # already set and halt the resumed run before it dispatched anything.
        self._halted.discard(task_id)
        self._halt_events.pop(task_id, None)

        # Seeded before planning, so the planner can see what it has to work
        # with and route the first step to whoever should read it.
        seeded = await self._seed_attachments(workspace, attachments)

        # What this mission may recall is fixed here, before any step runs.
        # Otherwise whether a step sees its parallel sibling's episode depends
        # on which of them finished first, which is a race rather than a
        # decision -- and it made the eval suite unreplayable, since a replay
        # returns instantly and orders those completions differently from the
        # recording that produced the cassettes.
        if settings.memory_enabled:
            memory.freeze()

        trace = tracing.start_mission(task_id, workspace.dir)
        try:
            async with tracing.span("mission", user_prompt[:120],
                                    task_id=task_id, resumed=bool(resumed)) as node:
                result = await self._run_mission(
                    task_id, workspace, user_prompt, resumed, seeded)
                node.set(cost_usd=result.get("cost_usd", 0.0),
                         steps=len(result.get("subtasks") or []))
                return result
        except BaseException:
            # `end_mission` sits at the end of the happy path, so until now
            # anything raising in between left the row saying `running` for the
            # life of the database -- not "this broke" but "this is still
            # going", which is the one thing it certainly was not. Observed on
            # a mission whose planner took a 502 from the provider: no plan, no
            # steps, and a row that still claimed to be in flight hours later.
            #
            # BaseException, not Exception, because a cancelled mission is the
            # commonest way out that is not a return -- CancelledError is not
            # an Exception, and a mission cancelled at shutdown strands its row
            # exactly like a crashed one does.
            #
            # A halt that goes through the normal path already ends as
            # `halted`; this reads `_halted` too so the rare halt that escapes
            # is not downgraded to `failed`. The cost is left as it stands: a
            # run that died here does not know its bill, and a resumed mission
            # already carries what its first run spent.
            runstore.get_store().end_mission(
                task_id,
                "halted" if task_id in self._halted else "failed",
                cost_usd=None,
            )
            raise
        finally:
            tracing.end_mission()
            # Back to seeing everything, so the NEXT mission recalls what this
            # one learned. The horizon is per mission, not a global setting.
            memory.thaw()
            # Contracts end here as well as on the happy path, and this is the
            # copy that matters. A contractor is in `self.agents`, and
            # `get_manifests` feeds that straight into the planner prompt as
            # the roster -- so one hired by a mission that then raised would be
            # offered to every later mission in the same process, as an agent
            # nothing on disk knows about.
            #
            # Worst where it is hardest to see: an eval sweep runs all fourteen
            # tasks in one process and the runner catches a failure per task so
            # the suite continues. One task failing after a hire would re-key
            # every task after it, and the recording would not replay from a
            # clean process. Two earlier sweeps were thrown away for faults of
            # exactly this shape.
            self._dismiss_spawned()

    @staticmethod
    def _rehydrate_workspace(workspace: MissionWorkspace) -> List[str]:
        """
        Put the artifacts a previous run already wrote back on the blackboard.

        The workspace is in-memory and dies with the process, but its mirror on
        disk does not -- that mirror exists precisely so a finished mission
        outlives the browser tab, and it turns out to be what makes a resume
        possible at all. Without this a resumed step would `consumes` an
        artifact the store has never heard of and receive an empty context,
        redoing upstream work that is sitting on disk beside it.
        """
        restored: List[str] = []
        if not workspace.dir or not os.path.isdir(workspace.dir):
            return restored

        skip = {"report.md", "mission.json", tracing.TRACE_FILENAME}
        for name in sorted(os.listdir(workspace.dir)):
            if name in skip:
                continue
            path = os.path.join(workspace.dir, name)
            if not os.path.isfile(path):
                continue
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as fh:
                    content = fh.read()
            except OSError as exc:
                logger.warning("could not rehydrate %s: %s", path, exc)
                continue
            workspace.artifacts[name] = Artifact(
                name=name, content=content, author_id="resumed",
                author_name="a previous run", size_chars=len(content))
            restored.append(name)
        return restored

    @staticmethod
    async def _seed_attachments(
        workspace: MissionWorkspace,
        names: Optional[List[str]]
    ) -> List[str]:
        """
        Copy the user's dropped files into the mission workspace as artifacts.

        Filed under an author of "you", because that is who produced them, and
        an agent that sees "delivered by you" treats it as the brief rather than
        as a colleague's working notes.

        Truncated on the way in, not on the way out: the whole point of an
        attachment is that it lands in a prompt, and `max_attachment_chars`
        exists so a 2MB CSV does not cost the mission budget on every turn.

        A named file that is not staged is skipped with a warning rather than
        failing the mission -- the goal is still runnable without it, and losing
        the run over a missing upload would be the worse outcome.
        """
        if not names:
            return []

        seeded: List[str] = []
        for name in names:
            # An image has no text to seed. Copied as bytes into the mission
            # directory instead, where `describe_image` can reach it -- that
            # tool is the only reader an image has, and it needs the pixels.
            # `workspace.put` stores a string, so this goes round it, and the
            # file still lands where every other artifact lives.
            if attachments.is_image(name):
                source = attachments.path_of(name)
                if not source or not workspace.dir:
                    logger.warning("image attachment %r is missing; skipped", name)
                    continue
                safe = attachments.safe_name(name)
                try:
                    shutil.copyfile(source, os.path.join(workspace.dir, safe))
                except OSError as exc:
                    logger.warning("could not seed image %r: %s", name, exc)
                    continue
                attachments.discard(name)
                seeded.append(safe)
                continue

            body = attachments.read(name)
            if not body.strip():
                logger.warning("attachment %r is missing or empty; skipped", name)
                continue
            clipped = body[:settings.max_attachment_chars]
            if len(body) > len(clipped):
                clipped += (
                    "\n\n... [attachment truncated at "
                    f"{settings.max_attachment_chars} characters "
                    f"of {len(body)}]")
            await workspace.put(name=attachments.safe_name(name), content=clipped,
                                author_id="user", author_name="you")
            attachments.discard(name)
            seeded.append(attachments.safe_name(name))

        if seeded:
            logger.info("seeded %d attachment(s) into the workspace: %s",
                        len(seeded), ", ".join(seeded))
        return seeded

    async def _run_mission(
        self,
        task_id: str,
        workspace: MissionWorkspace,
        user_prompt: str,
        resumed: Optional[Dict[str, Any]] = None,
        seeded: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        """The mission proper, run inside the mission span its caller opened."""
        main_task = Task(id=task_id, description=user_prompt, status="in_progress")
        self.active_tasks[task_id] = main_task

        store = runstore.get_store()
        # Rebuilt per mission so a changed max_parallel_agents takes effect, and
        # here rather than in __init__ so it binds to the running event loop.
        limit = settings.max_parallel_agents if settings.parallel_enabled else 1
        self._slots = asyncio.Semaphore(max(1, limit))
        self._peak_active_steps = 0
        self._active_steps = 0

        start_time = time.time()

        await event_bus.emit(Event(
            type=EventType.TASK_CREATED,
            task_id=task_id,
            data={"description": user_prompt}
        ))

        # 1. The boss goes to the whiteboard to plan.
        if self.boss_agent:
            await self.boss_agent.update_status(AgentStatus.THINKING)
            await self.boss_agent.speak(f"Decomposing goal: '{user_prompt[:50]}...'", task_id=task_id)
            if settings.spatial_enabled:
                board = office.get_station("whiteboard")
                if board:
                    await office.walk_to(
                        self.boss_agent.id, (board.x, board.y),
                        agent_name=self.boss_agent.name, label=board.label,
                        station_id=board.id, task_id=task_id
                    )

        # 2. Plan -- or recover the one this mission was already running.
        already_completed: Set[str] = set()
        if resumed:
            # Re-planning a resumed mission would be a different mission wearing
            # the same id: the artifacts already on disk were produced against
            # the original graph, and a fresh plan would consume files that no
            # step in it produces. Memory routing is skipped for the same
            # reason -- a step that has already run cannot change hands now.
            plan = Plan(goal=resumed["goal"], source=resumed["plan"].get("source", "llm"),
                        steps=[PlanStep(**s) for s in resumed["plan"].get("steps", [])])
            # Contractors back before any step is dispatched. The plan records
            # each step's agent_id, and a step hired for is assigned to an
            # agent that exists nowhere on disk -- so a resume without this
            # raises "unknown agent" on exactly the step the hire was for, and
            # only when the process had been restarted, which is the hardest
            # kind of bug to catch.
            self._rehire_spawned(task_id, store)
            already_completed = set(store.completed_step_ids(task_id))
            restored = self._rehydrate_workspace(workspace)
            logger.info("[%s] resuming: %d/%d steps already done, %d artifact(s) restored",
                        task_id, len(already_completed), len(plan.steps), len(restored))
            if self.boss_agent:
                await self.boss_agent.speak(
                    f"Resuming: {len(already_completed)} of {len(plan.steps)} steps "
                    f"already done", task_id=task_id)
            await event_bus.emit(Event(
                type=EventType.TERMINAL_LOG,
                task_id=task_id,
                data={"message": f"Resumed {task_id}: skipping "
                                 f"{sorted(already_completed)}, restored "
                                 f"{len(restored)} artifact(s)",
                      "level": "INFO"}
            ))
        else:
            store.start_mission(task_id, user_prompt, workspace.dir or "")
            plan = await planner.plan(user_prompt, self.get_manifests(),
                                      attachments=seeded)
            self._apply_memory_routing(plan, task_id)
            await self._staff_plan(plan, user_prompt, task_id)
            store.record_plan(task_id, plan)
        self.current_plan = plan

        await event_bus.emit(Event(
            type=EventType.PLAN_CREATED,
            task_id=task_id,
            data={
                "goal": user_prompt,
                "source": plan.source,
                "steps": [s.model_dump() for s in plan.steps],
                "parallel_enabled": settings.parallel_enabled,
                "max_parallel": settings.max_parallel_agents
            }
        ))
        if self.boss_agent:
            await self.boss_agent.speak(
                f"{len(plan.steps)}-step plan ({plan.source}). Dispatching.", task_id=task_id
            )
            if settings.spatial_enabled:
                await office.return_to_desk(self.boss_agent.id, self.boss_agent.name, task_id)

        # 3. Build the DAG the UI draws, then run it.
        self.current_dag = self._build_dag(plan)
        await self._emit_dag(task_id, plan.source)

        results = await self._execute_plan(
            plan, workspace, task_id, user_prompt, already_completed)

        # 4. Synthesise.
        if self.boss_agent:
            await self.boss_agent.update_status(AgentStatus.THINKING)
            await self.boss_agent.speak("Synthesizing final executive briefing...", task_id=task_id)

        duration = time.time() - start_time
        # Queries run before a restart were never recorded in this process, so a
        # resumed mission cannot be checked without flagging real work.
        self._provenance_checkable = not already_completed
        synthesis_text = await self._synthesize_results(
            user_prompt, results, duration, plan, workspace)

        main_task.status = "completed"
        main_task.result = synthesis_text

        # Put the run on disk before announcing it. The websocket payload is
        # the only other copy, so a browser that is closed or reloaded at this
        # moment would otherwise take the whole mission with it.
        telemetry = self._mission_telemetry(task_id, plan)
        outcome = self._mission_outcome(task_id, plan)
        store.end_mission(task_id, outcome, telemetry["cost_usd"])

        workspace.save_report(synthesis_text)
        workspace.save_manifest({
            "mission_id": task_id,
            "goal": user_prompt,
            "outcome": outcome,
            "plan_source": plan.source,
            "duration_seconds": round(duration, 2),
            "total_tokens": self._mission_tokens_spent(task_id),
            "token_budget": settings.max_tokens_budget_per_task,
            # Both figures are now this run's. `total_tokens` is what the budget
            # gate measured -- every token the roster burned for this mission,
            # including turns no step could be billed for. `mission_tokens` sums
            # the per-step rows, so it is the part the breakdown accounts for
            # and is the smaller of the two whenever the two disagree.
            "mission_tokens": telemetry["mission_tokens"],
            "cost_usd": telemetry["cost_usd"],
            "unpriced_models": telemetry["unpriced_models"],
            # Figures the conclusion asserts that no artifact contains. Empty is
            # the good case and is recorded either way, because "checked and
            # clean" and "never checked" must not read alike.
            "ungrounded_figures": list(getattr(self, "_last_ungrounded", [])),
            # SQL shown in an artifact that no successful query_data call in
            # this mission ran. `null` means not checked (a resumed mission),
            # which must not read like an empty, clean list.
            "unexecuted_sql": getattr(self, "_last_unexecuted_sql", None),
            "tokens_estimated": telemetry["tokens_estimated"],
            "per_step": telemetry["per_step"],
            "steps": [
                {
                    "id": s.id,
                    "agent_id": s.agent_id,
                    "description": s.description,
                    "kind": getattr(s, "kind", "work"),
                    "attendees": getattr(s, "attendees", []),
                    "dependencies": s.dependencies,
                    "produces": s.produces,
                    "consumes": s.consumes,
                    "status": s.status,
                    # What the step said it could not be done without, and who
                    # it was taken off to satisfy that. Recorded even when null:
                    # "the planner named no tools" and "the planner was not
                    # asked" read identically otherwise.
                    "needs_tools": getattr(s, "needs_tools", []),
                    "regranted_from": getattr(s, "regranted_from", None),
                }
                for s in plan.steps
            ],
            "artifacts": [a.name for a in workspace.artifacts.values()],
        })

        await event_bus.emit(Event(
            type=EventType.TASK_COMPLETED,
            task_id=task_id,
            data={
                "result": synthesis_text,
                "duration_seconds": round(duration, 2),
                "subtasks_count": len(plan.steps),
                # Counts, not just a total. A mission whose every step failed
                # still reaches this line, and without these it announced
                # itself as a success.
                "steps_completed": sum(1 for s in plan.steps if s.status == "completed"),
                "steps_failed": sum(1 for s in plan.steps if s.status == "failed"),
                # The same outcome mission.json records. A mission can finish
                # every step and still be "unverified", which the counts above
                # cannot say.
                "outcome": outcome,
                "unverified_steps": [s.id for s in self._unverified_steps(task_id, plan)],
                # The id the artifact and bundle endpoints address. The envelope
                # carries it too, but `task.completed` is handed to the output
                # panel as data alone.
                "mission_id": task_id,
                "plan_source": plan.source,
                "artifacts": workspace.summary(),
                "mission_dir": workspace.dir,
                "cost_usd": telemetry["cost_usd"],
                # False here does not mean free -- it means the numbers came
                # from a provider rather than the simulator.
                "tokens_estimated": telemetry["tokens_estimated"],
            }
        ))
        self._update_missions_index()

        if self.boss_agent:
            await self.boss_agent.update_status(AgentStatus.IDLE)
            failed_count = sum(1 for s in plan.steps if s.status == "failed")
            await self.boss_agent.speak(
                "Mission complete! Delivered briefing." if not failed_count
                else f"Mission ended with {failed_count} failed step(s).",
                task_id=task_id)

        for agent in self.agents.values():
            await agent.update_status(AgentStatus.IDLE)
        office.reset()
        # After office.reset(), which clears the desk assignments this reads
        # nothing from -- but before the next mission, which must see the
        # roster it will actually be planned against.
        self._dismiss_spawned()

        return {
            "task_id": task_id,
            "status": "completed",
            "result": synthesis_text,
            "subtasks": results,
            "plan_source": plan.source,
            "artifacts": workspace.summary(),
            "mission_dir": workspace.dir,
            "duration_seconds": round(duration, 2),
            "cost_usd": telemetry["cost_usd"],
            "tokens_estimated": telemetry["tokens_estimated"],
            "per_step": telemetry["per_step"],
            "resumed": bool(resumed),
            "skipped_steps": sorted(already_completed),
        }

    # ------------------------------------------------------------------ routing

    async def _staff_plan(self, plan: Plan, goal: str, task_id: str) -> None:
        """
        Hire a specialist for any step the roster cannot staff.

        `planner.regrant_steps` has already moved every step it could to
        somebody who holds the tools it needs. What is left is the set it gave
        up on -- steps whose declared tools no single agent holds -- and until
        now those ran anyway, on an agent that could not call the tool and
        would write a plausible report around the hole.

        Registered into `self.agents` for this mission only, and persisted with
        the mission rather than into `agents_dir`: a roster that grows by itself
        is one nobody curated, and the planner would pay to list it forever.
        """
        if not settings.foundry_enabled or not plan.steps:
            return

        agent_tools = {aid: list(a.tools) for aid, a in self.agents.items()}
        gaps = foundry.find_gaps(plan.steps, agent_tools)
        if not gaps:
            return

        hires, refusals = await foundry.hire(
            goal, gaps,
            existing_ids=list(self.agents.keys()) + (
                [self.boss_agent.id] if self.boss_agent else []),
            mission_id=task_id,
            seats_available=office.free_seats(),
            existing_names=[a.name for a in self.agents.values()] + (
                [self.boss_agent.name] if self.boss_agent else []),
        )

        for manifest in hires:
            seat = office.assign_seat(manifest.id)
            if seat is None:
                # The office filled up between the budget check and here. Said
                # rather than seated on the default tile, where two contractors
                # would stack and read as one agent.
                refusals.append(f"no desk free for {manifest.name}; not hired")
                continue

            agent = BaseAgent(manifest)
            self.agents[manifest.id] = agent
            self._spawned_ids.add(manifest.id)
            runstore.get_store().record_spawned_agent(task_id, manifest)

            await event_bus.emit(Event(
                type=EventType.AGENT_SPAWNED,
                agent_id=manifest.id,
                task_id=task_id,
                data={
                    "manifest": manifest.model_dump(),
                    "seat": {"x": seat[0], "y": seat[1]},
                    "entrance": {"x": office_mod.ENTRANCE[0], "y": office_mod.ENTRANCE[1]},
                    "for_step": manifest.spawned_for,
                    "tools": list(manifest.tools),
                },
            ))
            if self.boss_agent:
                await self.boss_agent.speak(
                    f"Nobody here holds {manifest.spawned_reason}. "
                    f"{manifest.name} is joining us for this one.", task_id=task_id)

            # Walk them in from the street door before the plan dispatches, so
            # the office shows them arriving rather than appearing at a desk.
            await office.walk_in(manifest.id, agent_name=manifest.name, task_id=task_id)

        if hires:
            # Re-run the routing now the roster is bigger. This is what moves
            # the gap step onto its new specialist -- the hire is pointless
            # without it, and doing it here rather than in `hire` keeps the
            # foundry free of any opinion about the plan.
            planner_mod.regrant_steps(
                plan.steps,
                list(self.agents.keys()),
                {aid: list(a.tools) for aid, a in self.agents.items()},
            )

        for note in refusals:
            logger.warning("foundry: %s", note)
            await event_bus.emit(Event(
                type=EventType.TERMINAL_LOG,
                task_id=task_id,
                data={"level": "ERROR", "message": f"[FOUNDRY] {note}"},
            ))

    def _rehire_spawned(self, mission_id: str, store) -> None:
        """
        Rebuild the contractors a resumed mission had hired.

        Their desks are re-assigned too, in the order they were hired, so an
        agent resumes at the desk it left rather than at DEFAULT_SEAT with
        everyone else who has no seat.
        """
        for payload in store.get_spawned_agents(mission_id):
            try:
                manifest = AgentManifest(**payload)
            except Exception as exc:                           # noqa: BLE001
                logger.warning("could not rebuild spawned agent %s: %s",
                               payload.get("id"), exc)
                continue
            if manifest.id in self.agents:
                continue
            if office.assign_seat(manifest.id) is None:
                logger.warning("no desk free to rehire %s on resume", manifest.id)
            self.agents[manifest.id] = BaseAgent(manifest)
            self._spawned_ids.add(manifest.id)
            logger.info("[%s] rehired %s for the resumed mission", mission_id, manifest.id)

    def _dismiss_spawned(self) -> None:
        """
        End every contract from the mission that just finished.

        Their desks go back to the pool and they leave `self.agents`, because
        the next mission's planner is shown that dict and a specialist hired
        for somebody else's goal is noise in a prompt that is already the
        largest thing sent.
        """
        for agent_id in list(self._spawned_ids):
            self.agents.pop(agent_id, None)
            self._agent_locks.pop(agent_id, None)
            office.release_seat(agent_id)
        self._spawned_ids.clear()

    def _apply_memory_routing(self, plan: Plan, task_id: str):
        """
        Let the record override the plan.

        The planner picks on role and tools; memory picks on what has actually
        worked. When a challenger has enough relevant history and beats the pick
        by a clear margin, the step changes hands and the swap is announced --
        silent rerouting would make the office impossible to reason about.
        """
        if not settings.memory_enabled:
            return

        for step in plan.steps:
            # Never take work from a specialist hired specifically for this step.
            if step.agent_id in getattr(self, "_spawned_ids", set()):
                continue

            # A step can only be rerouted to someone who actually holds the tools
            # it declared it needs. `_run_step` grants write_file to any step
            # producing an artifact, so write_file is not a blocker then.
            needed = set(step.needs_tools or [])
            if step.produces:
                needed.discard("write_file")

            valid_candidates = [
                aid for aid, a in self.agents.items()
                if not needed or needed <= set(a.tools)
            ]
            if len(valid_candidates) <= 1:
                continue

            suggestion = memory.suggest_agent(step.description, valid_candidates, step.agent_id)
            if not suggestion:
                continue
            new_id, new_score, old_score = suggestion
            old_id = step.agent_id
            step.routed_from = old_id
            step.agent_id = new_id

            old_name = self.agents[old_id].name if old_id in self.agents else old_id
            new_name = self.agents[new_id].name if new_id in self.agents else new_id
            logger.info(f"Memory rerouted {step.id}: {old_id} -> {new_id} "
                        f"({old_score:.2f} -> {new_score:.2f})")
            asyncio.create_task(event_bus.emit(Event(
                type=EventType.ROUTE_CHANGED,
                agent_id=new_id,
                task_id=task_id,
                data={
                    "step_id": step.id,
                    "from_agent_id": old_id,
                    "from_agent_name": old_name,
                    "to_agent_id": new_id,
                    "to_agent_name": new_name,
                    "from_score": round(old_score, 3),
                    "to_score": round(new_score, 3),
                    "reason": f"{new_name} has a stronger record on work like this"
                }
            )))

    # ---------------------------------------------------------------- scheduling

    def _build_dag(self, plan: Plan) -> List[DAGNode]:
        return [
            DAGNode(
                id=step.id,
                label=step.description[:28],
                agent_id=step.agent_id,
                status=step.status,
                dependencies=list(step.dependencies)
            )
            for step in plan.steps
        ]

    @staticmethod
    def _best_artifact(workspace, step, before):
        """
        The file this step actually wrote, if it wrote one.

        An agent may write several (drafts, notes). Preference goes to an exact
        match on the promised name, then to the largest new one -- the closing
        summary an agent writes about its work is always shorter than the work.

        Only files this step wrote count. Steps run in parallel, so "new since
        the attempt started" also covers a sibling's output: observed, an
        extraction step that wrote nothing was credited with the guardrail
        step's file, which was then re-filed as `extraction.md` and read by the
        review as the extraction.
        """
        fresh = [workspace.get(n) for n in workspace.names() if n not in before]
        fresh = [a for a in fresh if a is not None and a.step_id == step.id]
        if not fresh:
            return None
        for a in fresh:
            if a.name == step.produces:
                return a
        return max(fresh, key=lambda a: a.size_chars)

    def _tokens_spent(self) -> int:
        """
        Tokens burned across the whole roster.

        Agents keep their own running totals (BaseAgent.token_usage), so the
        mission figure is just their sum -- there is no separate ledger to keep
        in step with them.
        """
        total = sum(a.token_usage.total_tokens for a in self.agents.values())
        if self.boss_agent:
            total += self.boss_agent.token_usage.total_tokens
        return total

    def _mission_tokens_spent(self, task_id: str) -> int:
        """
        What this mission alone has burned, in-flight turns included.

        The roster's counters are lifetime totals that never reset, so reading
        them directly made the budget gate tighten as a process served more
        missions: a long-lived server would eventually refuse a mission that a
        fresh process would have run. Subtracting the baseline taken at mission
        start scopes the figure to the run without giving up the per-turn
        resolution -- the gate is checked while steps are still in flight, and
        `_step_telemetry` has nothing to say about a step until it finishes.

        Clamped at zero. Nothing resets an agent's counter mid-mission today,
        but a negative budget reading would fail open, and that is the one
        direction this must not fail in.
        """
        baseline = self._token_baseline.get(task_id)
        if baseline is None:
            # No baseline means the mission was never registered here -- a unit
            # test driving `_execute_plan` directly, most likely. Charging it
            # the roster's lifetime total is the conservative reading.
            return self._tokens_spent()
        return max(0, self._tokens_spent() - baseline)

    def _record_step_telemetry(self, task_id: str, step: PlanStep, stats: Dict[str, Any]):
        """
        File what one step cost, from the agent's `last_run`.

        Written for failed steps as well as completed ones: a step that raised
        after four tool calls and two retries still spent that money, and a
        cost report that only counts successes understates every bad run.
        """
        rows = self._step_telemetry.setdefault(task_id, {})
        # Additive, because a step can be dispatched more than once: a repair
        # attempt is real spend on the same step, and a row that overwrote
        # would report the cost of quality as the cost of the last try only.
        prior = rows.get(step.id) or {}
        rows[step.id] = {
            "step_id": step.id,
            "agent_id": step.agent_id,
            "model": stats.get("model", "") or prior.get("model", ""),
            "prompt_tokens": prior.get("prompt_tokens", 0) + int(stats.get("prompt_tokens", 0) or 0),
            "completion_tokens": prior.get("completion_tokens", 0) + int(stats.get("completion_tokens", 0) or 0),
            "cost_usd": round(prior.get("cost_usd", 0.0)
                              + float(stats.get("cost_usd", 0.0) or 0.0), 6),
            "duration_sec": round(prior.get("duration_sec", 0.0)
                                  + float(stats.get("duration_sec", 0.0) or 0.0), 2),
            "tool_calls": prior.get("tool_calls", 0) + len(stats.get("tools_used", []) or []),
            "retries": prior.get("retries", 0) + int(stats.get("retries", 0) or 0),
            "attempts": prior.get("attempts", 0) + 1,
            "fell_back": bool(stats.get("fell_back")) or bool(prior.get("fell_back")),
            "estimated": bool(stats.get("estimated")) or bool(prior.get("estimated")),
        }

    def _mission_outcome(self, task_id: str, plan: Plan) -> str:
        """
        How this mission ended: completed, failed, or halted.

        "halted" is its own status, not a kind of failure. In the mission list
        the two looked identical -- both `failed` -- so an operator could not
        tell a run they stopped on purpose from one that broke, which is the
        first thing you want to know about a red row. `api_missions` gates
        resume on "completed" alone, so a third value costs nothing and a
        halted mission stays resumable.

        A halt wins over the failed steps it caused, because those steps failed
        *because* of it.
        """
        if task_id in self._halted:
            return "halted"
        if any(s.status == "failed" for s in plan.steps):
            return "failed"
        # Every step ran, but a reviewer's last word on at least one was
        # "revise". Recorded as its own outcome because "completed" was printed
        # for a mission whose extraction step scored 0.05 and filed nothing of
        # its own -- the status is what a reader trusts first. Resume gates on
        # "completed" alone, so an unverified mission can be resumed.
        rows = self._step_telemetry.get(task_id, {})
        if any(rows.get(s.id, {}).get("verdict") == "revise" for s in plan.steps):
            return "unverified"
        return "completed"

    def _mission_telemetry(self, task_id: str, plan: Plan) -> Dict[str, Any]:
        """
        The mission's cost breakdown, one row per plan step.

        `cost_usd` here is this mission's, summed from the steps -- not
        `_tokens_spent()`, which sums the roster's lifetime totals and so keeps
        climbing across every mission the process has served.

        A model with no row in `pricing` contributes 0.0 and is named in
        `unpriced_models`, because a total that silently omits a model is worse
        than one that admits what it could not price.
        """
        rows = self._step_telemetry.get(task_id, {})
        per_step = []
        unpriced = set()
        for step in plan.steps:
            row = dict(rows.get(step.id, {"step_id": step.id, "agent_id": step.agent_id}))
            row["status"] = step.status
            model = row.get("model") or ""
            if model and not pricing.is_priced(model):
                unpriced.add(model)
            per_step.append(row)

        return {
            "per_step": per_step,
            "cost_usd": round(sum(float(r.get("cost_usd", 0.0) or 0.0)
                                  for r in per_step), 6),
            "mission_tokens": sum(int(r.get("prompt_tokens", 0) or 0)
                                  + int(r.get("completion_tokens", 0) or 0)
                                  for r in per_step),
            "unpriced_models": sorted(unpriced),
            # True when any step's numbers came from the simulator rather than
            # a provider, so nothing downstream presents a guess as a reading.
            "tokens_estimated": any(r.get("estimated") for r in per_step),
        }

    async def _emit_dag(self, task_id: str, source: str = ""):
        """
        Restate the graph, with what each finished step actually cost.

        The node used to carry status and nothing else, so the HUD could draw
        the shape of a mission but not its price: cost and latency existed only
        in `mission.json`, written once at the end. Merging the telemetry rows
        in here means the panel reads the same numbers the manifest will, while
        the mission is still running -- and it costs no new event type, because
        this one already fires on every status change.

        A step that has not run yet contributes no row and carries no numbers.
        `null` rather than 0.0 for those, so "not started" cannot be drawn as
        "free".
        """
        rows = self._step_telemetry.get(task_id, {})
        nodes = []
        for node in self.current_dag:
            payload = node.model_dump()
            row = rows.get(node.id)
            if row:
                payload.update({
                    "cost_usd": row.get("cost_usd", 0.0),
                    "duration_sec": row.get("duration_sec", 0.0),
                    "total_tokens": (int(row.get("prompt_tokens", 0) or 0)
                                     + int(row.get("completion_tokens", 0) or 0)),
                    "model": row.get("model", ""),
                    "attempts": row.get("attempts", 0),
                    "estimated": bool(row.get("estimated")),
                })
            nodes.append(payload)

        await event_bus.emit(Event(
            type=EventType.DAG_UPDATE,
            task_id=task_id,
            data={
                "dag": nodes,
                "source": source,
                # Mission totals, so the cost panel never has to sum the roster's
                # lifetime counters -- which climb across every mission the
                # process has served and would report this run as the sum of all
                # of them.
                "cost_usd": round(sum(float(r.get("cost_usd", 0.0) or 0.0)
                                      for r in rows.values()), 6),
                "mission_tokens": sum(int(r.get("prompt_tokens", 0) or 0)
                                      + int(r.get("completion_tokens", 0) or 0)
                                      for r in rows.values()),
                "tokens_estimated": any(r.get("estimated") for r in rows.values()),
            }
        ))

    def _set_node_status(self, step_id: str, status: str):
        for node in self.current_dag:
            if node.id == step_id:
                node.status = status

    async def _run_step_scheduled(
        self,
        step: PlanStep,
        workspace: MissionWorkspace,
        task_id: str,
        goal: str
    ) -> Dict[str, Any]:
        """
        One step, under a concurrency slot, a deadline, and a retry budget.

        The slot is taken *before* the deadline starts. A step queued behind the
        parallelism cap has not begun work, and timing it out for waiting its
        turn would punish it for the scheduler's decision rather than its own.
        """
        attempt = 0
        while True:
            attempt += 1
            try:
                async with self._slots:
                    return await asyncio.wait_for(
                        self._run_step(step, workspace, task_id, goal),
                        timeout=settings.step_timeout_sec)

            except asyncio.TimeoutError:
                # Deliberately not retried, and deliberately not re-raised as
                # anything containing "Timeout" -- llm.is_transient reads that
                # word in a class name as retryable, and this is the opposite. A
                # step that burned the whole step_timeout_sec has already spent
                # the time budget, and the usual cause (a model that will not
                # stop) recurs on the next attempt at the same price.
                raise StepDeadlineExceeded(
                    f"step {step.id} exceeded {settings.step_timeout_sec:.0f}s "
                    f"and was stopped") from None

            except asyncio.CancelledError:
                raise

            except Exception as exc:
                # llm.is_transient rather than a second classifier here: one
                # place decides what "worth trying again" means, or the two
                # drift and a 401 starts getting retried in one of them. A step
                # that failed because the agent produced garbage is contracts.py
                # and critic.py's problem, not a retry candidate.
                if attempt >= max(1, settings.step_max_attempts) or not llm.is_transient(exc):
                    raise
                logger.warning("[%s] step %s failed transiently (%s), retrying %d/%d",
                               task_id, step.id, type(exc).__name__,
                               attempt + 1, settings.step_max_attempts)
                await event_bus.emit(Event(
                    type=EventType.TERMINAL_LOG,
                    task_id=task_id,
                    data={"message": f"{step.id} hit a transient failure "
                                     f"({type(exc).__name__}); retrying",
                          "level": "INFO"}
                ))

    async def _execute_plan(
        self,
        plan: Plan,
        workspace: MissionWorkspace,
        task_id: str,
        goal: str,
        already_completed: Optional[Set[str]] = None
    ) -> List[Dict[str, Any]]:
        """
        Run the graph, starting every step the moment it becomes runnable.

        This used to take a slice of the ready set -- `ready[:limit]` -- which
        made `max_parallel_agents` a batch size rather than a concurrency limit:
        a wave of five ran three, waited for all three, then ran two, leaving
        slots idle whenever one step finished sooner than its neighbours. Now
        the whole ready set is dispatched and `self._slots` does the capping, so
        the fourth step starts the instant a slot frees.

        `already_completed` is what a resumed mission has done before: those
        steps are treated as satisfied dependencies and never re-run.
        """
        store = runstore.get_store()
        by_id: Dict[str, PlanStep] = {s.id: s for s in plan.steps}
        order = list(by_id)
        completed: Set[str] = {sid for sid in (already_completed or set()) if sid in by_id}
        pending: Set[str] = {sid for sid in by_id if sid not in completed}
        failed: Set[str] = set()
        results: List[Dict[str, Any]] = []
        running: Dict[str, asyncio.Task] = {}
        wave_index = 0
        halt_announced = False
        # Created here rather than in __init__ so it binds to the running loop,
        # the same reason `self._slots` is rebuilt per mission.
        halt_event = self._halt_events.setdefault(task_id, asyncio.Event())

        # Each promised filename belongs to the step that promised it, so a
        # step running alongside cannot overwrite it (see workspace.reserve).
        if hasattr(workspace, "reserve"):
            workspace.reserve({s.produces: s.id for s in plan.steps if s.produces})

        for sid in completed:
            by_id[sid].status = "completed"
            self._set_node_status(sid, "completed")
        if completed:
            await self._emit_dag(task_id, plan.source)

        while pending or running:
            # Halt gate, ahead of the budget gate because a halted mission is
            # not going to spend anything either way. `cancel_all` has already
            # aborted whatever call was in flight; this is the half that stops
            # the graph, by refusing to dispatch and cancelling what is running.
            #
            # Steps are failed rather than left pending: a halted step did not
            # produce its artifact, so everything downstream of it would starve
            # at an empty desk, and "pending forever" is not a state the store
            # or the panel has any way to draw.
            #
            # Announced once. `asyncio.wait` returns on the first completion, so
            # a halt with three steps in flight comes back round here with two
            # still running, and without the latch the office would get one
            # TASK_FAILED per straggler.
            if task_id in self._halted and not halt_announced:
                halt_announced = True
                for sid in sorted(pending):
                    by_id[sid].status = "failed"
                    self._set_node_status(sid, "failed")
                    store.upsert_step(task_id, by_id[sid], "failed",
                                      error="halted by emergency stop")
                    failed.add(sid)
                halted_count = len(pending) + len(running)
                pending.clear()
                for fut in running.values():
                    fut.cancel()
                await self._emit_dag(task_id, plan.source)
                await event_bus.emit(Event(
                    type=EventType.TASK_FAILED,
                    task_id=task_id,
                    data={"error": f"Halted by emergency stop. "
                                   f"{halted_count} step(s) stopped."}
                ))
                logger.warning("[%s] halted by emergency stop; %d step(s) stopped",
                               task_id, halted_count)
                if not running:
                    break

            # Budget gate. Checked before dispatching rather than mid-step: a
            # step already running has to be allowed to finish, or its artifact
            # is lost and everything downstream of it stalls anyway.
            spent = self._mission_tokens_spent(task_id)
            if pending and spent >= settings.max_tokens_budget_per_task:
                for sid in sorted(pending):
                    by_id[sid].status = "failed"
                    self._set_node_status(sid, "failed")
                    store.upsert_step(task_id, by_id[sid], "failed",
                                      error="token budget exhausted")
                    failed.add(sid)
                skipped = len(pending)
                pending.clear()
                await self._emit_dag(task_id, plan.source)
                await event_bus.emit(Event(
                    type=EventType.TASK_FAILED,
                    task_id=task_id,
                    data={"error": f"Token budget exhausted: {spent:,} of "
                                   f"{settings.max_tokens_budget_per_task:,} used. "
                                   f"{skipped} step(s) not started."}
                ))
                logger.warning("[%s] budget exhausted (%s tokens); %d step(s) skipped",
                               task_id, spent, skipped)

            ready = [sid for sid in sorted(pending, key=order.index)
                     if set(by_id[sid].dependencies) <= completed]

            if not ready and not running:
                # Nothing runnable and nothing in flight: everything left is
                # waiting on something that failed.
                for sid in sorted(pending):
                    by_id[sid].status = "failed"
                    self._set_node_status(sid, "failed")
                    store.upsert_step(task_id, by_id[sid], "failed",
                                      error="unreachable after an upstream failure")
                    failed.add(sid)
                if pending:
                    logger.warning("[%s] %d step(s) unreachable after a failure",
                                   task_id, len(pending))
                pending.clear()
                await self._emit_dag(task_id, plan.source)
                break

            for sid in ready:
                pending.discard(sid)
                by_id[sid].status = "running"
                self._set_node_status(sid, "running")
                # Written before the event goes out. A process killed in the gap
                # would otherwise have shown a step the database never heard of,
                # and a resume would run it a second time.
                store.upsert_step(task_id, by_id[sid], "running")
                running[sid] = asyncio.ensure_future(
                    self._run_step_scheduled(by_id[sid], workspace, task_id, goal))

            if ready:
                wave_index += 1
                await self._emit_dag(task_id, plan.source)
                if self.boss_agent and len(ready) > 1:
                    names = ", ".join(
                        self.agents[by_id[s].agent_id].name
                        for s in ready if by_id[s].agent_id in self.agents)
                    if names:
                        await self.boss_agent.speak(
                            f"Running {len(ready)} in parallel: {names}", task_id=task_id)

            if not running:
                continue

            # The kill switch waits alongside the steps. Without it the halt
            # gate at the top of the loop is only reached when a step happens to
            # finish, so a HALT during a stuck tool call would sit unread until
            # the step deadline expired.
            halt_waiter = asyncio.ensure_future(halt_event.wait())
            try:
                done, _ = await asyncio.wait(
                    set(running.values()) | {halt_waiter},
                    return_when=asyncio.FIRST_COMPLETED)
            finally:
                halt_waiter.cancel()
            # Only the halt fired: nothing to collect, go round and let the gate
            # deal with it.
            done.discard(halt_waiter)

            for finished in done:
                sid = next(k for k, v in running.items() if v is finished)
                del running[sid]
                step = by_id[sid]

                # Two ways a step arrives here halted, and both have to land in
                # the same place.
                #
                # `.cancelled()` is the scheduler having got there first, and it
                # must be asked before the outcome is read: `.exception()` on a
                # cancelled task *raises* CancelledError rather than returning
                # it, which would come out of the scheduler unhandled.
                #
                # The mission being halted is the other race, and the likelier
                # one in practice: `cancel_all` aborts the agent's in-flight
                # request, `BaseAgent.run` catches that and *returns* "Task
                # halted by emergency stop" -- a perfectly ordinary success as
                # far as the future is concerned. Taking it at face value files
                # that sentence as the step's artifact and lets the DAG carry on
                # reading it.
                #
                # Read from `_halted` rather than the announcement latch: the
                # halt can land while this very iteration is parked in the wait
                # below, so the gate at the top has not run yet. The cost is
                # that a step which genuinely finished a moment before the HALT,
                # and is collected in the same batch, is discarded with the
                # rest. That is the same reading resume already takes of a step
                # left `running` by a killed process -- unknown outcome, run it
                # again -- and it is the one that cannot file halt text as work.
                if finished.cancelled() or task_id in self._halted:
                    step.status = "failed"
                    self._set_node_status(sid, "failed")
                    store.upsert_step(task_id, step, "failed",
                                      error="halted by emergency stop")
                    failed.add(sid)
                    continue

                outcome = finished.exception() or finished.result()

                if isinstance(outcome, BaseException):
                    await self._record_step_failure(task_id, step, goal, outcome, store)
                    failed.add(sid)
                    continue

                step.status = "completed"
                completed.add(sid)
                self._set_node_status(sid, "completed")
                stats = self._step_telemetry.get(task_id, {}).get(sid, {})
                # The step still counts as done for the DAG -- stranding every
                # step behind a rejected one throws good downstream work away --
                # but a final verdict of "revise" is not a pass, and the record
                # says so. Stored as `unverified` rather than `completed`, so a
                # resume re-runs it instead of trusting it.
                verdict = outcome.get("verdict") if isinstance(outcome, dict) else None
                if stats:
                    stats["verdict"] = verdict
                    stats["quality"] = (outcome.get("quality")
                                        if isinstance(outcome, dict) else None)
                    stats["recovered"] = bool(isinstance(outcome, dict)
                                              and outcome.get("recovered"))
                store.finish_step(
                    task_id, sid, "unverified" if verdict == "revise" else "completed",
                    tokens=stats.get("prompt_tokens", 0) + stats.get("completion_tokens", 0),
                    cost_usd=stats.get("cost_usd", 0.0),
                    artifact_path=step.produces or "")
                results.append(outcome)

            await self._emit_dag(task_id, plan.source)
            await event_bus.emit(Event(
                type=EventType.TASK_PROGRESS,
                task_id=task_id,
                data={
                    "completed_step": len(completed),
                    "total_steps": len(by_id),
                    "wave": wave_index,
                    "failed": len(failed)
                }
            ))

        return results

    async def _record_step_failure(self, task_id: str, step: PlanStep, goal: str,
                                   outcome: BaseException, store) -> None:
        """
        One place where a failed step is written down.

        Was inline in the wave loop. Pulled out because a resumed mission and a
        timed-out step both reach it by different routes now, and three copies
        of "and don't forget to tell memory" is how one of them ends up missing
        it -- which is exactly the bug that once left success_rate at 1.0 for
        every agent in the office.
        """
        logger.error("Step %s (%s) raised: %s", step.id, step.agent_id, outcome,
                     exc_info=outcome if isinstance(outcome, Exception) else None)
        step.status = "failed"
        self._set_node_status(step.id, "failed")

        specialist = self._agent_by_id(step.agent_id)
        stats = dict(specialist.last_run or {}) if specialist else {}
        self._record_step_telemetry(task_id, step, stats)

        store.finish_step(
            task_id, step.id, "failed",
            tokens=stats.get("prompt_tokens", 0) + stats.get("completion_tokens", 0),
            cost_usd=stats.get("cost_usd", 0.0),
            error=f"{type(outcome).__name__}: {outcome}")

        await event_bus.emit(Event(
            type=EventType.TASK_FAILED,
            agent_id=step.agent_id,
            task_id=f"{task_id}_{step.id}",
            data={"step_id": step.id, "error": str(outcome),
                  "kind": type(outcome).__name__}
        ))

        # Failures have to reach memory too. memory.record lives at the end of
        # _run_step, which a raising step never gets to, so every episode ever
        # written said "success" -- leaving success_rate at 1.0 for everyone and
        # making the competence-based rerouting dead code.
        if settings.memory_enabled:
            memory.record(
                agent_id=step.agent_id,
                agent_name=specialist.name if specialist else step.agent_id,
                goal=goal,
                subtask=step.description,
                outcome="failure",
                summary=f"{type(outcome).__name__}: {outcome}"[:400],
                duration_sec=stats.get("duration_sec", 0.0),
                total_tokens=stats.get("tokens", 0),
            )

    async def _run_meeting(
        self,
        step: PlanStep,
        chair,
        subtask: Task,
        workspace: MissionWorkspace,
        context: str,
        hints: str,
        goal: str,
        task_id: str
    ) -> str:
        """
        Run a step as a meeting: gather the attendees, hear their objections,
        let the chair work with the room's own tools, then send everyone back.

        The attendees are held at the table for the whole call, so the office
        shows a group sitting together rather than one agent at a desk. They
        still do not each run a ReAct loop -- N loops would multiply cost for
        no more information, which is true of *reading*. It is not true of
        disagreeing, so each attendee gets exactly one bounded turn to object
        before the chair sums up (see `debate.py`). What makes it a meeting
        rather than a solo read is that review_artifacts and record_decision
        exist only here, that every attendee's work is on the table whether or
        not the step declared it in `consumes`, and that the room answers back.
        """
        from tools import meeting as meeting_tools

        attendee_agents = [ag for a in step.attendees
                           if (ag := self._agent_by_id(a)) is not None]
        attendees = [(ag.id, ag.name) for ag in attendee_agents]

        # Everything filed so far, minus what was already delivered as context.
        on_the_table = [n for n in workspace.names() if n not in (step.consumes or [])]
        room_context = context
        if on_the_table:
            extra = workspace.render_context(on_the_table)
            room_context = f"{context}\n\n{extra}" if context else extra

        roster = ", ".join(name for _, name in attendees) or "no one else"
        brief = (
            f"You are chairing a meeting at the conference table. "
            f"In the room with you: {roster}.\n"
            f"Use review_artifacts to read what they brought, and record_decision "
            f"to write down what the room agreed before you finish."
        )

        meeting_tools.bind_workspace(workspace, step.id)
        # file_ops too, not just the meeting tools. A chair whose own manifest
        # carries write_file will use it -- and with only meeting_tools bound
        # that write escaped the workspace and landed loose in backend/, giving
        # one artifact two copies: the workspace's and an orphan on disk.
        file_ops.bind_workspace(workspace, chair.id, chair.name, step.id)
        try:
            async with office.hold_meeting(
                "conference_table", attendees, task_id=task_id
            ):
                # The room speaks first. Inside the `hold_meeting` block, so
                # every objection is stated by someone who is actually sitting
                # at the table rather than shouting from their desk.
                positions = await debate.hold(
                    attendee_agents,
                    goal=goal,
                    step_description=step.description,
                    room_context=room_context,
                    task_id=task_id,
                    step_id=step.id,
                )
                floor = debate.render(positions)
                chair_context = "\n\n".join(
                    part for part in (brief, room_context, floor) if part)
                return await chair.run(
                    subtask,
                    context=chair_context,
                    hints=hints,
                    goal=goal,
                    # read_file and search_workspace as well as the table's own
                    # pair: a meeting asked to rule on "was the instruction in
                    # this file honoured" was chaired by the boss, who holds no
                    # tools, so the ruling came back "undetermined -- the file
                    # could not be obtained". A room that cannot open the
                    # document it is judging is not a review.
                    extra_tools=["review_artifacts", "record_decision",
                                 "read_file", "search_workspace"],
                )
        finally:
            meeting_tools.bind_workspace(None)
            file_ops.bind_workspace(None)

    async def _run_step(
        self,
        step: PlanStep,
        workspace: MissionWorkspace,
        task_id: str,
        goal: str
    ) -> Dict[str, Any]:
        """
        Assign one step, wait for the specialist, and file what it produced.

        The span is opened here rather than around the gather in `_execute_plan`
        because asyncio copies the context when it creates each task -- so every
        turn and tool call the specialist makes lands under this step, and two
        steps running in the same wave cannot end up as each other's children.
        """
        self._active_steps += 1
        self._peak_active_steps = max(self._peak_active_steps, self._active_steps)
        try:
            async with tracing.span(
                "step", f"{step.id}:{step.agent_id}",
                step_id=step.id, agent_id=step.agent_id, step_kind=step.kind
            ) as span:
                return await self._run_step_body(step, workspace, task_id, goal, span)
        finally:
            self._active_steps -= 1

    async def _dispatch_attempt(
        self,
        step: PlanStep,
        specialist,
        subtask_id: str,
        workspace: MissionWorkspace,
        task_id: str,
        goal: str,
        description: str,
        grant: Optional[List[str]],
        attempt: int
    ) -> str:
        """
        Hand the step to its specialist once and wait for the reply.

        Split out of `_run_step_body` so a repair can re-run it. A repair is a
        genuine re-dispatch -- same agent, same tools, same station walk, with
        the reviewer's complaint appended to the instruction -- rather than a
        second conversation bolted onto the first. The agent that has to fix the
        work should see the same brief the reader of its artifact saw.
        """
        subtask = Task(
            id=subtask_id,
            description=description,
            parent_task_id=task_id,
            assigned_to=step.agent_id
        )

        await event_bus.emit(Event(
            type=EventType.TASK_ASSIGNED,
            agent_id=step.agent_id,
            task_id=subtask_id,
            data={
                "assigned_to": step.agent_id,
                "agent_name": specialist.name,
                "subtask_description": step.description,
                "source_agent": "orchestrator",
                "step_id": step.id,
                "produces": step.produces,
                "consumes": step.consumes,
                "rationale": step.rationale,
                "routed_from": step.routed_from,
                "kind": step.kind,
                "attendees": step.attendees,
                # 1 on the original dispatch. Anything higher is a repair, and
                # the office should show it as one rather than as a new step.
                "attempt": attempt,
            }
        ))
        if self.boss_agent:
            if attempt > 1:
                await self.boss_agent.speak(
                    f"Sending {step.id} back to {specialist.name} for a revision",
                    task_id=task_id)
            elif step.kind == "meeting":
                names = ", ".join(
                    ag.name for a in step.attendees
                    if (ag := self._agent_by_id(a)) is not None
                ) or "the team"
                await self.boss_agent.speak(
                    f"Calling {specialist.name} and {names} to the conference room",
                    task_id=task_id)
            else:
                await self.boss_agent.speak(
                    f"Dispatched {step.id} to {specialist.name}", task_id=task_id)

        # The mission budget, readable from inside the agent's own loop, so a
        # step already running stops spending when the mission is out.
        budget_token = step_guard.bind_budget(
            lambda: (self._mission_tokens_spent(task_id), settings.max_tokens_budget_per_task))
        try:
            return await self._dispatch_under_lock(
                step, specialist, subtask, workspace, task_id, goal, grant)
        finally:
            step_guard.reset_budget(budget_token)

    async def _dispatch_under_lock(self, step, specialist, subtask, workspace,
                                   task_id, goal, grant):
        # One task per specialist at a time, even if the graph offers two.
        async with self._agent_lock(step.agent_id):
            await asyncio.sleep(0.3)  # Visual handoff pacing

            # Carry the upstream artifacts to this desk before work starts.
            if step.consumes:
                await workspace.deliver(step.consumes, step.agent_id, specialist.name, step.id)
            context = workspace.render_context(step.consumes)

            hints = ""
            if settings.memory_enabled:
                hints = memory.render_hints(step.agent_id, f"{goal} {step.description}")
                if hints:
                    await event_bus.emit(Event(
                        type=EventType.MEMORY_RECALLED,
                        agent_id=step.agent_id,
                        task_id=subtask.id,
                        data={
                            "step_id": step.id,
                            "agent_name": specialist.name,
                            "episodes": len(memory.recall(step.agent_id, f"{goal} {step.description}")),
                            "preview": hints[:220]
                        }
                    ))

            if step.kind == "meeting":
                return await self._run_meeting(
                    step, specialist, subtask, workspace, context, hints, goal, task_id)

            # write_file files into the workspace for the length of this
            # step, so whatever the agent writes is attributed to it.
            file_ops.bind_workspace(
                workspace, step.agent_id, specialist.name, step.id)
            try:
                try:
                    return await specialist.run(
                        subtask, context=context, hints=hints, goal=goal,
                        extra_tools=grant, produces=step.produces)
                except TypeError:
                    return await specialist.run(
                        subtask, context=context, hints=hints, goal=goal,
                        extra_tools=grant)
            finally:
                file_ops.bind_workspace(None)

    async def _file_artifact(
        self,
        workspace: MissionWorkspace,
        step: PlanStep,
        specialist,
        before: Set[str],
        before_content: Optional[str],
        result_text: str
    ) -> str:
        """
        File what this attempt produced, and return the text to be judged.

        `before_content` is what stood under the promised name when the attempt
        started. A repair usually rewrites that same file, and `_best_artifact`
        compares *names* -- so an in-place rewrite looks like nothing new was
        written at all, and the repair would be silently discarded. Comparing
        the content is what catches it.
        """
        if not step.produces:
            return result_text

        current = workspace.get(step.produces)
        if (current is not None and current.content != before_content
                and current.step_id == step.id):
            # Rewritten in place, under the name the plan promised.
            return current.content

        # Prefer what the agent actually wrote over what it said about it.
        # `result_text` is the closing chat message -- filing that as the
        # artifact meant downstream steps consumed "Task completed after 4
        # execution steps." instead of the 2.4KB report the agent produced.
        written = self._best_artifact(workspace, step, before)
        if written is not None:
            if written.name != step.produces:
                # Re-file under the name the plan promised, so `consumes`
                # on the next step still resolves.
                await workspace.put(
                    name=step.produces,
                    content=written.content,
                    author_id=step.agent_id,
                    author_name=specialist.name,
                    step_id=step.id
                )
            return written.content

        if current is not None:
            # A repair that wrote nothing new. Keep what the previous attempt
            # filed rather than replacing real work with a chat message.
            return current.content

        await workspace.put(
            name=step.produces,
            content=result_text,
            author_id=step.agent_id,
            author_name=specialist.name,
            step_id=step.id
        )
        return result_text

    async def _recover_missing_artifact(
        self,
        workspace: MissionWorkspace,
        step: PlanStep,
        specialist,
        goal: str,
        failures: List[str]
    ) -> Optional[str]:
        """
        Synthesize a promised deliverable if the specialist ran out of turns or failed contracts.

        Reads the available workspace artifacts and calls the primary model to produce
        the full file directly, preventing 39-byte stubs or blank files from breaking
        the mission outcome.

        What it files is a stand-in, and is marked as one: a banner in the file,
        `recovered: true` on the step, and a verdict that stays "revise" so the
        mission ends `unverified`. The model is held to the documents it is
        given, because the step that should have gathered the facts did not.
        """
        if not step.produces or not llm_configured():
            return None

        # Check if there is context to synthesize from
        context = workspace.render_context(step.consumes) if step.consumes else ""
        if not context:
            context = workspace.render_context(workspace.names())
        if not context.strip():
            return None

        produces_lower = step.produces.lower()
        if produces_lower.endswith(".json"):
            format_instruction = (
                f"Generate the full, complete JSON file for '{step.produces}'. Extract real data values "
                f"from the workspace documents and do not emit null placeholders for requested attributes. "
                f"Output valid JSON only, without markdown fence or prose."
            )
            system_role = "You are a precise data extraction specialist generating clean, complete JSON."
        elif produces_lower.endswith(".csv"):
            format_instruction = (
                f"Generate the full, complete CSV file for '{step.produces}'. Output valid CSV with "
                f"header row and real data rows, without placeholder N/As or empty fields."
            )
            system_role = "You are a precise data extraction specialist generating clean, complete CSV."
        else:
            format_instruction = (
                f"Write the Markdown deliverable for '{step.produces}' from the documents above. "
                f"Output the file contents directly."
            )
            system_role = ("You assemble a deliverable strictly from the documents you are given, "
                           "and you never add information that is not in them.")

        prompt = (
            f"Mission Goal: {goal}\n"
            f"Deliverable Filename: {step.produces}\n"
            f"Task Requirement:\n{step.description}\n\n"
            f"Prior Contract / Quality Failures to Address:\n"
            + "\n".join(f"- {f}" for f in failures) + "\n\n"
            f"Available Workspace Documents:\n{context}\n\n"
            f"Instructions:\n"
            f"{format_instruction}\n\n"
            # The failure this exists to stop is invention. The step that should
            # have written this file did not, so there is nothing behind it but
            # these documents -- and a model told to produce "the full, complete
            # deliverable" with nothing to produce it from wrote a data summary,
            # sources and meeting quotes out of thin air.
            f"RULES:\n"
            f"- Use only facts, figures, names, URLs and quotations that appear in the "
            f"documents above. Invent none of them.\n"
            f"- Do not describe work, runs, meetings or tool results the documents do "
            f"not record.\n"
            f"- Where the documents do not contain what the task requires, say plainly "
            f"what is missing instead of filling the gap."
        )

        try:
            logger.info("[%s] attempting artifact recovery for '%s'", specialist.name, step.produces)
            result = await llm.acompletion(
                messages=[
                    {"role": "system", "content": system_role},
                    {"role": "user", "content": prompt}
                ],
                model=settings.default_model,
                temperature=0.2,
                max_tokens=settings.max_tokens,
            )
            text = (result.response.choices[0].message.content or "").strip()
            if produces_lower.endswith(".json") and text.startswith("```"):
                text = re.sub(r"^```[a-zA-Z0-9_-]*\n?", "", text)
                text = re.sub(r"\n?```$", "", text).strip()

            if text and len(text) > 40:
                if not produces_lower.endswith((".json", ".csv")):
                    # Said in the file itself, where every later reader sees it:
                    # the next step, the synthesis, and a person opening it.
                    text = RECOVERY_BANNER + text
                await workspace.put(
                    name=step.produces,
                    content=text,
                    author_id=step.agent_id,
                    author_name=specialist.name,
                    step_id=step.id
                )
                logger.info("[%s] successfully recovered artifact '%s' (%d chars)", specialist.name, step.produces, len(text))
                return text
        except Exception as exc:
            logger.warning("[%s] artifact recovery failed: %s", specialist.name, exc)

        return None

    async def _emit_critique(
        self,
        task_id: str,
        step: PlanStep,
        specialist,
        verdict: str,
        score: Optional[float],
        reasons: List[str],
        attempt: int,
        source: str,
        repairing: bool
    ):
        await event_bus.emit(Event(
            type=EventType.STEP_CRITIQUED,
            agent_id=step.agent_id,
            task_id=f"{task_id}_{step.id}",
            data={
                "step_id": step.id,
                "agent_name": specialist.name,
                "verdict": verdict,
                "score": round(score, 3) if score is not None else None,
                "reasons": reasons[:5],
                "attempt": attempt,
                # "contract" is the regex layer, "critic" the model. Worth
                # distinguishing in the log: one is free and certain, the other
                # costs a call and is a judgement.
                "source": source,
                # False on a "revise" means the budget was spent, so the step
                # completes with work its reviewer rejected. That has to be
                # visible, or a capped repair looks like an accepted one.
                "repairing": repairing,
                "artifact": step.produces,
            }
        ))

    async def _run_step_body(
        self,
        step: PlanStep,
        workspace: MissionWorkspace,
        task_id: str,
        goal: str,
        span: Any
    ) -> Dict[str, Any]:
        specialist = self._agent_by_id(step.agent_id)
        if not specialist:
            raise KeyError(f"step {step.id} names unknown agent '{step.agent_id}'")

        subtask_id = f"{task_id}_{step.id}"
        # A step that promises an artifact needs the means to write one. The
        # researcher holds only web_search and read_file, so being told to file
        # its findings was an instruction it could not follow -- it returned no
        # text at all and the step filed a 39-byte stub as the research the
        # rest of the mission was built on. Granted per step through the same
        # mechanism meetings use, rather than by widening any manifest.
        grant = ["write_file"] if (step.produces and "write_file" not in specialist.tools) else None

        # Name the deliverable in the task itself. The plan promises this step
        # produces a specific artifact, and an agent that is never told that
        # has no reason to write one -- observed: a researcher did its work
        # entirely through tool calls, returned empty content, and the step
        # filed base_agent's "Task completed after N execution steps." stub as
        # the research the rest of the mission was supposed to build on.
        description = step.description
        if step.produces:
            description = (
                f"{step.description}\n\n"
                f"Deliverable: write your full output to `{step.produces}` with the "
                f"write_file tool before you finish. Put the actual content in that "
                f"file -- the next step reads the file, not your reply.\n"
                # A person opens this file first. One produced for a "best XI"
                # goal opened with four SQL blocks and never showed the eleven
                # together: correct, and unreadable.
                f"Lead with the answer. Open the file with a short summary a "
                f"reader can take in at once -- the result itself, as a table or "
                f"a short list when there is more than one figure, name or pick. "
                f"Method, SQL, evidence and caveats come after it, and anything "
                f"you could not obtain is stated plainly at the end.\n"
                # A report once named the top scorer "Üñïcodé Plåyer 9971"; the
                # file says "Ünïcodé". The tool returned it correctly and the
                # model retyped it. Names, ids and codes are the fields where
                # that is silent and wrong rather than merely untidy.
                f"Copy names, identifiers, codes and quoted text character for "
                f"character from the tool output you were given. Do not retype "
                f"them from memory, correct their spelling, or normalise their "
                f"accents, capitals or leading zeros."
            )

        # Citations are asked for only when they were obtainable. Whether a
        # source could be fetched at all depends on this agent's tool grants and
        # on a Tavily key being set, and a contract that demands one regardless
        # fails every offline run for a reason the agent cannot fix.
        spec = contracts.infer_spec(
            step.produces, step.description,
            # fetch_url counts as well: an agent that can open a page it was
            # given can cite it, and demanding citations only of searchers made
            # the bar depend on which of two reaching-out tools was granted.
            citations_expected=bool(settings.tavily_api_key)
            and bool({"web_search", "fetch_url"} & set(specialist.tools)),
        )

        feedback = ""
        attempt = 0
        quality: Optional[float] = None
        verdict = "accept"
        recovered = False
        stats: Dict[str, Any] = {}
        result_text = ""
        artifact_text = ""

        while True:
            attempt += 1
            before = set(workspace.names())
            existing = workspace.get(step.produces) if step.produces else None
            before_content = existing.content if existing is not None else None

            async with tracing.span(
                "attempt", f"{step.id} attempt {attempt}",
                step_id=step.id, attempt=attempt, repair=attempt > 1
            ) as attempt_span:
                result_text = await self._dispatch_attempt(
                    step, specialist, subtask_id, workspace, task_id, goal,
                    f"{description}\n\n{feedback}" if feedback else description,
                    grant, attempt)
                attempt_span.set(chars=len(result_text or ""))

            stats = dict(specialist.last_run or {})
            self._record_step_telemetry(task_id, step, stats)
            artifact_text = await self._file_artifact(
                workspace, step, specialist, before, before_content, result_text)

            # A re-dispatch is only worth paying for if something could come
            # back different. The built-in simulator returns the same canned
            # reply to the same input, so repairing it spends the budget to
            # receive a byte-identical artifact. The verdict is still emitted,
            # so the office shows the work was judged and found wanting.
            simulated = bool(stats.get("estimated"))
            budget_left = attempt <= settings.max_repair_attempts
            can_repair = budget_left and not simulated

            check = (contracts.check(artifact_text, spec) if settings.contracts_enabled
                     else contracts.CheckResult(ok=True))
            if not check.ok:
                verdict = "revise"
                await self._emit_critique(
                    task_id, step, specialist, verdict="revise", score=None,
                    reasons=check.failures, attempt=attempt, source="contract",
                    repairing=can_repair)
                if can_repair:
                    feedback = check.as_guidance()
                    continue
                if step.produces:
                    recovered_text = await self._recover_missing_artifact(
                        workspace, step, specialist, goal, check.failures)
                    if recovered_text:
                        # Filed so downstream steps are not stranded, but the
                        # verdict stays "revise". This used to be "accept", with
                        # no reviewer ever reading the result: observed, a
                        # recovered scouting report with 150 invented records,
                        # made-up URLs and quotes from a meeting that never
                        # happened, marked as a passing step.
                        artifact_text = recovered_text
                        recovered = True
                break

            critique = await critic.critique(goal, step.description, artifact_text)
            if critique.skipped:
                verdict = "accept"
                break

            quality = critique.score
            verdict = critique.verdict
            await self._emit_critique(
                task_id, step, specialist, verdict=critique.verdict,
                score=critique.score, reasons=critique.reasons, attempt=attempt,
                source="critic",
                repairing=can_repair and critique.verdict == "revise")
            if critique.verdict == "revise" and can_repair:
                feedback = critique.guidance
                continue
            break

        span.set(
            model=stats.get("model", ""),
            cost_usd=stats.get("cost_usd", 0.0),
            total_tokens=stats.get("tokens", 0),
            retries=stats.get("retries", 0),
            estimated=bool(stats.get("estimated")),
            tools=len(stats.get("tools_used", []) or []),
            attempts=attempt,
            verdict=verdict,
            quality=quality,
            recovered=recovered,
        )

        if settings.memory_enabled:
            memory.record(
                agent_id=step.agent_id,
                agent_name=specialist.name,
                goal=goal,
                subtask=step.description,
                outcome=stats.get("outcome", "success"),
                summary=result_text[:400],
                duration_sec=stats.get("duration_sec", 0.0),
                total_tokens=stats.get("tokens", 0),
                tools_used=stats.get("tools_used", []),
                artifact=step.produces,
                # None when nothing judged this run -- an unscored episode is
                # not a perfect one, and WP5's routing has to be able to tell.
                quality=quality,
            )

        return {
            "step_id": step.id,
            "agent_id": step.agent_id,
            "agent_name": specialist.name,
            "subtask": step.description,
            "rationale": step.rationale,
            "routed_from": step.routed_from,
            "produces": step.produces,
            "consumes": step.consumes,
            "result": result_text,
            "duration_sec": stats.get("duration_sec", 0.0),
            "tools_used": stats.get("tools_used", []),
            "attempts": attempt,
            "verdict": verdict,
            "quality": quality,
            # The artifact was written by the harness, not by this step's agent.
            "recovered": recovered,
        }

    # ---------------------------------------------------------------- synthesis

    @staticmethod
    def _slugify_goal(prompt: str, max_words: int = 4) -> str:
        """Convert a user goal prompt into a clean, human-readable slug for folder naming."""
        filler = {
            "create", "make", "a", "an", "the", "me", "please", "for", "and", "or",
            "to", "in", "of", "with", "from", "give", "tell", "about", "this", "is",
            "there", "find", "get", "run", "do", "now", "so", "that", "on", "across",
            "complete", "report", "build", "all", "time", "definitive", "using", "by"
        }
        cleaned = re.sub(r"https?://\S+", "", prompt)
        words = re.findall(r"[A-Za-z0-9]+", cleaned.lower())
        filtered = [w for w in words if w not in filler and len(w) > 1]
        selected = filtered[:max_words] if filtered else words[:max_words]
        slug = "_".join(selected) if selected else "mission"
        return slug[:28].strip("_")

    def _update_missions_index(self) -> None:
        """Maintain a human-readable Markdown index of all missions in backend/data/missions/INDEX.md."""
        try:
            missions_dir = settings.missions_path
            if not os.path.exists(missions_dir):
                return

            rows = []
            for entry in sorted(os.listdir(missions_dir), key=lambda x: os.path.getmtime(os.path.join(missions_dir, x)), reverse=True):
                full_path = os.path.join(missions_dir, entry)
                if not os.path.isdir(full_path):
                    continue

                m_json = os.path.join(full_path, "mission.json")
                goal = "Unknown Goal"
                outcome = "unknown"

                if os.path.exists(m_json):
                    try:
                        with open(m_json, "r", encoding="utf-8") as f:
                            m_data = json.load(f)
                            goal = m_data.get("goal", "").strip() or goal
                            outcome = m_data.get("outcome", "unknown")
                    except Exception:
                        pass

                files = [f for f in os.listdir(full_path) if not f.startswith(".")]
                deliverables = [
                    f for f in files
                    if f not in ("mission.json", "trace.jsonl", "report.md")
                    and not f.endswith(".tmp")
                ]

                if deliverables:
                    links = "<br>".join(f"[`{d}`](./{entry}/{d})" for d in deliverables[:4])
                    if len(deliverables) > 4:
                        links += f"<br>*(+{len(deliverables) - 4} more)*"
                else:
                    links = f"[`report.md`](./{entry}/report.md)"

                status_badge = "✅ Completed" if outcome == "completed" else f"⚠️ {outcome.title()}"
                mtime_str = time.strftime("%Y-%m-%d %H:%M", time.localtime(os.path.getmtime(full_path)))
                clean_goal = (goal[:90] + "...") if len(goal) > 90 else goal

                rows.append(
                    f"| {mtime_str} | [`{entry}`](./{entry}/) | {clean_goal} | {links} | {status_badge} |"
                )

            table_content = (
                "# 📂 AgentHQ Missions Catalog\n\n"
                "Auto-updated mission index. Click on any folder or deliverable link to inspect deliverables directly.\n\n"
                "| Timestamp | Mission Folder | Task Objective / Goal | Primary Deliverables | Status |\n"
                "| :--- | :--- | :--- | :--- | :---: |\n"
                + "\n".join(rows) + "\n"
            )

            index_path = os.path.join(missions_dir, "INDEX.md")
            readme_path = os.path.join(missions_dir, "README.md")
            with open(index_path, "w", encoding="utf-8") as f:
                f.write(table_content)
            with open(readme_path, "w", encoding="utf-8") as f:
                f.write(table_content)
        except Exception as exc:
            logger.warning("Failed to update missions index: %s", exc)

    async def _draw_conclusion(
        self,
        user_prompt: str,
        plan: Plan,
        workspace: MissionWorkspace
    ) -> synthesis.Conclusion:
        """
        Ask the model what the mission actually found.

        Reads the artifact bodies, not the steps' closing chat messages: the
        message is "Task completed after 4 execution steps", the artifact is the
        work. Failed steps are named so the conclusion can say what is missing
        rather than concluding confidently around a hole.
        """
        artifacts = []
        for name in workspace.names():
            art = workspace.get(name)
            if art is not None:
                artifacts.append((name, art.content or ""))

        failures = [f"{s.id} ({s.agent_id}): {s.description[:120]}"
                    for s in plan.steps if s.status == "failed"]
        # Finished but rejected by its reviewer. Named alongside the failures so
        # the conclusion treats that step's artifact as unreliable rather than
        # building on it.
        failures += [f"{s.id} ({s.agent_id}): finished, but its reviewer's final "
                     f"verdict was 'revise' -- treat its artifact as unverified"
                     for s in self._unverified_steps(workspace.mission_id, plan)]
        return await synthesis.conclude(user_prompt, artifacts, failures)

    def _unverified_steps(self, task_id: str, plan: Plan) -> List[PlanStep]:
        """Completed steps whose final review verdict was 'revise'."""
        rows = self._step_telemetry.get(task_id, {})
        return [s for s in plan.steps
                if s.status == "completed" and rows.get(s.id, {}).get("verdict") == "revise"]

    async def _synthesize_results(
        self,
        user_prompt: str,
        results: List[Dict[str, Any]],
        duration_sec: float,
        plan: Plan,
        workspace: MissionWorkspace
    ) -> str:
        """Create a polished executive Markdown summary."""
        planner_label = "LLM planner" if plan.source == "llm" else "heuristic planner"
        rerouted = [r for r in results if r.get("routed_from")]
        regranted = [s for s in plan.steps if getattr(s, "regranted_from", None)]
        conclusion = await self._draw_conclusion(user_prompt, plan, workspace)
        # Read by the manifest, which is written by the caller. Held here rather
        # than threaded back through the return value because the briefing is a
        # string by contract and three tests read it as one -- and the same
        # single-mission-at-a-time assumption already holds for `self._slots`
        # and the tracing stack.
        self._last_ungrounded = list(conclusion.ungrounded)
        unverified = self._unverified_steps(workspace.mission_id, plan)
        unexecuted: Optional[List[str]] = None
        if getattr(self, "_provenance_checkable", True):
            unexecuted = provenance.unexecuted_sql(
                [(n, a.content or "") for n, a in workspace.artifacts.items()],
                workspace.executed_queries)
        self._last_unexecuted_sql = unexecuted

        # A reader opens this to find out what was asked and what the answer
        # is. Everything else -- the graph, the handoffs, the run record -- is
        # how the answer was reached, and it used to sit at the same level as
        # the answer itself, which made a finished mission read as a log.
        asked = " ".join((user_prompt or "").split())
        lines = [
            "# 🎯 AgentHQ Mission Briefing",
            "",
            f"**You asked:** {asked[:400]}{'…' if len(asked) > 400 else ''}",
            "",
            "---",
        ]

        if not conclusion.skipped:
            lines.append("## ✅ The answer\n")
            lines.append(conclusion.text)
            if conclusion.ungrounded:
                # Named, not hidden, and not grounds for throwing the conclusion
                # away: $98 minus $61 is $37, and arithmetic on grounded numbers
                # is what a good summary does. A reader can check a named figure
                # in seconds; an unnamed wrong one is the one that gets quoted.
                shown = ", ".join(f"`{f}`" for f in conclusion.ungrounded)
                verb = "appears" if len(conclusion.ungrounded) == 1 else "appear"
                lines.append("")
                lines.append(
                    f"> ⚠️ **Check before quoting:** {shown} {verb} in the "
                    f"conclusion above but in none of the artifacts it was "
                    f"drawn from. Either derived from figures that are there, "
                    f"or wrong.")
            lines.append("")
            lines.append("---")

        # One line on whether the answer can be trusted, and what was produced.
        # Both used to be at the bottom, under the audit trail.
        completed = sum(1 for st in plan.steps if st.status == "completed")
        failed = sum(1 for st in plan.steps if st.status == "failed")
        if failed:
            status = (f"❌ **{failed} of {len(plan.steps)} steps failed.** "
                      f"Treat this answer as incomplete.")
        elif unverified:
            status = (f"⚠️ **Unverified.** All {completed} steps ran, but a reviewer "
                      f"rejected {', '.join(f'`{u.id}`' for u in unverified)}. "
                      f"Check those figures before relying on them.")
        else:
            status = f"✅ **Verified.** All {completed} steps ran and passed review."
        lines.append(status)
        lines.append("")

        artifacts = workspace.summary()
        if artifacts:
            lines.append("**Files produced:** " + ", ".join(
                f"`{a['name']}`" for a in artifacts))
            lines.append("")
        lines.append("---")

        if unexecuted:
            # Placed with the finding, not in the audit trail: a query printed
            # beside a figure is the reader's evidence for that figure.
            lines.append("## ⚠️ SQL no step ran\n")
            lines.append("These queries appear in the artifacts, but no successful "
                         "`query_data` call in this mission executed them. Any "
                         "figure shown with one was not produced by it.\n")
            lines.extend(f"- `{q}`" for q in unexecuted)
            lines.append("")
            lines.append("---")

        # Everything below is evidence, not the answer. Folded so the page ends
        # at the answer for a reader who wants only that, and opens in one click
        # for a reader who wants to check it. Markdown viewers that do not
        # render HTML simply show it all, which is the old behaviour.
        lines.append(f"<details><summary><b>How this was produced</b> — "
                     f"{len(results)} specialist(s), {duration_sec:.0f}s, "
                     f"{planner_label}, and every file produced</summary>\n")

        lines.append("## 🗺️ Execution Graph\n")

        for step in plan.steps:
            deps = ", ".join(step.dependencies) if step.dependencies else "start"
            name = self.agents[step.agent_id].name if step.agent_id in self.agents else step.agent_id
            marker = {"completed": "✅", "failed": "❌", "running": "⏳"}.get(step.status, "•")
            if any(u.id == step.id for u in unverified):
                marker = "⚠️"
            lines.append(f"- {marker} `{step.id}` **{name}** — {step.description[:90]} "
                         f"_(after: {deps} → produces `{step.produces}`)_")

        if rerouted:
            lines.append("")
            lines.append("**Memory rerouted this mission:** " + "; ".join(
                f"`{r['step_id']}` {r['routed_from']} → {r['agent_id']}" for r in rerouted
            ))

        if regranted:
            lines.append("")
            lines.append("**Reassigned for tool access:** " + "; ".join(
                f"`{s.id}` {s.regranted_from} → {s.agent_id} "
                f"(needs {', '.join(s.needs_tools)})" for s in regranted
            ))

        lines.append("")
        lines.append("---")
        lines.append("## 📋 Specialist Execution Handoffs\n")

        for item in results:
            lines.append(f"### {item['agent_name']}  ·  `{item['step_id']}`")
            lines.append(f"> **Assigned Subtask:** {item['subtask']}")
            if item.get("consumes"):
                lines.append(f"> **Received:** {', '.join('`%s`' % c for c in item['consumes'])}")
            if item.get("tools_used"):
                lines.append(f"> **Tools:** {', '.join('`%s`' % t for t in item['tools_used'])}")
            lines.append("")
            lines.append(f"{item['result']}\n")

        if artifacts:
            lines.append("---")
            lines.append("### 🗂️ Workspace Artifacts")
            for a in artifacts:
                lines.append(f"- `{a['name']}` — {a['size_chars']} chars, by {a['author_name']}")

        lines.append("")
        lines.append("---")
        # Deliberately bookkeeping, and only bookkeeping. This used to close on
        # "System state is healthy and ready for the next command", which is a
        # claim about the scheduler dressed as a claim about the work -- it
        # printed identically for a mission that answered the goal and one where
        # every specialist filed a stub. The finding is at the top now; what
        # belongs here is what can be counted.
        lines.append("### 📊 Run Record")
        if failed:
            lines.append(f"{completed} of {len(plan.steps)} steps completed; **{failed} failed**. "
                         f"Review the failed branches before acting on this briefing.")
        elif unverified:
            lines.append(f"All {completed} steps ran, but **{len(unverified)} ended with a "
                         f"'revise' verdict**: {', '.join(f'`{s.id}`' for s in unverified)}. "
                         f"Their artifacts are unverified.")
        else:
            lines.append(f"All {completed} steps completed and every artifact filed.")

        if conclusion.skipped:
            # Named, not hidden. A briefing with no conclusion and a briefing
            # whose conclusion was that everything is fine must not look alike.
            lines.append("")
            lines.append(f"_No conclusion was drawn: {conclusion.reason_skipped}. "
                         f"The artifacts above are the mission's output._")

        lines.append("")
        lines.append("</details>")

        return "\n".join(lines)


# Global singleton Orchestrator instance
orchestrator = Orchestrator()
