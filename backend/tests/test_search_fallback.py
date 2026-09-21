"""
The search provider chain.

One key was a single point of failure, and it failed the expensive way. On
2026-09-21 the configured Tavily key reached its 1,000-search plan limit;
every later search answered HTTP 432, and two fifteen-minute missions did
their research with a tool that could not return a single URL. Worse, the
contract layer still demanded cited sources, so the agent -- correctly
refusing to invent URLs -- was rejected until its repair budget ran out.

So a refusal now falls through to the next provider: a second Tavily key if
one is set, then Jina, which needs no credential beyond the JINA_API_KEY the
reader fallback already uses. A provider that refused is skipped for the rest
of the process rather than being asked again on every query, and only when the
whole chain is refusing does `provider_healthy()` go False and the citation
requirement drop away.
"""
import types

import httpx
import pytest

from config import settings
from tools import web_search


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    web_search.reset_health()
    monkeypatch.setattr(settings, "tavily_api_key", "tvly-primary")
    monkeypatch.setattr(settings, "tavily_api_key_fallback", "")
    monkeypatch.setattr(settings, "jina_api_key", "jina-key")
    monkeypatch.setattr(settings, "jina_search_enabled", True)
    yield
    web_search.reset_health()


class _Resp:
    """A provider answer: a status plus whatever body we want it to carry."""

    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("boom", request=None, response=None)


def _tavily_body(url="https://example.com/a"):
    return {"results": [{"title": "T", "content": "snippet", "url": url}]}


def _jina_body(url="https://jina.example/b"):
    return {"data": [{"title": "J", "description": "snippet", "url": url}]}


def _wire(monkeypatch, *, post, get):
    """Route Tavily (POST) and Jina (GET) to whatever each test needs."""

    class _Client:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, *a, **k):
            if isinstance(post, Exception):
                raise post
            return post

        async def get(self, *a, **k):
            if isinstance(get, Exception):
                raise get
            return get

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _Client(**kw))


# --- the chain --------------------------------------------------------------

def test_the_chain_is_ordered_and_skips_what_is_not_configured(monkeypatch):
    assert web_search.providers() == ["tavily", "jina"]

    monkeypatch.setattr(settings, "tavily_api_key_fallback", "tvly-second")
    assert web_search.providers() == ["tavily", "tavily2", "jina"]

    monkeypatch.setattr(settings, "jina_search_enabled", False)
    assert web_search.providers() == ["tavily", "tavily2"]

    monkeypatch.setattr(settings, "tavily_api_key", "")
    monkeypatch.setattr(settings, "tavily_api_key_fallback", "")
    assert web_search.providers() == []


@pytest.mark.asyncio
async def test_no_provider_at_all_says_so(monkeypatch):
    monkeypatch.setattr(settings, "tavily_api_key", "")
    monkeypatch.setattr(settings, "jina_api_key", "")
    out = await web_search.search_web("q")
    assert out["configured"] is False
    assert web_search.provider_healthy() is False


@pytest.mark.asyncio
async def test_the_primary_answering_never_reaches_the_fallback(monkeypatch):
    _wire(monkeypatch, post=_Resp(200, _tavily_body()), get=AssertionError("jina was called"))
    out = await web_search.search_web("q")
    assert out["provider"] == "tavily"
    assert out["count"] == 1


@pytest.mark.asyncio
async def test_a_spent_key_falls_through_to_the_next_provider(monkeypatch):
    """The regression: HTTP 432 on Tavily must not end the search."""
    _wire(monkeypatch,
          post=_Resp(432, {"detail": {"error": "exceeds your plan's set usage limit"}}),
          get=_Resp(200, _jina_body()))

    out = await web_search.search_web("q")

    assert out["provider"] == "jina"
    assert out["count"] == 1
    assert out["results"][0]["url"] == "https://jina.example/b"
    # The agent gets results, so no "unavailable" note is attached at all.
    assert "note" not in out
    # And a citation is still obtainable, so the contract stays in force.
    assert web_search.provider_healthy() is True


@pytest.mark.asyncio
async def test_a_refused_provider_is_not_asked_again(monkeypatch):
    """A spent key costs a round trip and a 432 on every query otherwise."""
    calls = {"tavily": 0}

    class _Client:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, *a, **k):
            calls["tavily"] += 1
            return _Resp(432, {"detail": {"error": "limit"}})

        async def get(self, *a, **k):
            return _Resp(200, _jina_body())

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _Client(**kw))

    first = await web_search.search_web("one")
    second = await web_search.search_web("two")

    assert first["provider"] == "jina" and second["provider"] == "jina"
    assert calls["tavily"] == 1, "the exhausted key should be tried once, not per query"


@pytest.mark.asyncio
async def test_a_timeout_also_falls_through(monkeypatch):
    _wire(monkeypatch, post=httpx.TimeoutException("slow"), get=_Resp(200, _jina_body()))
    out = await web_search.search_web("q")
    assert out["provider"] == "jina"


@pytest.mark.asyncio
async def test_when_every_provider_refuses_the_note_names_them(monkeypatch):
    _wire(monkeypatch,
          post=_Resp(432, {"detail": {"error": "limit"}}),
          get=_Resp(429, {}))

    out = await web_search.search_web("q")

    assert out["count"] == 0
    assert out["success"] is True          # a failed search never fails the step
    assert out["configured"] is True       # keys exist; the providers refused
    assert "tavily" in out["note"] and "jina" in out["note"]
    # Now, and only now, citations become unobtainable and the contract drops.
    assert web_search.provider_healthy() is False


@pytest.mark.asyncio
async def test_two_tavily_keys_are_tried_before_jina(monkeypatch):
    monkeypatch.setattr(settings, "tavily_api_key_fallback", "tvly-second")
    seen = []

    class _Client:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json=None, headers=None, **k):
            seen.append(headers["Authorization"].split()[-1])
            # The first key is spent; the second answers.
            if headers["Authorization"].endswith("tvly-primary"):
                return _Resp(432, {"detail": {"error": "limit"}})
            return _Resp(200, _tavily_body("https://second.example/c"))

        async def get(self, *a, **k):
            raise AssertionError("jina should not be reached")

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _Client(**kw))

    out = await web_search.search_web("q")

    assert seen == ["tvly-primary", "tvly-second"]
    assert out["provider"] == "tavily2"
    assert out["results"][0]["url"] == "https://second.example/c"


# --- the startup report -----------------------------------------------------

@pytest.mark.asyncio
async def test_the_startup_report_marks_an_exhausted_key_before_any_agent_runs(monkeypatch):
    """
    The whole point of asking at boot: an exhausted key used to be
    indistinguishable from a working one until an agent tried it mid-mission.
    """
    class _Client:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, **k):
            return _Resp(200, {"account": {"current_plan": "Researcher",
                                           "plan_usage": 1000, "plan_limit": 1000}})

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _Client(**kw))

    lines = await web_search.quota_report()

    assert any("EXHAUSTED" in line for line in lines)
    assert any("jina" in line for line in lines)
    # Marked unusable, so the first search of the next mission skips it.
    assert web_search.health()["tavily"]["last_ok"] is False
    # Jina is still there, so citations remain obtainable.
    assert web_search.provider_healthy() is True


@pytest.mark.asyncio
async def test_the_startup_report_states_what_is_left(monkeypatch):
    class _Client:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, **k):
            return _Resp(200, {"account": {"current_plan": "Researcher",
                                           "plan_usage": 60, "plan_limit": 1000}})

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _Client(**kw))

    lines = await web_search.quota_report()

    assert any("940 left" in line for line in lines)
    assert web_search.health().get("tavily") is None, "a healthy key needs no mark"


@pytest.mark.asyncio
async def test_a_usage_check_that_fails_does_not_gate_anything(monkeypatch):
    class _Client:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, **k):
            raise RuntimeError("network down")

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _Client(**kw))

    lines = await web_search.quota_report()

    assert any("usage check failed" in line for line in lines)
    # Unknown is not the same as spent: the provider is still worth trying.
    assert web_search.provider_healthy() is True
