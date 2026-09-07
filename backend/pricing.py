"""
What a call actually cost.

`base_agent` multiplied every model's tokens by two hardcoded Gemini-flash
constants -- 0.00000015 in, 0.0000006 out -- so a mission run on Claude or
GPT reported a Gemini price, and the simulator's invented token counts were
priced the same way. The number on the HUD was arithmetic, not cost.

The table below is deliberately incomplete, and that is the design. Provider
prices move, and a model this table has never heard of is reported through
`is_priced()` as UNPRICED rather than being guessed at. A guess is silently
wrong forever; an unpriced model is visible in `mission.json` and can be
fixed by adding one line here. Never invent a row to make a total look tidy.

Prices are USD per 1M tokens, (input, output), checked against the providers'
public pricing pages in February 2026. Re-check them before quoting a
scorecard figure at anyone.
"""
import logging
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("AgentHQ.Pricing")

# --------------------------------------------------------------- the table

_PRICES: Dict[str, Tuple[float, float]] = {
    # --- Google Gemini ---
    "gemini-2.5-pro": (1.25, 10.00),
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-2.5-flash-lite": (0.10, 0.40),
    "gemini-2.0-flash": (0.10, 0.40),
    "gemini-2.0-flash-lite": (0.075, 0.30),
    "gemini-1.5-pro": (1.25, 5.00),
    "gemini-1.5-flash": (0.075, 0.30),

    # --- OpenAI ---
    # gpt-5.5 is named in a config.py comment but is not listed here: its
    # published price was not verified at the time of writing, and an
    # unpriced row is the honest answer. Add it when you have checked.
    "gpt-5": (1.25, 10.00),
    "gpt-5-mini": (0.25, 2.00),
    "gpt-5-nano": (0.05, 0.40),
    "gpt-4o": (2.50, 10.00),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4.1": (2.00, 8.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1-nano": (0.10, 0.40),
    "o4-mini": (1.10, 4.40),
    "o3-mini": (1.10, 4.40),

    # --- Anthropic ---
    "claude-opus-4": (15.00, 75.00),
    "claude-opus-4-1": (15.00, 75.00),
    "claude-sonnet-4": (3.00, 15.00),
    "claude-sonnet-4-5": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-3-5-sonnet": (3.00, 15.00),
    "claude-3-5-haiku": (0.80, 4.00),
    "claude-3-haiku": (0.25, 1.25),

    # --- Free lanes -------------------------------------------------------
    # A zero here is a MEASURED zero, not the "we have never heard of this
    # model" zero that `is_priced()` exists to distinguish. Experiential Labs
    # serves these `-free` variants at no charge; the row is what stops the
    # startup warning crying wolf about a model whose cost genuinely is nothing.
    # If a provider ever starts charging for one, delete the row rather than
    # guessing a rate -- an unpriced model is visible, a wrong one is not.
    "minimax-m3-free": (0.0, 0.0),

    # --- Groq ---
    # Groq bills the open-weight models it hosts, so the key is the model
    # name rather than the provider.
    "llama-3.3-70b-versatile": (0.59, 0.79),
    "llama-3.1-8b-instant": (0.05, 0.08),
    "llama-3.1-70b-versatile": (0.59, 0.79),
    "mixtral-8x7b-32768": (0.24, 0.24),
    "gemma2-9b-it": (0.20, 0.20),
}

# Longest first, so "gemini-2.5-flash-lite" is matched before
# "gemini-2.5-flash" would swallow it and quote the dearer price.
_KEYS_BY_LENGTH = sorted(_PRICES, key=len, reverse=True)


# ----------------------------------------------------------------- lookup

def _prefix_matches(name: str, key: str) -> bool:
    """
    Whether `name` is `key` plus a version suffix, rather than merely
    starting with the same letters.

    A bare `startswith` priced `gpt-5.5` off the `gpt-5` row -- a model this
    table has never seen, quietly reported as costed. The next character has
    to be a real separator, so a dated `gpt-4o-2024-08-06` still resolves and
    a different model that happens to share a stem does not.
    """
    if not name.startswith(key):
        return False
    rest = name[len(key):]
    return rest == "" or rest[0] in "-_/@:"


def _resolve(model: str) -> Optional[str]:
    """
    The table key this model string means, or None.

    Model strings arrive in three shapes and all three have to land on the
    same row: bare (`gpt-4o`), litellm provider-prefixed
    (`gemini/gemini-2.5-flash`), and dated (`gpt-4o-2024-08-06`). The gateway
    path adds a fourth, since routing through an OpenAI-compatible base
    requires the `openai/` prefix on a model that is not OpenAI's at all --
    `openai/gemini-2.5-flash` has to price as Gemini, so the prefix is
    dropped before matching rather than trusted.
    """
    if not model:
        return None
    name = model.strip().lower()
    if name in _PRICES:
        return name

    tail = name.rsplit("/", 1)[-1]
    if tail in _PRICES:
        return tail

    for key in _KEYS_BY_LENGTH:
        if _prefix_matches(tail, key) or _prefix_matches(name, key):
            return key
    return None


def is_priced(model: str) -> bool:
    """Whether this model has a row. False means `price()` returned 0.0
    because nothing is known, not because the call was free."""
    return _resolve(model) is not None


def price(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    """
    USD for one call. 0.0 for a model the table does not know.

    Pair every call with `is_priced()` if the total is going to be shown to
    anyone -- a zero from here is indistinguishable from a free call unless
    the caller records the model name as unpriced alongside it.
    """
    key = _resolve(model)
    if key is None:
        return 0.0
    rate_in, rate_out = _PRICES[key]
    cost = ((max(prompt_tokens, 0) * rate_in) +
            (max(completion_tokens, 0) * rate_out)) / 1_000_000
    # 6dp: a single cheap turn costs a few hundred-thousandths of a dollar,
    # and rounding to 5 would floor most of them to zero.
    return round(cost, 6)


def warn_if_configured_models_are_unpriced() -> List[str]:
    """
    Say so at boot when the models this office is actually going to use have no
    row in the table.

    `is_priced()` has always been able to answer this, but nothing asked it
    until a mission was already over and the answer was buried in
    `mission.json`. The failure mode that motivated this: `default_model` moved
    to `gemini/gemini-3.5-flash` while the table still stopped at 2.5, so every
    mission, every cost panel and every eval budget check read $0.0000 -- a
    harness whose whole pitch is that you can price a run, silently reporting
    that every run was free.

    Returns the unpriced names so a caller can show them; logs a warning either
    way. Deliberately does not invent a rate to make the warning go away.
    """
    from config import settings

    configured = [settings.default_model, settings.fallback_model]
    for extra in (settings.critic_model, settings.synthesis_model,
                  settings.judge_model):
        if (extra or "").strip():
            configured.append(extra)

    unpriced = sorted({m for m in configured if m and not is_priced(m)})
    if unpriced:
        logger.warning(
            "No price for %s -- every cost this office reports will be $0.0000. "
            "Add a row to backend/pricing.py once you have checked the "
            "provider's published rate.", ", ".join(unpriced))
    return unpriced
