"""
Web search.

Backed by Tavily when TAVILY_API_KEY is set, and honest about it when not.

The honesty matters more than it sounds. This tool used to be a pure mock: it
had canned snippets for three keywords and, for anything else, returned filler
that *read* like a result -- "Found latest technical documentation ... related
to '<query>'" -- while carrying nothing. A capable model treats that as a
failed search and tries again with different wording. An observed gpt-5.5 run
called this tool eleven times on one subtask, exhausted its step budget, and
filed a 39-byte stub as the research the rest of the mission was built on.

So every path here returns something the agent can act on: real results, or a
clear statement that no search is available and it should answer from its own
knowledge and say so. Never filler.

Tavily is used rather than a scraper because it is built for this: it returns
ranked, already-extracted snippets and an optional direct answer, so the agent
does not have to fetch and parse pages itself.
"""
import logging
from typing import Any, Dict, List

import httpx

from config import settings

logger = logging.getLogger("AgentHQ.WebSearch")

TAVILY_URL = "https://api.tavily.com/search"

# Long enough for `advanced` depth, short enough that a dead provider does not
# eat the agent's own 90s call budget on top of its LLM time.
_TIMEOUT = 20.0

_NO_PROVIDER = (
    "No web search provider is configured in this deployment, so no external "
    "sources are available for this query. Do not retry the search. Answer from "
    "your own knowledge and say in your output that it was not verified against "
    "live sources."
)


def _unavailable(query: str, why: str) -> Dict[str, Any]:
    """A search that cannot happen, said plainly so the agent moves on."""
    return {
        "success": True,
        "configured": False,
        "query": query,
        "count": 0,
        "results": [],
        "note": why,
    }


async def _tavily(query: str, max_results: int) -> Dict[str, Any]:
    payload = {
        "query": query,
        "max_results": max(1, min(int(max_results or 5), 20)),
        "search_depth": settings.search_depth,
        "include_answer": settings.search_include_answer,
        "topic": "general",
    }
    headers = {
        "Authorization": f"Bearer {settings.tavily_api_key}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        response = await client.post(TAVILY_URL, json=payload, headers=headers)

    if response.status_code == 401:
        # Distinguished from a transient failure on purpose: a bad key fails
        # identically forever, and the agent should stop asking.
        logger.error("[web_search] Tavily rejected the API key (401)")
        return _unavailable(query, "The configured web search key was rejected. " + _NO_PROVIDER)
    if response.status_code == 429:
        logger.warning("[web_search] Tavily rate limit / quota reached (429)")
        return _unavailable(query, "The web search quota is exhausted. " + _NO_PROVIDER)
    response.raise_for_status()

    body = response.json()
    results: List[Dict[str, str]] = []
    for item in (body.get("results") or []):
        results.append({
            "title": item.get("title", ""),
            # `content` is Tavily's extracted snippet. Renamed to `snippet` so
            # the shape matches what this tool has always returned and the
            # agent prompts do not have to change.
            "snippet": item.get("content", ""),
            "url": item.get("url", ""),
            "score": item.get("score"),
        })

    out: Dict[str, Any] = {
        "success": True,
        "configured": True,
        "provider": "tavily",
        "query": query,
        "count": len(results),
        "results": results,
    }
    answer = body.get("answer")
    if answer:
        out["answer"] = answer
    if not results:
        out["note"] = ("The search returned no results. Try a broader query once, "
                       "then answer from your own knowledge.")
    return out


async def search_web(query: str, max_results: int = 5) -> Dict[str, Any]:
    """
    Search the web for information.

    Returns ranked snippets with source URLs. When no provider is configured,
    or the provider fails, it says so rather than inventing results -- an agent
    that is told the search is unavailable can fall back on what it knows,
    which is far better than one that keeps retrying an empty tool.
    """
    query = (query or "").strip()
    if not query:
        return {"success": False, "error": "A search needs a query."}

    if not settings.tavily_api_key:
        return _unavailable(query, _NO_PROVIDER)

    try:
        return await _tavily(query, max_results)
    except httpx.TimeoutException:
        logger.warning("[web_search] Tavily timed out after %ss", _TIMEOUT)
        return _unavailable(query, "The web search timed out. " + _NO_PROVIDER)
    except Exception as exc:                       # noqa: BLE001 - reported, not raised
        # A failing search must not fail the step. The agent still has its own
        # knowledge, and the note tells it to use it.
        logger.warning("[web_search] %s: %s", type(exc).__name__, exc)
        return _unavailable(query, f"The web search failed ({type(exc).__name__}). " + _NO_PROVIDER)
