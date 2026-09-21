"""
Web search.

The rule every one of these enforces: the tool may fail, but it must never
return something that *looks* like a result and isn't, and it must never raise
into the agent loop. The old mock broke the first rule -- its filler snippet
sent a gpt-5.5 researcher round eleven searches until its step budget was gone.
"""
import httpx
import pytest

from config import settings
from tools import web_search


class _Resp:
    """Minimal stand-in for httpx.Response."""
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("boom", request=None, response=None)


@pytest.fixture(autouse=True)
def single_provider(monkeypatch):
    """
    Pin the chain to Tavily alone.

    Every test in this file asserts what ONE provider does with a given
    response. `search_web` now falls through to the next provider on a refusal,
    so without this a 432 would be answered by Jina and the assertions would be
    describing the fallback. The chain itself is covered in
    `test_search_fallback.py`.
    """
    monkeypatch.setattr(settings, "tavily_api_key_fallback", "")
    monkeypatch.setattr(settings, "jina_search_enabled", False)
    web_search.reset_health()
    yield
    web_search.reset_health()


@pytest.fixture
def keyed(monkeypatch):
    monkeypatch.setattr(settings, "tavily_api_key", "tvly-test")


def _post(monkeypatch, result):
    """Patch the one HTTP call the tool makes."""
    sent = {}

    async def _fake_post(self, url, json=None, headers=None):
        sent["url"] = url
        sent["json"] = json
        sent["headers"] = headers
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(httpx.AsyncClient, "post", _fake_post)
    return sent


# --- no key --------------------------------------------------------------

@pytest.mark.asyncio
async def test_no_key_says_so_and_does_not_pretend(monkeypatch):
    monkeypatch.setattr(settings, "tavily_api_key", "")
    r = await web_search.search_web("solar panel efficiency")
    assert r["success"] is True          # the step continues
    assert r["configured"] is False
    assert r["results"] == []
    assert "Do not retry" in r["note"]


@pytest.mark.asyncio
async def test_empty_query_is_rejected():
    r = await web_search.search_web("   ")
    assert r["success"] is False


# --- the happy path ------------------------------------------------------

@pytest.mark.asyncio
async def test_results_are_mapped_and_auth_is_a_bearer_header(keyed, monkeypatch):
    sent = _post(monkeypatch, _Resp(200, {
        "answer": "Commercial panels reach 20-23%.",
        "results": [
            {"title": "NREL chart", "content": "Record cell efficiencies.",
             "url": "https://nrel.gov/x", "score": 0.9},
            {"title": "Tandem cells", "content": "Perovskite-silicon past 30%.",
             "url": "https://example.com/y", "score": 0.8},
        ],
    }))
    r = await web_search.search_web("solar panel efficiency", max_results=2)

    assert sent["url"] == web_search.TAVILY_URL
    assert sent["headers"]["Authorization"] == "Bearer tvly-test"
    assert sent["json"]["query"] == "solar panel efficiency"
    assert sent["json"]["max_results"] == 2

    assert r["configured"] is True and r["provider"] == "tavily"
    assert r["count"] == 2
    # Tavily calls it `content`; the tool has always returned `snippet`.
    assert r["results"][0]["snippet"] == "Record cell efficiencies."
    assert r["results"][0]["url"] == "https://nrel.gov/x"
    assert r["answer"].startswith("Commercial panels")


@pytest.mark.asyncio
async def test_max_results_is_clamped_to_the_api_range(keyed, monkeypatch):
    sent = _post(monkeypatch, _Resp(200, {"results": []}))
    await web_search.search_web("q", max_results=500)
    assert sent["json"]["max_results"] == 20, "Tavily's ceiling is 20"
    # 0 is falsy and read as "unspecified", so it takes the configured default
    # rather than asking for a single result -- a model passing 0 means it did
    # not have an opinion, not that it wants one hit.
    await web_search.search_web("q", max_results=0)
    assert sent["json"]["max_results"] == settings.search_max_results
    await web_search.search_web("q", max_results=-3)
    assert sent["json"]["max_results"] == 1


@pytest.mark.asyncio
async def test_zero_results_tells_the_agent_to_move_on(keyed, monkeypatch):
    _post(monkeypatch, _Resp(200, {"results": []}))
    r = await web_search.search_web("a query with no hits")
    assert r["count"] == 0
    assert "own knowledge" in r["note"]


# --- failure paths: none of these may raise ------------------------------

@pytest.mark.asyncio
async def test_bad_key_is_reported_not_raised(keyed, monkeypatch):
    _post(monkeypatch, _Resp(401))
    r = await web_search.search_web("q")
    # `configured` says this deployment HAS a key. A rejected one is a wrong
    # key, not a missing one, and the two want different fixes from the
    # operator. What the agent needs -- stop asking -- is in the note, and what
    # the contract layer needs is `provider_healthy()`.
    assert r["success"] is True and r["configured"] is True
    assert "rejected" in r["note"]
    assert "Do not retry" in r["note"]
    assert web_search.provider_healthy() is False


@pytest.mark.asyncio
async def test_quota_exhausted_is_reported_not_raised(keyed, monkeypatch):
    """
    `configured` means "this deployment has a key", not "the last search
    worked". It used to be False for every refusal, which told the operator a
    key was missing when the real fault was a spent quota -- and told the agent
    to write the tool off rather than try again next mission. The note carries
    the fault; `provider_healthy()` carries whether it is currently answering.
    """
    _post(monkeypatch, _Resp(429))
    r = await web_search.search_web("q")
    assert r["configured"] is True
    assert r["count"] == 0
    assert "429" in r["note"] or "quota" in r["note"]
    assert "Do not retry" in r["note"]
    assert web_search.provider_healthy() is False


@pytest.mark.asyncio
async def test_timeout_does_not_fail_the_step(keyed, monkeypatch):
    _post(monkeypatch, httpx.TimeoutException("slow"))
    r = await web_search.search_web("q")
    assert r["success"] is True and "timed out" in r["note"]
    assert web_search.provider_healthy() is False


@pytest.mark.asyncio
async def test_unexpected_error_does_not_fail_the_step(keyed, monkeypatch):
    _post(monkeypatch, RuntimeError("provider exploded"))
    r = await web_search.search_web("q")
    # Still a successful tool call carrying no results, and still a configured
    # deployment -- the provider broke, the key did not vanish.
    assert r["success"] is True and r["configured"] is True
    assert "RuntimeError" in r["note"]
    assert web_search.provider_healthy() is False


@pytest.mark.asyncio
async def test_every_failure_path_tells_the_agent_not_to_loop(keyed, monkeypatch):
    """The regression that cost a whole subtask: filler that invites a retry."""
    for outcome in (_Resp(401), _Resp(429), httpx.TimeoutException("x"), RuntimeError("y")):
        _post(monkeypatch, outcome)
        r = await web_search.search_web("q")
        assert "Do not retry the search" in r["note"]
        assert r["results"] == []
