import asyncio
import glob
import json
import logging
import os
import time
import uuid
from typing import Any, Dict, List, Optional, Set

import contracts
import critic
import pricing
import tracing
from base_agent import BaseAgent
from config import settings
from event_bus import event_bus
from memory import memory
from office import office
from planner import planner
from schemas import (AgentManifest, AgentStatus, DAGNode, Event, EventType, Plan,
                     PlanStep, Task)
from tools import file_ops
from workspace import MissionWorkspace

logger = logging.getLogger("AgentHQ.Orchestrator")


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
        # One agent runs one subtask at a time even when the DAG offers it two.
        self._agent_locks: Dict[str, asyncio.Lock] = {}
        self._load_agents()

    def _load_agents(self):
        """Discover and load all agent JSON manifests from the agents directory."""
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
        """Created lazily so no lock binds to the import-time event loop."""
        if agent_id not in self._agent_locks:
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
        """Emergency kill switch - halt all running agents."""
        logger.warning("Emergency kill switch activated! Cancelling all agents.")
        if self.boss_agent:
            self.boss_agent.cancel()
        for agent in self.agents.values():
            agent.cancel()

    # ------------------------------------------------------------------ mission

    async def handle_user_input(self, user_prompt: str) -> Dict[str, Any]:
        """
        Main entry point: plan the goal, run the graph, synthesise the briefing,
        and stream the whole thing to the office as it happens.

        Thin on purpose. The trace is opened here and closed in the `finally`,
        so a mission that raises halfway still leaves a readable trace.jsonl --
        which is then the only record of how far it got. The work itself is in
        `_run_mission`, running inside the mission span.
        """
        if not self.agents:
            self._load_agents()

        task_id = f"task_{uuid.uuid4().hex[:8]}"
        workspace = MissionWorkspace(task_id)
        self.workspaces[task_id] = workspace
        self._step_telemetry[task_id] = {}

        trace = tracing.start_mission(task_id, workspace.dir)
        try:
            async with tracing.span("mission", user_prompt[:120],
                                    task_id=task_id) as node:
                result = await self._run_mission(task_id, workspace, user_prompt)
                node.set(cost_usd=result.get("cost_usd", 0.0),
                         steps=len(result.get("subtasks") or []))
                return result
        finally:
            tracing.end_mission()

    async def _run_mission(
        self,
        task_id: str,
        workspace: MissionWorkspace,
        user_prompt: str
    ) -> Dict[str, Any]:
        """The mission proper, run inside the mission span its caller opened."""
        main_task = Task(id=task_id, description=user_prompt, status="in_progress")
        self.active_tasks[task_id] = main_task

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

        # 2. Plan.
        plan = await planner.plan(user_prompt, self.get_manifests())
        self._apply_memory_routing(plan, task_id)
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

        results = await self._execute_plan(plan, workspace, task_id, user_prompt)

        # 4. Synthesise.
        if self.boss_agent:
            await self.boss_agent.update_status(AgentStatus.THINKING)
            await self.boss_agent.speak("Synthesizing final executive briefing...", task_id=task_id)

        duration = time.time() - start_time
        synthesis_text = self._synthesize_results(user_prompt, results, duration, plan, workspace)

        main_task.status = "completed"
        main_task.result = synthesis_text

        # Put the run on disk before announcing it. The websocket payload is
        # the only other copy, so a browser that is closed or reloaded at this
        # moment would otherwise take the whole mission with it.
        telemetry = self._mission_telemetry(task_id, plan)

        workspace.save_report(synthesis_text)
        workspace.save_manifest({
            "mission_id": task_id,
            "goal": user_prompt,
            "plan_source": plan.source,
            "duration_seconds": round(duration, 2),
            "total_tokens": self._tokens_spent(),
            "token_budget": settings.max_tokens_budget_per_task,
            # `total_tokens` above is the roster's lifetime figure, which the
            # budget gate reads; `mission_tokens` is what this run alone spent.
            "mission_tokens": telemetry["mission_tokens"],
            "cost_usd": telemetry["cost_usd"],
            "unpriced_models": telemetry["unpriced_models"],
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
                "plan_source": plan.source,
                "artifacts": workspace.summary(),
                "mission_dir": workspace.dir,
                "cost_usd": telemetry["cost_usd"],
                # False here does not mean free -- it means the numbers came
                # from a provider rather than the simulator.
                "tokens_estimated": telemetry["tokens_estimated"],
            }
        ))

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
        }

    # ------------------------------------------------------------------ routing

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

        candidates = list(self.agents.keys())
        for step in plan.steps:
            suggestion = memory.suggest_agent(step.description, candidates, step.agent_id)
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
        """
        fresh = [workspace.get(n) for n in workspace.names() if n not in before]
        fresh = [a for a in fresh if a is not None]
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
        await event_bus.emit(Event(
            type=EventType.DAG_UPDATE,
            task_id=task_id,
            data={
                "dag": [node.model_dump() for node in self.current_dag],
                "source": source
            }
        ))

    def _set_node_status(self, step_id: str, status: str):
        for node in self.current_dag:
            if node.id == step_id:
                node.status = status

    async def _execute_plan(
        self,
        plan: Plan,
        workspace: MissionWorkspace,
        task_id: str,
        goal: str
    ) -> List[Dict[str, Any]]:
        """
        Run the graph in waves.

        Every step whose dependencies are satisfied starts together, capped at
        `max_parallel_agents`. That is the whole reason the office looks busy:
        three desks light up at once instead of one at a time.
        """
        by_id: Dict[str, PlanStep] = {s.id: s for s in plan.steps}
        pending: Set[str] = set(by_id)
        completed: Set[str] = set()
        failed: Set[str] = set()
        results: List[Dict[str, Any]] = []
        wave_index = 0

        while pending:
            ready = [
                sid for sid in sorted(pending, key=lambda s: list(by_id).index(s))
                if set(by_id[sid].dependencies) <= completed
            ]
            if not ready:
                # Everything left is waiting on something that failed.
                for sid in pending:
                    self._set_node_status(sid, "failed")
                    by_id[sid].status = "failed"
                await self._emit_dag(task_id, plan.source)
                logger.warning(f"[{task_id}] {len(pending)} step(s) unreachable after a failure")
                break

            # Budget gate. max_tokens_budget_per_task was broadcast to the UI
            # and enforced nowhere, which was harmless while every run was
            # simulated and is not once a paid key is in play. Checked between
            # waves rather than mid-step: a step already dispatched has to be
            # allowed to finish, or its artifact is lost and the graph stalls.
            spent = self._tokens_spent()
            if spent >= settings.max_tokens_budget_per_task:
                for sid in pending:
                    self._set_node_status(sid, "failed")
                    by_id[sid].status = "failed"
                    failed.add(sid)
                await self._emit_dag(task_id, plan.source)
                await event_bus.emit(Event(
                    type=EventType.TASK_FAILED,
                    task_id=task_id,
                    data={"error": f"Token budget exhausted: {spent:,} of "
                                   f"{settings.max_tokens_budget_per_task:,} used. "
                                   f"{len(pending)} step(s) not started."}
                ))
                logger.warning("[%s] budget exhausted (%s tokens); %d step(s) skipped",
                               task_id, spent, len(pending))
                pending.clear()
                break

            limit = settings.max_parallel_agents if settings.parallel_enabled else 1
            wave = ready[:limit]
            wave_index += 1

            for sid in wave:
                pending.discard(sid)
                by_id[sid].status = "running"
                self._set_node_status(sid, "running")
            await self._emit_dag(task_id, plan.source)

            if self.boss_agent and len(wave) > 1:
                names = ", ".join(
                    self.agents[by_id[s].agent_id].name
                    for s in wave if by_id[s].agent_id in self.agents
                )
                await self.boss_agent.speak(f"Running {len(wave)} in parallel: {names}", task_id=task_id)

            outcomes = await asyncio.gather(
                *[self._run_step(by_id[sid], workspace, task_id, goal) for sid in wave],
                return_exceptions=True
            )

            for sid, outcome in zip(wave, outcomes):
                step = by_id[sid]
                if isinstance(outcome, Exception):
                    logger.error(f"Step {sid} ({step.agent_id}) raised: {outcome}", exc_info=outcome)
                    step.status = "failed"
                    failed.add(sid)
                    self._set_node_status(sid, "failed")
                    await event_bus.emit(Event(
                        type=EventType.TASK_FAILED,
                        agent_id=step.agent_id,
                        task_id=f"{task_id}_{sid}",
                        data={"step_id": sid, "error": str(outcome)}
                    ))
                    # Failures have to reach memory too. memory.record lives at
                    # the end of _run_step, which a raising step never gets to,
                    # so every episode ever written said "success" -- leaving
                    # success_rate at 1.0 for everyone and making the
                    # competence-based rerouting in _route_step dead code.
                    # Cost is cost whether the step worked or not. Recorded
                    # here as well as in _run_step, which a raising step never
                    # reaches, or the mission's total would quietly omit every
                    # failure -- and a failed step is often the expensive one.
                    specialist = self._agent_by_id(step.agent_id)
                    self._record_step_telemetry(
                        task_id, step, dict(specialist.last_run or {}) if specialist else {})

                    if settings.memory_enabled:
                        memory.record(
                            agent_id=step.agent_id,
                            agent_name=specialist.name if specialist else step.agent_id,
                            goal=goal,
                            subtask=step.description,
                            outcome="failure",
                            summary=f"{type(outcome).__name__}: {outcome}"[:400],
                            duration_sec=(specialist.last_run.get("duration_sec", 0.0)
                                          if specialist else 0.0),
                            total_tokens=(specialist.last_run.get("tokens", 0)
                                          if specialist else 0),
                        )
                    continue

                step.status = "completed"
                completed.add(sid)
                self._set_node_status(sid, "completed")
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
        Run a step as a meeting: gather the attendees, let the chair work with
        the room's own tools, then send everyone back.

        The attendees are held at the table for the whole call, so the office
        shows a group sitting together rather than one agent at a desk. They do
        not each run a ReAct loop -- one conversation with everyone's artifacts
        on the table is the point, and N loops would multiply cost for no more
        information. What makes it a meeting rather than a solo read is that
        review_artifacts and record_decision exist only here, and every
        attendee's work is on the table whether or not the step declared it in
        `consumes`.
        """
        from tools import meeting as meeting_tools

        attendees = [(a, ag.name) for a in step.attendees
                     if (ag := self._agent_by_id(a)) is not None]

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

        meeting_tools.bind_workspace(workspace)
        # file_ops too, not just the meeting tools. A chair whose own manifest
        # carries write_file will use it -- and with only meeting_tools bound
        # that write escaped the workspace and landed loose in backend/, giving
        # one artifact two copies: the workspace's and an orphan on disk.
        file_ops.bind_workspace(workspace, chair.id, chair.name, step.id)
        try:
            async with office.hold_meeting(
                "conference_table", attendees, task_id=task_id
            ):
                return await chair.run(
                    subtask,
                    context=f"{brief}\n\n{room_context}" if room_context else brief,
                    hints=hints,
                    goal=goal,
                    extra_tools=["review_artifacts", "record_decision"],
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
        async with tracing.span(
            "step", f"{step.id}:{step.agent_id}",
            step_id=step.id, agent_id=step.agent_id, step_kind=step.kind
        ) as span:
            return await self._run_step_body(step, workspace, task_id, goal, span)

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
                        task_id=subtask_id,
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
        if current is not None and current.content != before_content:
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
                f"file -- the next step reads the file, not your reply."
            )

        # Citations are asked for only when they were obtainable. Whether a
        # source could be fetched at all depends on this agent's tool grants and
        # on a Tavily key being set, and a contract that demands one regardless
        # fails every offline run for a reason the agent cannot fix.
        spec = contracts.infer_spec(
            step.produces, step.description,
            citations_expected=bool(settings.tavily_api_key)
            and "web_search" in specialist.tools,
        )

        feedback = ""
        attempt = 0
        quality: Optional[float] = None
        verdict = "accept"
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
        }

    # ---------------------------------------------------------------- synthesis

    def _synthesize_results(
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

        lines = [
            "# 🎯 AgentHQ Mission Briefing",
            f"**Objective:** {user_prompt}",
            f"**Total Turnaround Time:** `{duration_sec:.2f}s` | "
            f"**Specialists Coordinated:** `{len(results)}` | "
            f"**Planned by:** `{planner_label}`\n",
            "---",
            "## 🗺️ Execution Graph\n"
        ]

        for step in plan.steps:
            deps = ", ".join(step.dependencies) if step.dependencies else "start"
            name = self.agents[step.agent_id].name if step.agent_id in self.agents else step.agent_id
            marker = {"completed": "✅", "failed": "❌", "running": "⏳"}.get(step.status, "•")
            lines.append(f"- {marker} `{step.id}` **{name}** — {step.description[:90]} "
                         f"_(after: {deps} → produces `{step.produces}`)_")

        if rerouted:
            lines.append("")
            lines.append("**Memory rerouted this mission:** " + "; ".join(
                f"`{r['step_id']}` {r['routed_from']} → {r['agent_id']}" for r in rerouted
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

        artifacts = workspace.summary()
        if artifacts:
            lines.append("---")
            lines.append("### 🗂️ Workspace Artifacts")
            for a in artifacts:
                lines.append(f"- `{a['name']}` — {a['size_chars']} chars, by {a['author_name']}")

        lines.append("")
        lines.append("---")
        lines.append("### ✅ Final Orchestrator Verdict")
        completed = sum(1 for s in plan.steps if s.status == "completed")
        failed = sum(1 for s in plan.steps if s.status == "failed")
        if failed:
            lines.append(f"{completed} of {len(plan.steps)} steps completed; **{failed} failed**. "
                         f"Review the failed branches before acting on this briefing.")
        else:
            lines.append(f"All {completed} steps completed and every artifact filed. "
                         f"System state is healthy and ready for the next command.")

        return "\n".join(lines)


# Global singleton Orchestrator instance
orchestrator = Orchestrator()
