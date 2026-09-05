"""
The meeting, as an argument rather than a monologue.

`orchestrator._run_meeting` held every attendee at the conference table for the
whole call, but only the chair ran a ReAct loop. The docstring there was honest
about why: N loops multiply cost for no more information. That is true of
*reading* -- five agents reading the same artifacts produce five copies of one
summary -- and false of *disagreeing*. An analyst and a writer looking at the
same draft object to different things, and neither objection is in the chair's
head.

So each attendee gets exactly one turn: one bounded call, no tools, no loop,
asked for the single strongest objection from their own specialism. The turns
are independent by construction, so they run concurrently, and the chair's
existing ReAct loop then runs with the room's objections in its context.

Cost discipline is the whole design. One call per attendee, capped at
`debate_max_tokens`, over a truncated view of the artifacts, at most
`debate_rounds` times, for at most `meeting_capacity - 1` people. Each turn is
its own trace span and is charged to the attendee's own token usage, so the
price of the argument shows up in the same gauge and the same budget as the
work -- a debate that cost more than the step it improved should be visible as
exactly that.

Unlike `critic.py`, this does not skip when there is no credential. The critic
abstains offline because a verdict invented by the same simulator that wrote
the artifact would look like a measurement; an objection is not a measurement,
and the office demo that everyone runs first has no key. Offline turns are
deterministic, derived from the attendee's role, and tagged `simulated: true`
on the event and in the text handed to the chair.
"""
import asyncio
import logging
from dataclasses import dataclass
from typing import Any, List, Optional

import llm
import tracing
from config import date_context, llm_configured, settings
from event_bus import event_bus
from schemas import Event, EventType

logger = logging.getLogger("AgentHQ.Debate")

# How much of what is on the table each attendee sees. An objection is formed
# from the shape and the claims of a draft, not from its final appendix, and
# every extra thousand characters is paid for once per attendee per round.
MAX_CONTEXT_CHARS = 3000

DEBATE_SYSTEM_PROMPT = """You are at the conference table in a multi-agent \
office. Colleagues have put their work on the table and the chair is about to \
write down what the room decided.

Your job in this turn is to state the single strongest objection to that work \
from the point of view of your own specialism. Not a summary, not praise, not a \
list -- one objection, the one you would actually raise out loud.

Rules:
- Be specific. Name the file, the claim or the number you are objecting to.
- Say what you would do about it, in one sentence.
- If the work is genuinely sound from where you sit, say so in one sentence and \
name the assumption it rests on that you would want checked.
- Three sentences at most. You get one turn."""


@dataclass
class Position:
    """One attendee's turn at the table."""
    agent_id: str
    agent_name: str
    role: str
    text: str
    round_index: int = 1
    simulated: bool = False
    model: str = ""
    total_tokens: int = 0
    cost_usd: float = 0.0


def enabled() -> bool:
    """Whether a meeting would run a debate round at all."""
    return bool(settings.debate_enabled and settings.debate_rounds > 0)


def _simulated_position(agent, step_description: str) -> str:
    """
    The offline turn.

    Deterministic and derived from the manifest, so the no-key demo shows the
    argument going round the table without inventing a specific objection that
    reads like a real finding.
    """
    return (
        f"[simulated - no model configured] From {agent.role.lower()}, the part "
        f"of \"{step_description[:80]}\" I would push back on is whatever the "
        f"draft assumes rather than shows. I would want that assumption named "
        f"before the room signs it off."
    )


async def _one_turn(
    agent,
    goal: str,
    step_description: str,
    table: str,
    earlier: str,
    round_index: int,
    task_id: Optional[str],
    step_id: str,
) -> Optional[Position]:
    """
    One attendee, one call, no tools.

    Never raises. An attendee whose call fails drops out of the round and the
    meeting continues -- the chair's synthesis is the deliverable, and losing
    one objection is a worse meeting, not a failed step.
    """
    user_parts = [f"The mission: {goal}" if goal else "",
                  f"What the room is deciding: {step_description}"]
    if table:
        user_parts.append(f"On the table:\n{table}")
    if earlier:
        user_parts.append(f"Already said in this meeting:\n{earlier}")
    user_parts.append("State your objection.")
    user = "\n\n".join(p for p in user_parts if p)

    system = (f"{date_context()}\n\n{agent.manifest.system_prompt}\n\n"
              f"{DEBATE_SYSTEM_PROMPT}")

    async with tracing.span(
        "debate", f"{agent.id}:r{round_index}",
        agent_id=agent.id, step_id=step_id, round=round_index
    ) as span:
        if not llm_configured():
            position = Position(
                agent_id=agent.id, agent_name=agent.name, role=agent.role,
                text=_simulated_position(agent, step_description),
                round_index=round_index, simulated=True,
            )
            span.set(simulated=True, position=position.text)
            return position

        try:
            result = await llm.acompletion(
                [{"role": "system", "content": system},
                 {"role": "user", "content": user}],
                max_tokens=settings.debate_max_tokens,
            )
        except Exception as e:
            # Deliberately not respecting llm_strict, for the reason critic.py
            # gives: strict mode exists so a misconfiguration cannot look like
            # a working run, and this call is not the run.
            logger.warning(f"[{agent.name}] debate turn failed: {type(e).__name__}: {e}")
            span.set(status_note="failed")
            return None

        text = (result.response.choices[0].message.content or "").strip()
        if not text:
            span.set(status_note="empty")
            return None

        # The objection itself goes in the span, clipped by `trace_max_attr_chars`
        # like every other attribute. Without it a replayed meeting can show
        # that people spoke but not what they said, which is the only
        # interesting thing about a meeting.
        span.set(model=result.model, total_tokens=result.total_tokens,
                 cost_usd=result.cost_usd, position=text)
        await _charge(agent, result, task_id)
        return Position(
            agent_id=agent.id, agent_name=agent.name, role=agent.role,
            text=text, round_index=round_index, model=result.model,
            total_tokens=result.total_tokens, cost_usd=result.cost_usd,
        )


async def _charge(agent, result: Any, task_id: Optional[str]):
    """
    Bill the turn to the attendee, exactly as a ReAct turn is billed.

    Without this the debate would be the one part of a mission that spends real
    tokens and reports none -- invisible to the HUD gauge, and free as far as
    the mission budget gate in `_execute_plan` is concerned.
    """
    usage = agent.token_usage
    usage.prompt_tokens += result.prompt_tokens
    usage.completion_tokens += result.completion_tokens
    usage.total_tokens = usage.prompt_tokens + usage.completion_tokens
    usage.estimated_cost_usd = round(usage.estimated_cost_usd + result.cost_usd, 6)

    payload = usage.model_dump()
    payload["estimated"] = False
    payload["model"] = result.model
    await event_bus.emit(Event(
        type=EventType.TOKEN_UPDATE, agent_id=agent.id, task_id=task_id, data=payload
    ))


def render(positions: List[Position]) -> str:
    """The room's objections, as a block for the chair's context."""
    if not positions:
        return ""
    lines = ["What the room said before you summed up:"]
    for position in positions:
        lines.append(f"- {position.agent_name} ({position.role}): {position.text}")
    lines.append(
        "Address these in what you record. Where you disagree with one, say so "
        "and say why -- do not quietly drop it."
    )
    return "\n".join(lines)


async def hold(
    attendees: List[Any],
    goal: str,
    step_description: str,
    room_context: str = "",
    task_id: Optional[str] = None,
    step_id: str = "",
) -> List[Position]:
    """
    Run the debate round(s) and return every position stated.

    `attendees` is the agent objects at the table, chair excluded. The guest
    list is capped at `meeting_capacity - 1` -- the same arithmetic
    `office.hold_meeting` uses, where the leftover seat is the chair's.
    """
    if not enabled() or not attendees:
        return []

    seats = max(0, settings.meeting_capacity - 1)
    speaking = attendees[:seats]
    if len(attendees) > seats:
        logger.info(f"Debate capped at {seats} of {len(attendees)} attendees "
                    f"(meeting_capacity={settings.meeting_capacity})")

    table = (room_context or "")[:MAX_CONTEXT_CHARS]
    positions: List[Position] = []

    for round_index in range(1, settings.debate_rounds + 1):
        earlier = render(positions) if positions else ""
        turns = await asyncio.gather(*[
            _one_turn(agent, goal, step_description, table, earlier,
                      round_index, task_id, step_id)
            for agent in speaking
        ], return_exceptions=True)

        for agent, turn in zip(speaking, turns):
            if isinstance(turn, BaseException):
                logger.warning(f"[{agent.name}] debate turn raised: "
                               f"{type(turn).__name__}: {turn}")
                continue
            if turn is None:
                continue
            positions.append(turn)
            await event_bus.emit(Event(
                type=EventType.AGENT_ARGUED,
                agent_id=turn.agent_id,
                task_id=task_id,
                data={
                    "agent_name": turn.agent_name,
                    "role": turn.role,
                    "position": turn.text,
                    "round": turn.round_index,
                    "step_id": step_id,
                    "simulated": turn.simulated,
                    "model": turn.model,
                    "total_tokens": turn.total_tokens,
                    "cost_usd": round(turn.cost_usd, 6),
                }
            ))

    return positions
