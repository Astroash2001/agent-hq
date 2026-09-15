import asyncio
import json
import logging
import time
from typing import Any, Dict, List, Optional

import llm
import step_guard
import tracing
from config import date_context, settings, llm_configured
from event_bus import event_bus
from memory import memory
from office import office
from schemas import AgentManifest, AgentStatus, Event, EventType, Task, TokenUsage
from tool_registry import tool_registry

logger = logging.getLogger("AgentHQ.BaseAgent")


def _call_id(step: int, call_index: int) -> str:
    """
    A tool call's id, unique within the conversation.

    It used to be `call_{step}`, which collides the moment a model returns two
    tool calls in one turn: both assistant messages claim the same id and both
    results answer it. Gemini never noticed; a stricter provider matches results
    to calls by this id.
    """
    return f"call_{step}_{call_index}"


# Arguments whose value is a body rather than a choice. The trace keeps their
# length, not their text: a write_file span carrying the whole report would make
# the trace larger than the work it describes.
_BODY_ARGS = {"content", "decision", "code_snippet"}

# Tools called for their side effect. Repeating one is a new write, not a repeat
# of an observation, so the duplicate-call guard leaves them alone.
_SIDE_EFFECT_TOOLS = {"write_file", "record_decision"}
_TRACE_ARG_CHARS = 300


def _trace_args(tool_args: Any) -> Dict[str, Any]:
    """
    A tool call's arguments as the trace records them.

    Recorded at all because a span that says only `success: false` cannot be
    debugged: observed, an extraction step made 35 failing `query_data` calls and
    the trace could not say which file it had asked for or what came back.
    """
    if not isinstance(tool_args, dict):
        return {}
    out: Dict[str, Any] = {}
    for key, value in tool_args.items():
        if key in _BODY_ARGS and isinstance(value, str):
            out[key] = f"<{len(value)} chars>"
        elif isinstance(value, str):
            out[key] = value[:_TRACE_ARG_CHARS] + ("..." if len(value) > _TRACE_ARG_CHARS else "")
        elif isinstance(value, (int, float, bool)) or value is None:
            out[key] = value
        else:
            text = json.dumps(value, default=str)
            out[key] = text[:_TRACE_ARG_CHARS] + ("..." if len(text) > _TRACE_ARG_CHARS else "")
    return out


def _append_tool_exchange(messages, call_id, tool_name, tool_args, result) -> None:
    """
    Append the assistant/tool pair that records one tool call and its result.

    The OpenAI wire format requires a `tool` message to be preceded by an
    assistant message carrying the matching `tool_calls` entry -- send one
    without and the whole request is rejected, not just that message. Both the
    real-result path and the search-cap refusal go through here so neither can
    drift into sending a bare `tool` message again.
    """
    messages.append({
        "role": "assistant",
        "content": None,
        "tool_calls": [{
            "id": call_id,
            "type": "function",
            "function": {"name": tool_name, "arguments": json.dumps(tool_args)},
        }],
    })
    messages.append({
        "role": "tool",
        "tool_call_id": call_id,
        "name": tool_name,
        "content": json.dumps(result),
    })


class BaseAgent:
    """
    Standard ReAct Agent Engine (LLM + Tools + Execution Loop).
    Every specialist agent in the harness inherits from or is instantiated through this engine.
    """

    def __init__(self, manifest: AgentManifest):
        self.manifest = manifest
        self.id = manifest.id
        self.name = manifest.name
        self.role = manifest.role
        self.tools = manifest.tools
        self.status = AgentStatus.IDLE
        self.token_usage = TokenUsage()
        self._is_cancelled = False
        # The LLM request currently in flight, so cancel() can abort it rather
        # than waiting for it to time out.
        self._inflight: Optional[asyncio.Future] = None
        # Telemetry for the step that just finished, read by the orchestrator
        # when it writes the episode to memory.
        self.last_run: Dict[str, Any] = {}

    async def update_status(self, status: AgentStatus, details: Optional[Dict[str, Any]] = None):
        """Update agent state and broadcast to EventBus for live 2D canvas animation."""
        self.status = status
        data = {"status": status.value, "agent_name": self.name, "role": self.role}
        if details:
            data.update(details)

        await event_bus.emit(Event(
            type=EventType.AGENT_STATUS,
            agent_id=self.id,
            data=data
        ))

    async def speak(self, message: str, task_id: Optional[str] = None):
        """Display an animated speech bubble above this agent in the 2D canvas."""
        await event_bus.emit(Event(
            type=EventType.AGENT_SPEAKING,
            agent_id=self.id,
            task_id=task_id,
            data={"speech": message, "agent_name": self.name}
        ))

    def cancel(self):
        """
        Emergency interrupt / kill switch trigger.

        Cancels the in-flight request as well as setting the flag. The flag
        alone is only read between ReAct steps, so a HALT pressed during a call
        used to sit for the rest of the 90s timeout before anything happened.
        """
        self._is_cancelled = True
        inflight = self._inflight
        if inflight is not None and not inflight.done():
            inflight.cancel()

    async def run(
        self,
        task: Task,
        context: str = "",
        hints: str = "",
        goal: str = "",
        extra_tools: Optional[List[str]] = None,
        produces: Optional[str] = None
    ) -> str:
        """
        Execute the ReAct Loop for the assigned task.
        Reason -> Act (Tool Call) -> Observe -> Repeat -> Final Answer.

        `context` is the text of the artifacts colleagues have already delivered
        to this desk, `hints` what this agent remembers about similar work, and
        `goal` the mission the step belongs to. All three are optional, so a bare
        `run(task)` behaves exactly as it did before the workspace existed.

        `extra_tools` grants tools for this call only, without touching the
        agent's manifest. It exists for meetings: review_artifacts and
        record_decision belong to the conference table, not to any one
        specialist, so the chair borrows them for the length of the meeting and
        gives them back.

        Tool calls are routed through the office: the agent walks to the station
        the tool lives at and holds it for the duration of the call.
        """
        self._is_cancelled = False
        start_time = time.time()
        tokens_at_start = self.token_usage.total_tokens
        tools_used: List[str] = []

        # This run's own telemetry, kept separately from the agent's lifetime
        # totals. `token_usage` accumulates across every mission the process
        # has served, so it cannot answer "what did this step cost".
        run = {
            "prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0,
            "retries": 0, "fell_back": False, "estimated": False, "model": "",
        }
        logger.info(f"[{self.name}] Received task: {task.description}")

        # 1. Announce starting work
        await self.update_status(AgentStatus.THINKING)
        await self.speak(f"Analyzing task: '{task.description[:40]}...' ", task_id=task.id)

        # 2. Prepare message history & schemas
        #
        # The date leads the system prompt. Without it a model reasons from its
        # training cutoff and cannot tell current information from stale --
        # observed: a news brief headed "Today" citing an article three weeks
        # old, because nothing in the conversation said what today was.
        system_prompt = (
            f"{date_context()}\n\n"
            f"{self.manifest.system_prompt}\n\n"
            "DATA INTEGRITY & EXTRACTION DIRECTIVE:\n"
            "- When extracting or transforming data from upstream files, web pages, or APIs, extract real, complete information.\n"
            "- Never populate requested attributes (such as addresses, numbers, descriptions, names, or contact info) with 'null', 'None', or 'N/A' placeholders if the upstream source contains the data.\n"
            "- If an upstream artifact snippet indicates it was partially condensed or truncated, use 'read_file' to inspect the complete file on disk before writing your deliverable."
        )
        if hints:
            system_prompt = f"{system_prompt}\n\n{hints}"

        user_parts = [f"Task: {task.description}"]
        if goal:
            # Context, not a second assignment. Handed the whole goal with no
            # boundary, a guardrail agent also ran the extraction and wrote the
            # extraction step's file -- work, tokens and a file that were not
            # its to produce.
            user_parts.append(
                f"For context only, this task is one step of a larger mission: {goal}\n"
                f"Other steps of that mission are assigned to other specialists. "
                f"Do only the Task above, and write only your own deliverable.")
        if context:
            user_parts.append(context)

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": "\n\n".join(user_parts)}
        ]
        granted = list(self.tools)
        for extra in (extra_tools or []):
            if extra not in granted:
                granted.append(extra)
        tool_schemas = tool_registry.get_schemas_for_agent(granted)

        step = 0
        final_answer = ""
        last_content = ""      # best non-empty reply seen, for the fallback below
        searches = 0           # web_search calls this run, capped below
        seen_calls: Dict[str, int] = {}   # identical observing calls, see below
        budget_last_turn = False          # the one closing turn after the budget runs out

        while step < settings.max_agent_steps:
            if self._is_cancelled:
                await self.update_status(AgentStatus.ERROR, {"error": "Cancelled by user emergency stop."})
                await self.speak("Emergency stop received. Halting!", task_id=task.id)
                self.last_run = {
                    "duration_sec": round(time.time() - start_time, 2),
                    "tokens": self.token_usage.total_tokens - tokens_at_start,
                    "tools_used": tools_used,
                    "steps": step,
                    "outcome": "failure",
                    **run,
                }
                return "Task halted by emergency stop."

            # The mission's token budget, checked every turn rather than only
            # between steps. Out of budget: one last turn to file what exists,
            # with write_file as the only tool, then stop. Observed without
            # this: the gate fired at 756K of 400K and a running step carried
            # on to 1.1M.
            over_budget = step_guard.budget_exhausted()
            turn_schemas = tool_schemas
            if over_budget:
                if budget_last_turn:
                    logger.warning("[%s] stopped: mission token budget exhausted (%s)",
                                   self.name, over_budget)
                    final_answer = last_content or (
                        f"Stopped: the mission token budget is exhausted ({over_budget}).")
                    break
                budget_last_turn = True
                messages.append({
                    "role": "user",
                    "content": (
                        f"The mission's token budget is used up ({over_budget}). Stop all "
                        f"further investigation. This is your final turn. "
                        + (f"Write your deliverable to `{produces}` with write_file now, "
                           f"using only what you already have, and say plainly what "
                           f"you did not get to. " if produces and "write_file" in granted
                           else "Reply now with what you have, and say plainly what you "
                                "did not get to. ")
                    ),
                })
                turn_schemas = [s for s in tool_schemas
                                if s.get("function", {}).get("name") == "write_file"]

            step += 1
            await self.update_status(AgentStatus.THINKING, {"step": step})

            # Call LLM or fallback simulator
            try:
                async with tracing.span(
                    "turn", f"{self.id} turn {step}", agent_id=self.id, step=step
                ) as turn:
                    response_data = await self._call_llm(messages, turn_schemas, task)
                    turn.set(
                        model=response_data.get("model", ""),
                        prompt_tokens=response_data.get("prompt_tokens", 0),
                        completion_tokens=response_data.get("completion_tokens", 0),
                        cost_usd=response_data.get("cost_usd", 0.0),
                        estimated=bool(response_data.get("estimated")),
                    )
            except asyncio.CancelledError:
                # cancel() aborted the request mid-flight. Loop round so the
                # check at the top produces the halt result, rather than having
                # a second copy of that bookkeeping here.
                if self._is_cancelled:
                    continue
                raise

            # Update token usage telemetry.
            #
            # The counts are whatever the provider reported, and 0 when it
            # reported nothing -- the old defaults of 120/80 meant a response
            # with no usage block was indistinguishable from a real one. The
            # simulator still estimates, because it has to, but says so.
            prompt_tokens = int(response_data.get("prompt_tokens", 0) or 0)
            completion_tokens = int(response_data.get("completion_tokens", 0) or 0)
            turn_cost = float(response_data.get("cost_usd", 0.0) or 0.0)
            estimated = bool(response_data.get("estimated"))

            self.token_usage.prompt_tokens += prompt_tokens
            self.token_usage.completion_tokens += completion_tokens
            self.token_usage.total_tokens = (self.token_usage.prompt_tokens
                                             + self.token_usage.completion_tokens)
            # Priced by the model that actually answered. This was two hardcoded
            # Gemini-flash constants applied to the running total, so a mission
            # on Claude or GPT reported a Gemini bill -- and re-pricing the
            # lifetime total every turn meant a roster that changed models
            # mid-run silently re-costed its own history.
            self.token_usage.estimated_cost_usd = round(
                self.token_usage.estimated_cost_usd + turn_cost, 6)

            run["prompt_tokens"] += prompt_tokens
            run["completion_tokens"] += completion_tokens
            run["cost_usd"] = round(run["cost_usd"] + turn_cost, 6)
            run["retries"] += max(int(response_data.get("attempts", 1) or 1) - 1, 0)
            run["fell_back"] = run["fell_back"] or bool(response_data.get("fell_back"))
            run["estimated"] = run["estimated"] or estimated
            if response_data.get("model"):
                run["model"] = response_data["model"]

            # Broadcast Token Update
            payload = self.token_usage.model_dump()
            # The gauge showed invented numbers on the no-key path and had no
            # way to say so. `estimated` is what lets the HUD mark the figure
            # as unmeasured instead of quietly presenting a guess as a reading.
            payload["estimated"] = estimated
            payload["model"] = run["model"]
            await event_bus.emit(Event(
                type=EventType.TOKEN_UPDATE,
                agent_id=self.id,
                task_id=task.id,
                data=payload
            ))

            content_now = (response_data.get("content") or "").strip()
            if len(content_now) > len(last_content):
                last_content = content_now

            thought_text = response_data.get("thought", "")
            if thought_text:
                await self.speak(thought_text[:70], task_id=task.id)

            if response_data.get("truncated"):
                logger.warning("[%s] turn truncated by token ceiling during reasoning", self.name)
                messages.append({
                    "role": "user",
                    "content": (
                        "Your previous turn exceeded the token limit during internal reasoning before producing "
                        "any text or tool calls. Do NOT use lengthy internal reasoning. Produce your deliverable "
                        "directly or call write_file immediately."
                    )
                })
                continue

            tool_calls = response_data.get("tool_calls", [])

            # Warn the agent before its budget runs out.
            #
            # Nothing ever told an agent how many steps it had, so it could not
            # ration them. With live search wired up, a researcher spent all 8
            # on thirteen increasingly specific queries and was cut off before
            # writing anything -- the work was done, and none of it was kept.
            # A model that knows it is nearly out of turns stops gathering and
            # produces the deliverable.
            remaining = settings.max_agent_steps - step
            if 0 < remaining <= 2 and tool_calls:
                messages.append({
                    "role": "user",
                    "content": (
                        f"You have {remaining} step{'' if remaining == 1 else 's'} left "
                        f"before this task is stopped. Stop gathering information now "
                        f"and produce your deliverable this turn -- write it out in full "
                        f"with write_file if you have that tool, then reply with a "
                        f"one-line summary. Work with what you already have."
                    )
                })

            # If no tool calls requested, we have reached the final answer
            if not tool_calls:
                final_answer = response_data.get("content", "Task completed successfully.")
                break

            # Execute tool calls, one station at a time
            for call_index, tool_call in enumerate(tool_calls):
                tool_name = tool_call.get("name")
                tool_args = tool_call.get("arguments", {})
                tools_used.append(tool_name)

                # Out of budget: nothing but filing the deliverable runs.
                if budget_last_turn and tool_name != "write_file":
                    _append_tool_exchange(
                        messages, _call_id(step, call_index), tool_name, tool_args,
                        {"success": False, "refused": True,
                         "error": "The mission token budget is exhausted. Only write_file is available."})
                    continue

                # The same observing call, with the same arguments, again. Its
                # result is already in this conversation and will not change, and
                # every repeat re-sends the whole history. Observed: an agent ran
                # an identical 30,000-row query ten times hoping the 200-row cap
                # would lift, on a mission that went on to spend 1.1M tokens.
                if tool_name not in _SIDE_EFFECT_TOOLS:
                    try:
                        call_key = f"{tool_name}:{json.dumps(tool_args, sort_keys=True, default=str)}"
                    except (TypeError, ValueError):
                        call_key = f"{tool_name}:{tool_args!r}"
                    seen_calls[call_key] = seen_calls.get(call_key, 0) + 1
                    if seen_calls[call_key] > 1:
                        _append_tool_exchange(
                            messages, _call_id(step, call_index), tool_name, tool_args,
                            {"success": False, "refused": True,
                             "error": (
                                 f"You already made this exact {tool_name} call in this task, "
                                 f"and its result is earlier in this conversation. Repeating "
                                 f"it returns the same result. Use that result, change the "
                                 f"arguments, or write your deliverable.")})
                        logger.info("[%s] refused repeat of an identical %s call",
                                    self.name, tool_name)
                        continue

                # Late-turn tool gating: force write_file on final turns when a deliverable is promised
                if remaining <= 1 and produces and "write_file" in granted and tool_name in (
                    "web_search", "fetch_url", "read_file", "search_workspace", "calculate"
                ):
                    _append_tool_exchange(
                        messages, _call_id(step, call_index), tool_name,
                        tool_args,
                        {
                            "success": False,
                            "error": (
                                f"Turn limit reached. Further exploration ({tool_name}) is disabled. "
                                f"Write and file your deliverable to '{produces}' now using write_file."
                            ),
                        })
                    logger.info("[%s] exploratory tool '%s' blocked on final turn; write_file required", self.name, tool_name)
                    continue

                # Hard ceiling on searches per task.
                #
                # Search is the one tool an agent will happily call forever:
                # every result suggests another query worth trying. Observed on
                # one subtask -- eighteen searches, each re-sent in the history
                # on every later turn, and a single 3-step mission costing 144k
                # tokens. The cap is enforced here rather than asked for in the
                # prompt because a request is advisory and this is a budget.
                if tool_name == "web_search":
                    searches += 1
                    if searches > settings.search_max_calls:
                        # Through the same helper as a real result, and that is
                        # not tidiness. This used to append a bare `tool`
                        # message with no assistant `tool_calls` before it and
                        # no `tool_call_id`. Gemini accepted it; OpenAI rejects
                        # the whole request with "messages with role 'tool' must
                        # be a response to a preceeding message with
                        # 'tool_calls'", which kills the step -- so the cheapest
                        # guard against one path drifting from the other is for
                        # there to be only one path.
                        _append_tool_exchange(
                            messages, _call_id(step, call_index), tool_name,
                            tool_args,
                            {
                                "success": False,
                                "error": (
                                    f"Search limit reached ({settings.search_max_calls} "
                                    f"searches per task). No further searches are "
                                    f"available. Write your deliverable now using what "
                                    f"you have already found."
                                ),
                            })
                        logger.info("[%s] search cap hit (%d)", self.name, settings.search_max_calls)
                        continue

                # The span wraps the walk as well as the call: an agent queued
                # behind a busy station is waiting, and a trace that started
                # timing only at tool_registry.execute would report the wait as
                # zero and the office as faster than it is.
                async with tracing.span(
                    "tool", tool_name, agent_id=self.id
                ) as tool_span, office.use_tool_station(
                    self.id, tool_name, agent_name=self.name, task_id=task.id
                ) as station:
                    await self.update_status(AgentStatus.TOOL_CALLING, {
                        "tools": [tool_name],
                        "station_id": station.id if station else None,
                        "station_label": station.label if station else None
                    })
                    if station:
                        await self.speak(f"At the {station.label}: {tool_name}", task_id=task.id)
                    else:
                        await self.speak(f"Running tool: {tool_name}", task_id=task.id)

                    tool_result = await tool_registry.execute(
                        tool_name=tool_name,
                        arguments=tool_args,
                        agent_id=self.id,
                        task_id=task.id,
                        # The same list the schemas were built from, so what
                        # the model was offered and what it may run cannot
                        # drift apart. This is the only path by which a name a
                        # model chose reaches a tool function.
                        granted=granted,
                    )
                    succeeded = bool(isinstance(tool_result, dict)
                                     and tool_result.get("success", True))
                    tool_span.set(
                        station_id=station.id if station else None,
                        success=succeeded,
                        args=_trace_args(tool_args),
                    )
                    if not succeeded and isinstance(tool_result, dict):
                        tool_span.set(
                            refused=bool(tool_result.get("refused")),
                            tool_error=str(tool_result.get("error") or "")[:_TRACE_ARG_CHARS],
                        )

                # Feed observation back to context
                _append_tool_exchange(messages, _call_id(step, call_index),
                                      tool_name, tool_args, tool_result)

            await asyncio.sleep(0.3)  # Visual pacing for UI observation

        # Step limit reached safety check.
        #
        # `last_content` rather than the generic string wherever possible: a
        # model that says everything through tool calls and returns empty
        # content on its final turn is normal, and filing "Task completed
        # after N execution steps." as that step's artifact threw the real
        # work away.
        if not final_answer:
            final_answer = last_content or f"Task completed after {step} execution steps."

        # Back to the desk, so the next mission starts from a clean floor.
        if settings.spatial_enabled:
            await office.return_to_desk(self.id, agent_name=self.name, task_id=task.id)

        duration_sec = time.time() - start_time
        # `run` carries this call's own tokens, cost, model and retry count.
        # The orchestrator reads last_run to write the episode and the mission
        # manifest, so putting the telemetry here is what makes a per-step cost
        # breakdown possible without a second ledger to keep in step.
        self.last_run = {
            "duration_sec": round(duration_sec, 2),
            "tokens": self.token_usage.total_tokens - tokens_at_start,
            "tools_used": tools_used,
            "steps": step,
            "outcome": "success",
            **run,
        }
        await self.update_status(AgentStatus.DONE, {"duration_sec": round(duration_sec, 2)})
        await self.speak("Work completed! Delivering output.", task_id=task.id)

        return final_answer

    async def _call_llm(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        task: Task
    ) -> Dict[str, Any]:
        """
        Call LLM using litellm with graceful fallback simulator.
        """
        # Try LiteLLM if API keys are available
        has_key = llm_configured()

        if has_key:
            try:
                async def _narrate(note, kind):
                    await self.speak(note, task_id=task.id)

                # Held on the agent so cancel() can reach into the request that
                # is actually in flight. Without this, HALT only took effect
                # between ReAct steps -- up to llm_timeout (90s) of nothing
                # happening after the user pressed the button.
                self._inflight = asyncio.ensure_future(
                    llm.acompletion(
                        messages,
                        tools=tools,
                        on_retry=_narrate,
                    )
                )
                try:
                    result = await self._inflight
                finally:
                    self._inflight = None

                # `.response` is the untouched provider object; the rest of the
                # LLMResult is what the call cost and which model answered it.
                choice = result.response.choices[0]
                message = choice.message

                tool_calls_extracted = []
                if hasattr(message, "tool_calls") and message.tool_calls:
                    for tc in message.tool_calls:
                        fn_args = tc.function.arguments
                        if isinstance(fn_args, str):
                            try:
                                fn_args = json.loads(fn_args)
                            except:
                                fn_args = {}
                        tool_calls_extracted.append({
                            "name": tc.function.name,
                            "arguments": fn_args
                        })

                content = message.content or ""

                # A truncated turn is otherwise invisible: the model returns no
                # content and no tool call, the loop treats that as "done", and
                # the step files a stub. Say so instead.
                truncated = bool(choice.finish_reason == "length" and not content and not tool_calls_extracted)
                if truncated:
                    logger.warning(
                        "[%s] reply hit the %s-token ceiling before producing text "
                        "(reasoning consumed the budget); raise MAX_TOKENS",
                        self.name, settings.max_tokens)
                    await self.speak("Reply hit the token ceiling - retrying.",
                                     task_id=task.id)

                return {
                    "truncated": truncated,
                    "content": content,
                    "thought": content[:60] if content else ("Token ceiling reached" if truncated else "Executing tool..."),
                    "tool_calls": tool_calls_extracted,
                    "prompt_tokens": result.prompt_tokens,
                    "completion_tokens": result.completion_tokens,
                    "cost_usd": result.cost_usd,
                    "model": result.model,
                    "attempts": result.attempts,
                    "fell_back": result.fell_back,
                    "estimated": False,
                }
            except Exception as e:
                # A credential or routing failure must not masquerade as a
                # working run. With llm_strict on (the default in development)
                # it raises here instead of quietly producing canned output
                # that looks like the model answered.
                if settings.llm_strict:
                    logger.error(f"[{self.name}] LLM call failed: {type(e).__name__}: {e}")
                    raise
                logger.warning(f"[{self.name}] LLM call error: {e}. Utilizing specialist simulator.")

        # Robust Autonomous Specialist Simulator (100% reliable fallback)
        await asyncio.sleep(0.4)
        granted = [
            t.get("function", {}).get("name")
            for t in (tools or []) if isinstance(t, dict)
        ]
        simulated = self._simulate_specialist_action(
            task, messages, [g for g in granted if g])
        # Nothing was called, so these counts are invented and the cost is not
        # zero-because-free but zero-because-unknown. Tagged here rather than in
        # each branch of the simulator, so every path is flagged including any
        # added later, and the HUD can mark the gauge as unmeasured.
        simulated["estimated"] = True
        simulated["cost_usd"] = 0.0
        return simulated

    def _simulate_specialist_action(
        self,
        task: Task,
        messages: List[Dict[str, Any]],
        granted: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        """High-fidelity fallback simulator that generates realistic tool calls and outputs."""
        history_len = len(messages)
        desc = task.description.lower()
        granted = granted or []

        # A meeting overrides the usual keyword routing. Without this the chair
        # ignores the room it is sitting in and wanders off to whichever station
        # its description happens to match -- observed: the boss walked to the
        # library and called read_file in the middle of its own meeting. The
        # meeting tools only exist when the orchestrator granted them, so their
        # presence is a reliable signal that this step IS a meeting.
        if "review_artifacts" in granted:
            if history_len <= 2:
                return {
                    "thought": "Reading what everyone brought to the table...",
                    "tool_calls": [{"name": "review_artifacts", "arguments": {}}],
                    "prompt_tokens": 130,
                    "completion_tokens": 50
                }
            if history_len <= 4 and "record_decision" in granted:
                return {
                    "thought": "Writing up what the room agreed...",
                    "tool_calls": [{
                        "name": "record_decision",
                        "arguments": {
                            "decision": (
                                f"Meeting on: {task.description}\n\n"
                                "The room reviewed the artifacts on the table and agreed to "
                                "proceed on the approach they describe."
                            )
                        }
                    }],
                    "prompt_tokens": 150,
                    "completion_tokens": 70
                }

        # Step 1: Call relevant tool
        if history_len <= 2:
            if "coder" in self.id or "jim" in self.id or "bug" in desc or "code" in desc:
                return {
                    "thought": "Reading the code in the workspace...",
                    "tool_calls": [{
                        "name": "search_workspace",
                        "arguments": {"pattern": "def "}
                    }],
                    "prompt_tokens": 140,
                    "completion_tokens": 60
                }
            elif "research" in self.id or "scout" in self.id or "search" in desc or "rag" in desc or "job" in desc:
                return {
                    "thought": f"Querying search index for technical specs...",
                    "tool_calls": [{
                        "name": "web_search",
                        "arguments": {"query": task.description[:60], "max_results": 2}
                    }],
                    "prompt_tokens": 120,
                    "completion_tokens": 55
                }
            elif "data" in self.id or "analyst" in self.id or "sql" in desc:
                return {
                    "thought": "Inspecting workspace files and data sources...",
                    "tool_calls": [{
                        "name": "list_files",
                        "arguments": {"directory": "."}
                    }],
                    "prompt_tokens": 110,
                    "completion_tokens": 45
                }
            else:
                return {
                    "thought": "Processing request parameters...",
                    "tool_calls": [{
                        "name": "read_file",
                        "arguments": {"filepath": "schemas.py"}
                    }],
                    "prompt_tokens": 90,
                    "completion_tokens": 40
                }

        # Step 2: Final synthesis
        return {
            "thought": "Formulating final structured output...",
            "content": f"### Output from {self.name} ({self.role})\n\n"
                       f"✅ Successfully processed task: **{task.description}**\n\n"
                       f"- **Status**: Verified and validated\n"
                       f"- **Findings**: Key criteria matched. Tools executed with 0 error codes.\n"
                       f"- **Recommendation**: Proceed with pipeline handoff.",
            "tool_calls": [],
            "prompt_tokens": 180,
            "completion_tokens": 95
        }
