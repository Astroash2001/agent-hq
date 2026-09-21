"""
What a step does when it is running out of time, and what a refused search
provider is allowed to claim.

Both come from one observed mission. A research step was handed a contract
demanding cited URLs while Tavily answered HTTP 432 ("exceeds your plan's set
usage limit") to every query; the tool reported that as "no provider is
configured", so the operator read it as a missing key and the agent -- rightly
refusing to invent URLs -- was rejected until its repairs ran out. A later step
then wrote a complete 10,223-character report, was sent back for a rewrite at
62%, and the rewrite was still waiting on a dead gateway when the 900s step
deadline killed it. The step was recorded FAILED and the good draft was thrown
away.

So: a repair must not be started that the clock cannot pay for, a retry must
not sleep past the deadline, a gateway fault must go straight to the fallback
model, and a provider that refused must say it refused.
"""
import time
import types

import httpx
import pytest

import llm
import step_guard
from tools import web_search


@pytest.fixture(autouse=True)
def clean_health():
    web_search.reset_health()
    yield
    web_search.reset_health()


# --- the step deadline ------------------------------------------------------

def test_no_deadline_bound_means_no_limit():
    """The planner, a test, a bare agent: unbound must behave as before."""
    assert step_guard.seconds_left() is None


def test_a_bound_deadline_counts_down():
    token = step_guard.bind_deadline(time.monotonic() + 30)
    try:
        left = step_guard.seconds_left()
        assert left is not None and 28 < left <= 30
    finally:
        step_guard.reset_deadline(token)
    assert step_guard.seconds_left() is None


def test_a_call_timeout_is_capped_by_the_time_left():
    """A 180s call started with 40s on the clock cannot return in time."""
    token = step_guard.bind_deadline(time.monotonic() + 40)
    try:
        assert llm._bounded_timeout(180.0) == pytest.approx(38.0, abs=1.0)
        # Never below a floor: a 1s timeout fails every call on principle.
        short = step_guard.bind_deadline(time.monotonic() + 1)
        try:
            assert llm._bounded_timeout(180.0) == 5.0
        finally:
            step_guard.reset_deadline(short)
    finally:
        step_guard.reset_deadline(token)


def test_an_unbound_call_keeps_its_requested_timeout():
    assert llm._bounded_timeout(180.0) == 180.0


# --- gateway faults ---------------------------------------------------------

class _BadGatewayError(Exception):
    status_code = 502


class _RateLimitError(Exception):
    status_code = 429


def test_a_gateway_fault_is_recognised_by_status_and_by_name():
    assert llm.is_gateway_fault(_BadGatewayError("upstream unavailable"))
    assert not llm.is_gateway_fault(_RateLimitError("slow down"))


def test_a_gateway_fault_is_still_transient():
    """It must not be reclassified as fatal -- the fallback model is worth a try."""
    assert llm.is_transient(_BadGatewayError("upstream unavailable"))


@pytest.mark.asyncio
async def test_a_gateway_fault_switches_model_instead_of_retrying(monkeypatch):
    """
    The regression. Three attempts and two 60s provider-requested waits on a
    dead gateway burned a 900s step and never reached the configured fallback.
    """
    import litellm
    from config import settings

    monkeypatch.setattr(settings, "default_model", "openai/dead-gateway")
    monkeypatch.setattr(settings, "fallback_model", "openai/works")
    monkeypatch.setattr(settings, "llm_fallback_enabled", True)
    monkeypatch.setattr(settings, "llm_max_retries", 3)

    tried = []

    async def _acompletion(**kwargs):
        tried.append(kwargs["model"])
        if kwargs["model"] == "openai/dead-gateway":
            raise _BadGatewayError("Bad gateway")
        return types.SimpleNamespace(
            usage=types.SimpleNamespace(prompt_tokens=10, completion_tokens=5),
            choices=[types.SimpleNamespace(
                message=types.SimpleNamespace(content="ok", tool_calls=None))])

    monkeypatch.setattr(litellm, "acompletion", _acompletion)

    result = await llm.acompletion([{"role": "user", "content": "hi"}])

    assert result.model == "openai/works"
    assert result.fell_back is True
    # One attempt on the dead endpoint, not three.
    assert tried == ["openai/dead-gateway", "openai/works"]


@pytest.mark.asyncio
async def test_a_retry_wait_longer_than_the_step_has_is_not_slept(monkeypatch):
    import litellm
    from config import settings

    monkeypatch.setattr(settings, "default_model", "openai/throttled")
    monkeypatch.setattr(settings, "fallback_model", "")
    monkeypatch.setattr(settings, "llm_fallback_enabled", False)
    monkeypatch.setattr(settings, "llm_max_retries", 3)

    attempts = []
    slept = []

    async def _acompletion(**kwargs):
        attempts.append(kwargs["model"])
        raise _RateLimitError("rate limited")

    async def _sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(litellm, "acompletion", _acompletion)
    monkeypatch.setattr(llm.asyncio, "sleep", _sleep)
    monkeypatch.setattr(llm, "retry_after_seconds", lambda exc: 60.0)

    token = step_guard.bind_deadline(time.monotonic() + 20)   # 20s left, 60s wait
    try:
        with pytest.raises(_RateLimitError):
            await llm.acompletion([{"role": "user", "content": "hi"}])
    finally:
        step_guard.reset_deadline(token)

    assert len(attempts) == 1, "should not retry into a wait it cannot afford"
    assert slept == [], "should not sleep past the step deadline"


# --- the search provider ----------------------------------------------------

def test_an_unconfigured_provider_is_not_healthy(monkeypatch):
    from config import settings
    # Every provider in the chain, or the chain is not empty: Jina needs no
    # Tavily key, so dropping one alone leaves a working provider behind.
    monkeypatch.setattr(settings, "tavily_api_key", "")
    monkeypatch.setattr(settings, "tavily_api_key_fallback", "")
    monkeypatch.setattr(settings, "jina_api_key", "")
    assert web_search.providers() == []
    assert web_search.provider_healthy() is False


def test_a_configured_provider_is_healthy_until_it_fails(monkeypatch):
    from config import settings
    monkeypatch.setattr(settings, "tavily_api_key", "tvly-test")
    # Nothing attempted yet: unknown counts as healthy, so the first search of
    # a mission is not pre-judged.
    assert web_search.provider_healthy() is True


@pytest.mark.asyncio
async def test_a_plan_limit_refusal_says_so_and_keeps_configured_true(monkeypatch):
    """
    HTTP 432 used to fall through to `raise_for_status`, be caught as
    HTTPStatusError, and be described as "no provider is configured" -- so a
    spent quota read as a missing key.
    """
    from config import settings
    monkeypatch.setattr(settings, "tavily_api_key", "tvly-test")

    class _Response:
        status_code = 432

        @staticmethod
        def json():
            return {"detail": {"error": "This request exceeds your plan's set usage limit."}}

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, *a, **k):
            return _Response()

    monkeypatch.setattr(httpx, "AsyncClient", lambda **k: _Client())
    # One provider, so the 432 is the answer rather than being superseded by
    # the fallback's. The chain is covered in `test_search_fallback.py`.
    monkeypatch.setattr(settings, "tavily_api_key_fallback", "")
    monkeypatch.setattr(settings, "jina_search_enabled", False)

    out = await web_search.search_web("anything")

    assert out["success"] is True          # a failed search never fails the step
    assert out["count"] == 0
    # The key IS configured. Saying otherwise sent an operator looking for a
    # missing key and told the agent to write the tool off entirely.
    assert out["configured"] is True
    assert "432" in out["note"]
    assert "not configured" not in out["note"]
    # And the provider is now known to be refusing, which is what stops the
    # citation contract demanding URLs nobody can fetch.
    assert web_search.provider_healthy() is False


def test_the_citation_contract_is_dropped_when_the_provider_is_refusing(monkeypatch):
    """
    The contract's own docstring says citations are asked for only when they
    were obtainable. A spent key is configured and unobtainable.
    """
    import contracts

    spec_with = contracts.infer_spec(
        "research_notes.md", "Research the market and cite sources",
        citations_expected=True)
    assert spec_with.require_citations is True

    spec_without = contracts.infer_spec(
        "research_notes.md", "Research the market and cite sources",
        citations_expected=False)
    assert spec_without.require_citations is False

    body = "# Notes\n\n" + ("No URLs here. " * 60)
    assert contracts.check(body, spec_with).ok is False
    assert contracts.check(body, spec_without).ok is True


def test_a_provider_that_recovers_is_healthy_again(monkeypatch):
    """
    The last outcome, not a tally. A tally would keep a recovered provider
    marked dead for the rest of the process, and would keep a spent key marked
    healthy because it worked a hundred times this morning.
    """
    from config import settings
    monkeypatch.setattr(settings, "tavily_api_key", "tvly-test")

    monkeypatch.setattr(settings, "tavily_api_key_fallback", "")
    monkeypatch.setattr(settings, "jina_search_enabled", False)

    web_search._mark("tavily", False, "HTTP 432")
    assert web_search.provider_healthy() is False

    web_search._mark("tavily", True)
    assert web_search.provider_healthy() is True
