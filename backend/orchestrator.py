import asyncio
import glob
import json
import logging
import os
import time
import uuid
from typing import Any, Dict, List, Optional, Set

from base_agent import BaseAgent
from config import settings
from event_bus import event_bus
from memory import memory
from office import office
from planner import planner
from schemas import (AgentManifest, AgentStatus, DAGNode, Event, EventType, Plan,
                     PlanStep, Task)
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
        """
        if not self.agents:
            self._load_agents()

        task_id = f"task_{uuid.uuid4().hex[:8]}"
        main_task = Task(id=task_id, description=user_prompt, status="in_progress")
        self.active_tasks[task_id] = main_task
        workspace = MissionWorkspace(task_id)
        self.workspaces[task_id] = workspace

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

        await event_bus.emit(Event(
            type=EventType.TASK_COMPLETED,
            task_id=task_id,
            data={
                "result": synthesis_text,
                "duration_seconds": round(duration, 2),
                "subtasks_count": len(plan.steps),
                "plan_source": plan.source,
                "artifacts": workspace.summary()
            }
        ))

        if self.boss_agent:
            await self.boss_agent.update_status(AgentStatus.IDLE)
            await self.boss_agent.speak("Mission complete! Delivered briefing.", task_id=task_id)

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
            "duration_seconds": round(duration, 2)
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

    async def _run_step(
        self,
        step: PlanStep,
        workspace: MissionWorkspace,
        task_id: str,
        goal: str
    ) -> Dict[str, Any]:
        """Assign one step, wait for the specialist, and file what it produced."""
        specialist = self.agents.get(step.agent_id)
        if not specialist:
            raise KeyError(f"step {step.id} names unknown agent '{step.agent_id}'")

        subtask_id = f"{task_id}_{step.id}"
        subtask = Task(
            id=subtask_id,
            description=step.description,
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
                "routed_from": step.routed_from
            }
        ))
        if self.boss_agent:
            await self.boss_agent.speak(f"Dispatched {step.id} to {specialist.name}", task_id=task_id)

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

            result_text = await specialist.run(subtask, context=context, hints=hints, goal=goal)

        stats = dict(specialist.last_run or {})

        if step.produces:
            await workspace.put(
                name=step.produces,
                content=result_text,
                author_id=step.agent_id,
                author_name=specialist.name,
                step_id=step.id
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
                artifact=step.produces
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
            "tools_used": stats.get("tools_used", [])
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
