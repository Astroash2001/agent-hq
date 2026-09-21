"""
Limits an agent obeys from inside its own step.

The mission token budget used to be checked only between dispatches. A step
already running never looked, so a recorded mission reported "Token budget
exhausted: 756,281 of 400,000" and then kept an extraction agent working for
seven more minutes, ending at 1,114,035 tokens. The gate stopped the next steps
and not the spend.

The orchestrator binds a budget reader for the length of a step. It is a
ContextVar, like the file binding, so parallel steps each see their own mission
and a bare agent (a test, the planner) sees no budget at all.
"""
import contextvars
import time
from typing import Callable, Optional, Tuple

_budget: contextvars.ContextVar[Optional[Callable[[], Tuple[int, int]]]] = \
    contextvars.ContextVar("mission_budget", default=None)

_deadline: contextvars.ContextVar[Optional[float]] = \
    contextvars.ContextVar("step_deadline", default=None)


def bind_budget(reader: Optional[Callable[[], Tuple[int, int]]]):
    """Set the `(spent, limit)` reader for this task. Returns a reset token."""
    return _budget.set(reader)


def reset_budget(token) -> None:
    _budget.reset(token)


def bind_deadline(monotonic_deadline: Optional[float]):
    """
    Set when the step running in this context must be finished, on the
    `time.monotonic()` scale. Returns a reset token.

    Time is the other limit a step can blow through from the inside.
    `asyncio.wait_for` stops a step at `step_timeout_sec`, but nothing inside
    it knew that deadline existed -- so the retry loop in `llm.py` would sleep
    60s three times over with 90s left on the clock, and a repair attempt would
    be started at 780s of a 900s budget and killed before it could finish,
    discarding a perfectly good first draft with it.
    """
    return _deadline.set(monotonic_deadline)


def reset_deadline(token) -> None:
    _deadline.reset(token)


def seconds_left() -> Optional[float]:
    """
    How long the running step has left, or None when nothing is bound -- a unit
    test, the planner, an agent called outside a step. None means "no deadline
    known", never "no time left", so an unbound caller behaves as it always did.
    """
    deadline = _deadline.get()
    if deadline is None:
        return None
    return deadline - time.monotonic()


def ceiling_for_step(budget: int, steps_waiting: int, per_step: int,
                     max_fraction: float) -> int:
    """
    What a running step may spend when `steps_waiting` steps come after it.

    A mission budget spent first-come leaves nothing for the step that writes
    the deliverable: two research steps once spent 413,788 of 400,000 and the
    step that was to write the itinerary was failed by the gate without ever
    calling its model. Research and no deliverable is worse than less research.

    So a step's ceiling is the budget minus a reserve for each step still
    waiting -- it reaches its closing turn sooner and the rest stay affordable.
    The reserve is capped as a fraction of the budget, because a wide plan would
    otherwise reserve the whole thing and leave the first step unable to start.
    """
    if budget <= 0 or steps_waiting <= 0 or per_step <= 0:
        return budget
    reserve = min(per_step * steps_waiting, int(budget * max(0.0, max_fraction)))
    return max(1, budget - reserve)


def budget_exhausted() -> Optional[str]:
    """A short description when the bound mission is over budget, else None."""
    reader = _budget.get()
    if reader is None:
        return None
    try:
        spent, limit = reader()
    except Exception:
        return None
    if limit and limit > 0 and spent >= limit:
        return f"{spent:,} of {limit:,} tokens used"
    return None
