"""
Retry behaviour.

The point of these is the classification, not the plumbing: retrying a 429 is
correct and retrying a 401 is actively harmful, because it turns an instant,
legible "your key is wrong" into the same message three backoffs later.
"""
import asyncio

import pytest

import llm
from config import settings


class _Err(Exception):
    """Carries a status the way litellm's exceptions do."""
    def __init__(self, msg="boom", status=None, name=None):
        super().__init__(msg)
        if status is not None:
            self.status_code = status
        if name:
            self.__class__ = type(name, (_Err,), {})


def _mk(name, msg="boom", status=None):
    e = type(name, (Exception,), {})(msg)
    if status is not None:
        e.status_code = status
    return e


# --- classification ------------------------------------------------------

@pytest.mark.parametrize("status", [408, 429, 500, 502, 503, 504])
def test_transient_statuses_retry(status):
    assert llm.is_transient(_mk("APIError", status=status)) is True


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_fatal_statuses_do_not_retry(status):
    assert llm.is_transient(_mk("APIError", status=status)) is False


def test_auth_error_by_name_does_not_retry():
    """No status attribute -- the class name has to carry it."""
    assert llm.is_transient(_mk("AuthenticationError")) is False


def test_connection_error_retries():
    assert llm.is_transient(ConnectionError("connection reset")) is True


def test_cancellation_is_never_transient():
    """An emergency stop must not be retried into life again."""
    assert llm.is_transient(asyncio.CancelledError()) is False


def test_status_in_message_is_read_when_there_is_no_attribute():
    assert llm.is_transient(_mk("Whatever", "server said 503 try later")) is True
    assert llm.is_transient(_mk("Whatever", "server said 401 nope")) is False


# --- provider-requested delay -------------------------------------------

def test_google_retry_info_is_honoured():
    """The exact shape that killed the Gemini run."""
    exc = _mk("RateLimitError",
              '{"type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "28s"}',
              status=429)
    assert llm.retry_after_seconds(exc) == 28.0


def test_openai_prose_delay_is_honoured():
    exc = _mk("RateLimitError", "Rate limit reached. Please try again in 12s.", status=429)
    assert llm.retry_after_seconds(exc) == 12.0


def test_no_delay_named_returns_none():
    assert llm.retry_after_seconds(_mk("APIError", "boom", status=500)) is None


# --- the loop ------------------------------------------------------------

@pytest.fixture
def fast(monkeypatch):
    """No real waiting, and record what was asked for."""
    waits = []

    async def _sleep(s):
        waits.append(s)

    monkeypatch.setattr(llm.asyncio, "sleep", _sleep)
    return waits


@pytest.fixture
def stub(monkeypatch):
    """Replace litellm.acompletion with a scripted sequence."""
    def _install(*outcomes):
        calls = []

        async def _fake(**kwargs):
            calls.append(kwargs["model"])
            out = outcomes[min(len(calls) - 1, len(outcomes) - 1)]
            # BaseException, not Exception: CancelledError is not an Exception
            # subclass, which is exactly why llm.py has to catch it explicitly.
            if isinstance(out, BaseException):
                raise out
            return out

        import litellm
        monkeypatch.setattr(litellm, "acompletion", _fake)
        return calls
    return _install


@pytest.mark.asyncio
async def test_retries_then_succeeds(fast, stub):
    calls = stub(_mk("RateLimitError", status=429), "OK")
    got = await llm.acompletion([{"role": "user", "content": "hi"}])
    assert got.response == "OK"
    assert len(calls) == 2
    assert len(fast) == 1


@pytest.mark.asyncio
async def test_fatal_error_does_not_retry_the_same_model(fast, stub, monkeypatch):
    monkeypatch.setattr(settings, "llm_fallback_enabled", False)
    calls = stub(_mk("AuthenticationError", status=401))
    with pytest.raises(Exception):
        await llm.acompletion([{"role": "user", "content": "hi"}])
    assert len(calls) == 1, "a 401 must fail on the first attempt"
    assert fast == [], "and must not sleep"


@pytest.mark.asyncio
async def test_falls_back_to_the_other_model_once(fast, stub, monkeypatch):
    monkeypatch.setattr(settings, "default_model", "openai/primary")
    monkeypatch.setattr(settings, "fallback_model", "gemini/backup")
    monkeypatch.setattr(settings, "llm_fallback_enabled", True)
    monkeypatch.setattr(settings, "llm_max_retries", 2)

    # Primary exhausts its retries; the fallback answers.
    seq = [_mk("RateLimitError", status=429)] * 2 + ["OK"]
    calls = stub(*seq)
    got = await llm.acompletion([{"role": "user", "content": "hi"}])
    assert got.response == "OK"
    assert calls == ["openai/primary", "openai/primary", "gemini/backup"]


@pytest.mark.asyncio
async def test_raises_last_error_when_everything_fails(fast, stub, monkeypatch):
    monkeypatch.setattr(settings, "llm_max_retries", 2)
    monkeypatch.setattr(settings, "llm_fallback_enabled", False)
    stub(_mk("RateLimitError", "still limited", status=429))
    with pytest.raises(Exception, match="still limited"):
        await llm.acompletion([{"role": "user", "content": "hi"}])


@pytest.mark.asyncio
async def test_provider_delay_is_used_instead_of_backoff(fast, stub):
    stub(_mk("RateLimitError", '"retryDelay": "28s"', status=429), "OK")
    await llm.acompletion([{"role": "user", "content": "hi"}])
    assert fast == [28.0]


@pytest.mark.asyncio
async def test_cancellation_propagates_immediately(fast, stub):
    """HALT must abort, not be retried into another attempt."""
    calls = stub(asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await llm.acompletion([{"role": "user", "content": "hi"}])
    assert len(calls) == 1
    assert fast == []


@pytest.mark.asyncio
async def test_on_retry_is_narrated(fast, stub):
    stub(_mk("RateLimitError", status=429), "OK")
    said = []

    async def note(msg, kind):
        said.append((msg, kind))

    await llm.acompletion([{"role": "user", "content": "hi"}], on_retry=note)
    assert said and said[0][1] == "retry"
