"""
Per-mission token accounting, including the calls no step owns.

The problem this exists for: a mission's tokens were counted in two places and
neither saw the whole run.

  * `BaseAgent.token_usage` counts an agent's turns, and only an agent's turns.
    It is also a lifetime counter -- nothing resets it between missions.
  * `Orchestrator._step_telemetry` counts what each step's agent reported, so
    the per-step breakdown and `mission_tokens` are built from it.

`planner.py`, `critic.py` and `synthesis.py` call `llm.acompletion` directly.
They belong to no agent and no step, so their tokens landed in neither place:
one planner call per mission, one critic call per step and one or two synthesis
calls were real spend that the cost panel, the mission manifest and the budget
gate all reported as zero. `debate.py` was half-counted -- it adds to an
agent's `token_usage` but never to that agent's `last_run`, so it appeared in
the roster's totals and not in the per-step rows.

This module is the missing third place: one ledger per mission that every call
reports into, whoever made it. The recording point is `llm.acompletion` itself,
so a new caller is counted without having to remember to opt in; the simulator
path in `base_agent` records explicitly, because it never reaches the provider.

Scope is carried in a `ContextVar`, which is what makes this work without
threading a task id through every signature. `asyncio.create_task` copies the
current context, so a step dispatched into its own task inherits the mission it
belongs to, and parallel missions cannot mix.
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Dict, Iterator, Optional

logger = logging.getLogger(__name__)

# The mission a call belongs to, and who made it. `None` means a call outside
# any mission -- a probe script, a unit test calling the planner directly --
# which is recorded nowhere rather than charged to whichever mission ran last.
_mission: ContextVar[Optional[str]] = ContextVar("token_ledger_mission", default=None)
_source: ContextVar[str] = ContextVar("token_ledger_source", default="other")

# Ledgers live for the life of the process, because the manifest, the trace and
# a late `dag.update` all read a mission's totals after it has finished. Capped
# so a long-lived server cannot grow one entry per mission forever; the oldest
# is dropped first, and a dropped mission reads as zero rather than as an error.
_MAX_MISSIONS = 64

_ledgers: Dict[str, Dict[str, Any]] = {}


def _blank() -> Dict[str, Any]:
    return {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "cost_usd": 0.0,
        "calls": 0,
        "estimated": False,
        # tokens by source, so a mission can say how much of its spend was
        # overhead rather than work. Keyed by the labels `source()` sets.
        "by_source": {},
    }


def begin(task_id: str) -> None:
    """
    Open (or reopen) a mission's ledger.

    A resumed mission starts a fresh one on purpose, matching the token
    baseline the orchestrator re-takes on resume: the budget governs the work
    still to be done, not what the process spent before it died.
    """
    if not task_id:
        return
    _ledgers[task_id] = _blank()
    while len(_ledgers) > _MAX_MISSIONS:
        _ledgers.pop(next(iter(_ledgers)))


@contextmanager
def mission(task_id: str) -> Iterator[None]:
    """Charge every call made inside this block to `task_id`."""
    token = _mission.set(task_id or None)
    try:
        yield
    finally:
        _mission.reset(token)


@contextmanager
def source(name: str) -> Iterator[None]:
    """
    Label what kind of call is being made: `planner`, `critic`, `synthesis`,
    `debate`, or `agent:<id>` for a specialist's own turn.
    """
    token = _source.set(name or "other")
    try:
        yield
    finally:
        _source.reset(token)


def current_mission() -> Optional[str]:
    return _mission.get()


def record(
    *,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    cost_usd: float = 0.0,
    model: str = "",
    estimated: bool = False,
    task_id: Optional[str] = None,
    source_name: Optional[str] = None,
) -> None:
    """
    Add one call to a mission's ledger.

    Never raises. Accounting that can break a mission is worse than accounting
    that is briefly wrong, so every failure here is logged and swallowed.
    """
    try:
        target = task_id or _mission.get()
        if not target:
            return
        led = _ledgers.get(target)
        if led is None:
            # A call arriving for a mission nobody opened. Open it rather than
            # dropping the tokens -- an unregistered mission is still spend.
            led = _ledgers[target] = _blank()
        prompt = max(0, int(prompt_tokens or 0))
        completion = max(0, int(completion_tokens or 0))
        led["prompt_tokens"] += prompt
        led["completion_tokens"] += completion
        led["cost_usd"] = round(led["cost_usd"] + float(cost_usd or 0.0), 6)
        led["calls"] += 1
        led["estimated"] = led["estimated"] or bool(estimated)
        key = source_name or _source.get()
        led["by_source"][key] = led["by_source"].get(key, 0) + prompt + completion
        if model:
            led.setdefault("models", set()).add(model)
    except Exception as exc:                                  # pragma: no cover
        logger.debug("[tokens] not recorded: %s: %s", type(exc).__name__, exc)


def totals(task_id: str) -> Dict[str, Any]:
    """
    What this mission has spent, across agents, planner, critic and reviewer.
    A mission with no ledger reads as zero, which is what an unregistered
    mission has provably spent through this path.
    """
    led = _ledgers.get(task_id) or _blank()
    total = int(led["prompt_tokens"]) + int(led["completion_tokens"])
    return {
        "prompt_tokens": int(led["prompt_tokens"]),
        "completion_tokens": int(led["completion_tokens"]),
        "total_tokens": total,
        "cost_usd": round(float(led["cost_usd"]), 6),
        "calls": int(led["calls"]),
        "estimated": bool(led["estimated"]),
        "by_source": dict(led["by_source"]),
        "models": sorted(led.get("models", set())),
    }


def total_tokens(task_id: str) -> int:
    led = _ledgers.get(task_id)
    if not led:
        return 0
    return int(led["prompt_tokens"]) + int(led["completion_tokens"])


def overhead_tokens(task_id: str) -> int:
    """
    The part of a mission that no step owns: planning, review, synthesis and
    the conference table. This is the figure that used to be invisible.
    """
    led = _ledgers.get(task_id)
    if not led:
        return 0
    return sum(n for key, n in led["by_source"].items()
               if not str(key).startswith("agent:"))


def reset() -> None:
    """Drop every ledger. For tests."""
    _ledgers.clear()
