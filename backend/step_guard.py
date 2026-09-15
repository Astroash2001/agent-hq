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
from typing import Callable, Optional, Tuple

_budget: contextvars.ContextVar[Optional[Callable[[], Tuple[int, int]]]] = \
    contextvars.ContextVar("mission_budget", default=None)


def bind_budget(reader: Optional[Callable[[], Tuple[int, int]]]):
    """Set the `(spent, limit)` reader for this task. Returns a reset token."""
    return _budget.set(reader)


def reset_budget(token) -> None:
    _budget.reset(token)


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
