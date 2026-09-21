"""
The one place a model gets called.

There were two independent call sites -- `base_agent._call_llm` and
`planner._call_planner_llm` -- neither of which retried anything. A single 429
from the provider killed the whole mission: observed on a free-tier Gemini key,
which answered `retryDelay: 28s` and got `0 of 2 steps completed; 2 failed`.

Retrying is only safe if you retry the right things. A 429 or a 503 is the
provider saying "later"; a 401 or a 404 is the provider saying "no", and
hammering it changes nothing while hiding the real problem behind a delay. So
the split below is deliberate and the default is NOT to retry: an unrecognised
error propagates on the first attempt, which is the same contract `llm_strict`
already promises everywhere else.

`settings.fallback_model` was declared in config.py and read by nothing. This
module is what finally makes it real -- and because litellm resolves the
provider from the model prefix, the fallback can be a different provider
entirely (OpenAI primary, Gemini fallback is the shape already in .env).
"""
import asyncio
import logging
import random
import re
from typing import Any, Dict, List, NamedTuple, Optional, Tuple

import pricing
import step_guard
import token_ledger
import tracing
from config import settings, llm_route

logger = logging.getLogger(__name__)


class LLMResult(NamedTuple):
    """
    What one call cost, alongside what it returned.

    This used to return the provider's response and nothing else, so
    everything the retry loop knew was thrown away at the door: which model in
    the fallback chain actually answered, how many attempts it took, and what
    the tokens were worth. The caller then guessed -- `base_agent` defaulted to
    100/50 tokens when the response had no usage, and priced every model in the
    roster at Gemini flash rates.

    `response` is the untouched provider object, so a caller that only wants
    `.choices[0]` reads it exactly as it did before.
    """
    response: Any
    model: str                  # the model that answered, not the one asked for
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    attempts: int               # 1 when it answered first time
    fell_back: bool             # True when the primary was exhausted

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

# Retry these: the provider is busy, throttling, or briefly broken.
_TRANSIENT_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}

# Transient, but not worth a second attempt on the SAME model: the gateway in
# front of the model is refusing or unreachable, which is a fact about the
# endpoint and not about this request. Observed: a mission spent three attempts
# and two 60-second provider-requested waits on `BadGatewayError`, blew the
# 900s step deadline, and never reached the fallback model that was configured
# and working. Switch models first; the chain comes back to nothing lost.
_GATEWAY_STATUS = {502, 503, 504}
_GATEWAY_NAMES = ("BadGatewayError", "ServiceUnavailableError", "GatewayTimeout")


def is_gateway_fault(exc: Exception) -> bool:
    status = _status_of(exc)
    if status in _GATEWAY_STATUS:
        return True
    return type(exc).__name__ in _GATEWAY_NAMES

# Never retry these, whatever else they look like. A bad key, a model id that
# does not exist on this account, or a malformed request will fail identically
# every time, and burning three attempts on one only delays the error.
_FATAL_STATUS = {400, 401, 403, 404, 413, 422}

# Groq answers an over-large request with 413 and the text below, and
# litellm relabels it RateLimitError -- which reads as transient and is not.
# The request is refused on its declared size before it ever runs, so it will
# be refused identically forever. Matched on the message because the status
# does not always survive the relabelling.
_FATAL_TEXT = ("request too large", "reduce your max_tokens")

_TRANSIENT_NAMES = (
    "RateLimitError", "ServiceUnavailableError", "InternalServerError",
    "APIConnectionError", "APIError", "Timeout", "APITimeoutError",
)
_FATAL_NAMES = (
    "AuthenticationError", "NotFoundError", "BadRequestError",
    "PermissionDeniedError", "ContextWindowExceededError",
    "UnsupportedParamsError",
)


def _status_of(exc: Exception) -> Optional[int]:
    for attr in ("status_code", "http_status", "code"):
        v = getattr(exc, attr, None)
        if isinstance(v, int):
            return v
        if isinstance(v, str) and v.isdigit():
            return int(v)
    m = re.search(r"\b(4\d\d|5\d\d)\b", str(exc))
    return int(m.group(1)) if m else None


def is_transient(exc: Exception) -> bool:
    """
    Whether this failure is worth trying again.

    Status code wins when there is one, because it is unambiguous. The class
    name is the fallback for transport errors that never reached the provider
    and so carry no status at all.
    """
    if isinstance(exc, asyncio.CancelledError):
        return False
    if any(t in str(exc).lower() for t in _FATAL_TEXT):
        return False
    status = _status_of(exc)
    if status in _FATAL_STATUS:
        return False
    if status in _TRANSIENT_STATUS:
        return True
    if status is not None:
        return False

    name = type(exc).__name__
    if any(n in name for n in _FATAL_NAMES):
        return False
    if any(n in name for n in _TRANSIENT_NAMES):
        return True
    if isinstance(exc, (asyncio.TimeoutError, ConnectionError, OSError)):
        return True
    return False


def retry_after_seconds(exc: Exception) -> Optional[float]:
    """
    The wait the provider asked for, if it named one.

    Worth honouring rather than guessing: Gemini replies with a RetryInfo of
    `28s`, which no sane backoff curve would land on, and retrying earlier just
    earns another 429.
    """
    for attr in ("retry_after", "retry_delay"):
        v = getattr(exc, attr, None)
        if isinstance(v, (int, float)) and v > 0:
            return float(v)
    text = str(exc)
    for pattern in (
        r'"retryDelay"\s*:\s*"(\d+(?:\.\d+)?)s"',   # Google RetryInfo
        r"retry[- _]after[\"'\s:]+(\d+(?:\.\d+)?)",  # header echoed into the body
        r"try again in (\d+(?:\.\d+)?)\s*s",         # OpenAI prose
    ):
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                pass
    return None


def _usage_of(response: Any) -> Tuple[int, int]:
    """
    Prompt and completion tokens off a provider response, or zeros.

    Every layer here is optional in practice: a response may carry no `usage`
    at all, carry it as None, or report either count as None. Guessing a number
    when the provider did not send one is what produced the fiction this module
    is meant to end, so an unknown count is 0 and says so by being 0.
    """
    usage = getattr(response, "usage", None)
    if usage is None:
        return 0, 0
    prompt = getattr(usage, "prompt_tokens", None)
    completion = getattr(usage, "completion_tokens", None)
    return int(prompt or 0), int(completion or 0)


def _backoff(attempt: int) -> float:
    """Exponential with jitter. Jitter matters when a wave of agents is
    rate-limited together -- without it they all come back at the same instant
    and trigger the same limit again."""
    base = settings.llm_backoff_base * (2 ** (attempt - 1))
    return round(min(base, settings.llm_backoff_max) * random.uniform(0.7, 1.3), 2)


def _bounded_timeout(requested: float) -> float:
    """The requested timeout, or the step's remaining time if that is shorter."""
    left = step_guard.seconds_left()
    if left is None:
        return requested
    return max(5.0, min(requested, left - 2.0))


async def acompletion(
    messages: List[Dict[str, Any]],
    *,
    model: Optional[str] = None,
    tools: Optional[List[Dict[str, Any]]] = None,
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
    timeout: Optional[float] = None,
    on_retry=None,
) -> LLMResult:
    """
    Call the model, retrying transient failures and falling back once.

    `on_retry(message, kind)` is an optional callback used to narrate what is
    happening -- a mission that pauses 28 seconds for a rate limit should say
    so, not look hung.

    Returns an `LLMResult`: the provider response on `.response`, plus what the
    call actually cost. Raises the last exception if every attempt fails, so
    `llm_strict` still surfaces a genuine misconfiguration instead of it being
    swallowed here.
    """
    import litellm
    litellm.drop_params = True

    primary = model or settings.default_model
    chain = [primary]
    if settings.llm_fallback_enabled:
        fb = (settings.fallback_model or "").strip()
        if fb and fb != primary:
            chain.append(fb)

    last: Optional[Exception] = None

    for model_index, current in enumerate(chain):
        for attempt in range(1, settings.llm_max_retries + 1):
            try:
                kwargs: Dict[str, Any] = {
                    "model": current,
                    "messages": messages,
                    "temperature": settings.temperature if temperature is None else temperature,
                    "max_tokens": settings.max_tokens if max_tokens is None else max_tokens,
                    # Never longer than the step has left: a 180s call
                    # started with 40s on the clock cannot return in time, and
                    # spending the remainder waiting for it costs the step the
                    # chance to file what it already has.
                    "timeout": _bounded_timeout(
                        settings.llm_timeout if timeout is None else timeout),
                    # api_base / api_key / User-Agent when a gateway is set;
                    # empty otherwise, so direct-provider routing is unchanged.
                    **llm_route(),
                }
                if tools:
                    kwargs["tools"] = tools
                    kwargs["tool_choice"] = "auto"

                async with tracing.span(
                    "llm", current, attempt=attempt, fell_back=model_index > 0
                ) as node:
                    response = await litellm.acompletion(**kwargs)
                    prompt_tokens, completion_tokens = _usage_of(response)
                    cost = pricing.price(current, prompt_tokens, completion_tokens)
                    node.set(
                        prompt_tokens=prompt_tokens,
                        completion_tokens=completion_tokens,
                        total_tokens=prompt_tokens + completion_tokens,
                        cost_usd=cost,
                        priced=pricing.is_priced(current),
                    )
                    # Charged to whichever mission is in context, whoever
                    # called. Recording here rather than in each caller is the
                    # point: the planner, the critic and the reviewer were all
                    # invisible to the mission's totals precisely because they
                    # each had to remember to report, and none of them did.
                    token_ledger.record(
                        prompt_tokens=prompt_tokens,
                        completion_tokens=completion_tokens,
                        cost_usd=cost,
                        model=current,
                    )
                    return LLMResult(
                        response=response,
                        model=current,
                        prompt_tokens=prompt_tokens,
                        completion_tokens=completion_tokens,
                        cost_usd=cost,
                        attempts=attempt,
                        fell_back=model_index > 0,
                    )

            except asyncio.CancelledError:
                # An emergency stop, not a provider failure. Never retry it and
                # never swallow it -- the caller is waiting to unwind.
                raise
            except Exception as exc:
                last = exc
                transient = is_transient(exc)
                final_attempt = attempt >= settings.llm_max_retries

                # The gateway is down, not busy. Go to the next model now
                # rather than spending this step's remaining time proving the
                # same endpoint is still down.
                if transient and is_gateway_fault(exc):
                    logger.warning("[llm] %s on %s: gateway fault, not retrying "
                                   "this model", type(exc).__name__, current)
                    if on_retry:
                        try:
                            await on_retry(
                                f"{current} gateway fault ({type(exc).__name__})"
                                f" — switching model", "fallback")
                        except Exception:
                            pass
                    break

                if not transient:
                    logger.error("[llm] %s on %s: %s", type(exc).__name__, current, exc)
                    break           # fatal: try the fallback model, not this one again

                if final_attempt:
                    logger.warning("[llm] %s exhausted %d attempts on %s",
                                   type(exc).__name__, attempt, current)
                    break

                wait = retry_after_seconds(exc)
                asked = wait is not None
                if wait is None:
                    wait = _backoff(attempt)
                wait = min(wait, settings.llm_retry_max_wait)

                # A wait the step cannot afford is not a retry, it is a slower
                # failure: the step dies holding whatever it had not filed yet.
                # Move to the next model instead, and if there is none, raise
                # now so the caller still has time to save its work.
                left = step_guard.seconds_left()
                if left is not None and wait >= max(0.0, left - 5.0):
                    logger.warning("[llm] %s on %s: %.0fs wait does not fit the "
                                   "%.0fs left in this step",
                                   type(exc).__name__, current, wait, left)
                    break

                note = (f"{current} unavailable ({type(exc).__name__})"
                        f"{' — provider asked for' if asked else ', retrying in'} {wait:.0f}s"
                        f" [{attempt}/{settings.llm_max_retries}]")
                logger.warning("[llm] %s", note)
                if on_retry:
                    try:
                        await on_retry(note, "retry")
                    except Exception:
                        pass
                await asyncio.sleep(wait)

        # This model is spent. If another remains, say so before switching.
        if model_index + 1 < len(chain):
            note = f"Falling back from {current} to {chain[model_index + 1]}"
            logger.warning("[llm] %s", note)
            if on_retry:
                try:
                    await on_retry(note, "fallback")
                except Exception:
                    pass

    assert last is not None
    raise last
