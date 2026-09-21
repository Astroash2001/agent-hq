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

import httpx  # type: ignore

from config import settings  # type: ignore

logger = logging.getLogger("AgentHQ.WebSearch")

TAVILY_URL = "https://api.tavily.com/search"

# Long enough for `advanced` depth, short enough that a dead provider does not
# eat the agent's own 90s call budget on top of its LLM time.
_TIMEOUT = 20.0

# Snippets are trimmed before they enter the agent's message history.
#
# The ReAct loop re-sends the whole history on every turn, so one tool result
# is not paid for once -- it is paid for on each remaining turn of that step.
# Untrimmed, a single search serialised to ~1,700 tokens and an observed
# research step carried eighteen of them: ~31k prompt tokens per step, and a
# three-step mission that spent 144k of its 150k budget. 600 characters keeps
# the substance of a snippet; the URL is there when an agent needs the rest.
MAX_SNIPPET_CHARS = 600

_NO_PROVIDER = (
    "No web search provider is configured in this deployment, so no external "
    "sources are available for this query. Do not retry the search. Answer from "
    "your own knowledge and say in your output that it was not verified against "
    "live sources."
)

# The provider exists and said no. Worth its own wording: the operator has a
# key to renew, which "not configured" does not tell them.
_SPENT = (
    "The key is configured but the provider is not serving this deployment "
    "right now, so no external sources are available. Do not retry the search. "
    "Answer from your own knowledge and say in your output that it was not "
    "verified against live sources."
)


# Whether the provider is answering, for this process.
#
# A key that is configured and a key that WORKS are different facts, and the
# difference decides whether it is fair to demand citations of an agent.
# `contracts.py` used to read key presence alone, so during an outage a
# research step was told "No sources are cited. Include the URLs you used" on
# every attempt, with a search tool that could not return a single URL. The
# agent correctly refused to invent any, and the step was rejected until it ran
# out of repairs.
# Per-provider health. `last_ok` is tri-state: None = nothing tried yet,
# True = it answered, False = it refused or broke. The LAST outcome rather than
# a running tally, because a tally gets both cases that matter wrong, in
# opposite directions: a key that served a hundred searches this morning and is
# now out of quota would still read as healthy, and a provider that failed once
# at the start would read as dead for the rest of the process.
_health: Dict[str, Dict[str, Any]] = {}


def _mark(name: str, ok: bool, error: str = "") -> None:
    row = _health.setdefault(name, {"attempts": 0, "last_ok": None, "last_error": ""})
    row["attempts"] += 1
    row["last_ok"] = ok
    row["last_error"] = error[:200] if error else ""


def _usable(name: str) -> bool:
    """A provider worth trying: never tried, or last seen working."""
    row = _health.get(name)
    return row is None or row["last_ok"] is not False


def providers() -> List[str]:
    """
    The chain, in order, of providers this deployment could use at all.

    Tavily first because it returns already-extracted, ranked snippets and an
    optional direct answer. A second Tavily key next, if one is set. Jina last:
    it needs no new credential, so it is the one provider that is there when
    every key has run out.
    """
    chain = []
    if settings.tavily_api_key:
        chain.append("tavily")
    if settings.tavily_api_key_fallback:
        chain.append("tavily2")
    if settings.jina_search_enabled and settings.jina_api_key:
        chain.append("jina")
    return chain


def provider_healthy() -> bool:
    """
    Can a citation be obtained right now? False when the deployment has no
    provider at all, and False once every provider in the chain has refused.
    A provider not yet tried counts as usable, so the first search of a mission
    is never pre-judged.

    `contracts.py` reads this: demanding cited URLs of an agent whose every
    search provider is refusing is a contract it cannot satisfy honestly, and
    the only way to pass it would be to invent the URLs.
    """
    return any(_usable(name) for name in providers())


def health() -> Dict[str, Dict[str, Any]]:
    """A copy, for the startup check and the tests."""
    return {k: dict(v) for k, v in _health.items()}


def last_provider_error() -> str:
    """The most recent failure from any provider, for a log line."""
    for row in _health.values():
        if row["last_ok"] is False and row["last_error"]:
            return row["last_error"]
    return ""


def reset_health() -> None:
    """For tests."""
    _health.clear()


def _unavailable(query: str, why: str, configured: bool = False) -> Dict[str, Any]:
    """
    A search that cannot happen, said plainly so the agent moves on.

    `configured` stays True when a key IS set and the provider refused: an
    agent told "not configured" writes off the tool for good, and a reader of
    the log is told the deployment is missing a key when the real fault is a
    spent quota.
    """
    return {
        "success": True,
        "configured": configured,
        # The flag the chain reads. A search that RAN and found nothing also
        # carries a `note` ("try a broader query"), and treating that as a
        # refusal marked a working provider dead and spent the next one's quota
        # on a query that legitimately has no hits.
        "unavailable": True,
        "query": query,
        "count": 0,
        "results": [],
        "note": why,
    }


async def _tavily(query: str, max_results: int, api_key: str,
                  label: str = "tavily") -> Dict[str, Any]:
    payload = {
        "query": query,
        "max_results": max(1, min(int(max_results or settings.search_max_results), 20)),
        "search_depth": settings.search_depth,
        "include_answer": settings.search_include_answer,
        "topic": "general",
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        response = await client.post(TAVILY_URL, json=payload, headers=headers)

    if response.status_code == 401:
        # Distinguished from a transient failure on purpose: a bad key fails
        # identically forever, and the agent should stop asking.
        logger.error("[web_search] %s rejected the API key (401)", label)
        return _unavailable(query, "The configured web search key was rejected. " + _NO_PROVIDER)
    if response.status_code in (402, 403, 429, 432):
        # 432 is Tavily's "this request exceeds your plan's set usage limit".
        # It used to fall through to `raise_for_status` and be reported as
        # HTTPStatusError, which the generic handler then described as "no
        # provider is configured" -- so a spent quota read as a missing key,
        # and that is what sent a mission chasing citations it could never get.
        detail = ""
        try:
            detail = str((response.json() or {}).get("detail") or "")[:200]
        except Exception:
            detail = ""
        logger.warning("[web_search] %s refused the request (%s): %s",
                       label, response.status_code, detail or "no detail")
        because = detail or {
            402: "payment required",
            403: "plan or permission",
            429: "rate limit or quota exhausted",
            432: "plan usage limit exceeded",
        }.get(response.status_code, "refused")
        return _unavailable(
            query,
            f"The web search provider refused the request (HTTP "
            f"{response.status_code}: {because}). " + _SPENT,
            configured=True)
    response.raise_for_status()

    body = response.json()
    results: List[Dict[str, str]] = []
    for item in (body.get("results") or []):
        snippet = (item.get("content") or "").strip()
        if len(snippet) > MAX_SNIPPET_CHARS:
            snippet = snippet[:MAX_SNIPPET_CHARS].rstrip() + " ..."
        results.append({
            "title": item.get("title", ""),
            # `content` is Tavily's extracted snippet. Renamed to `snippet` so
            # the shape matches what this tool has always returned and the
            # agent prompts do not have to change.
            "snippet": snippet,
            "url": item.get("url", ""),
        })

    out: Dict[str, Any] = {
        "success": True,
        "configured": True,
        "provider": label,
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


async def _jina(query: str, max_results: int) -> Dict[str, Any]:
    """
    Search through s.jina.ai, on the key the reader fallback already holds.

    Deliberately asked for WITHOUT page content (`X-Respond-With: no-content`).
    Jina will happily return each result's full text, and this tool's output is
    re-sent in the agent's history on every remaining turn of the step -- ten
    rendered pages would cost more than the work they inform. Title, URL and
    description is what a citation needs.
    """
    headers = {
        "Accept": "application/json",
        "X-Respond-With": "no-content",
        "Authorization": f"Bearer {settings.jina_api_key}",
    }
    wanted = max(1, min(int(max_results or settings.search_max_results), 20))

    async with httpx.AsyncClient(timeout=settings.jina_reader_timeout) as client:
        response = await client.get(settings.jina_search_base,
                                    params={"q": query}, headers=headers)

    if response.status_code in (401, 402, 403, 429):
        detail = {401: "key rejected", 402: "payment required",
                  403: "plan or permission", 429: "rate limited"}[response.status_code]
        logger.warning("[web_search] Jina refused the request (%s): %s",
                       response.status_code, detail)
        return _unavailable(
            query,
            f"The web search provider refused the request (HTTP "
            f"{response.status_code}: {detail}). " + _SPENT,
            configured=True)
    response.raise_for_status()

    body = response.json()
    results: List[Dict[str, str]] = []
    for item in (body.get("data") or [])[:wanted]:
        snippet = str(item.get("description") or "").strip()
        if len(snippet) > MAX_SNIPPET_CHARS:
            snippet = snippet[:MAX_SNIPPET_CHARS].rstrip() + " ..."
        results.append({
            "title": str(item.get("title") or ""),
            "snippet": snippet,
            "url": str(item.get("url") or ""),
        })

    out: Dict[str, Any] = {
        "success": True,
        "configured": True,
        "provider": "jina",
        "query": query,
        "count": len(results),
        "results": results,
    }
    if not results:
        out["note"] = ("The search returned no results. Try a broader query once, "
                       "then answer from your own knowledge.")
    return out


async def quota_report() -> List[str]:
    """
    What each Tavily key has left, for one line at startup.

    A key whose plan is spent is indistinguishable from a working one until an
    agent tries it mid-mission, which is how two fifteen-minute missions were
    spent researching with a search tool that could not return a single URL.
    Tavily publishes the numbers; asking once at boot costs nothing and no
    search quota (`/usage` is not a search).

    Returns one human-readable line per key, and marks a spent key unusable
    before any agent reaches it.
    """
    lines: List[str] = []
    for label, key in (("tavily", settings.tavily_api_key),
                       ("tavily2", settings.tavily_api_key_fallback)):
        if not key:
            continue
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.get("https://api.tavily.com/usage",
                                     headers={"Authorization": f"Bearer {key}"})
            if r.status_code != 200:
                lines.append(f"{label}: usage unavailable (HTTP {r.status_code})")
                continue
            account = (r.json() or {}).get("account") or {}
            used = int(account.get("plan_usage") or 0)
            limit = int(account.get("plan_limit") or 0)
            plan = str(account.get("current_plan") or "unknown")
            if limit and used >= limit:
                # Marked now rather than discovered later: `provider_healthy()`
                # reads this, so the citation contract already knows.
                _mark(label, False, f"plan limit reached ({used}/{limit})")
                lines.append(f"{label}: {used}/{limit} used on {plan} -- EXHAUSTED, "
                             f"searches will fall through to the next provider")
            else:
                left = limit - used if limit else 0
                lines.append(f"{label}: {used}/{limit} used on {plan}"
                             + (f", {left} left" if limit else ""))
        except Exception as exc:                   # noqa: BLE001 - a report, not a gate
            lines.append(f"{label}: usage check failed ({type(exc).__name__})")

    if settings.jina_search_enabled and settings.jina_api_key:
        lines.append("jina: available as the fallback search provider")
    if not lines:
        lines.append("no web search provider is configured")
    return lines


async def _run(name: str, query: str, max_results: int) -> Dict[str, Any]:
    """One provider attempt, by name."""
    if name == "tavily":
        return await _tavily(query, max_results, settings.tavily_api_key, "tavily")
    if name == "tavily2":
        return await _tavily(query, max_results,
                             settings.tavily_api_key_fallback, "tavily2")
    return await _jina(query, max_results)


async def search_web(query: str, max_results: int = 0) -> Dict[str, Any]:
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

    chain = providers()
    if not chain:
        return _unavailable(query, _NO_PROVIDER)

    # Providers already known to be refusing are skipped rather than retried on
    # every query: a spent Tavily key costs a round trip and a 432 each time,
    # and the agent has a step deadline to meet. They are skipped only for
    # THIS process -- a restart, or a renewed plan, starts them clean.
    live = [name for name in chain if _usable(name)] or []
    last_note = ""

    for name in live:
        try:
            out = await _run(name, query, max_results)
            # A refusal comes back as a result rather than an exception, and
            # `unavailable` is what marks it -- not the presence of a `note`,
            # which a successful empty search carries too.
            refused = bool(out.get("unavailable"))
            _mark(name, not refused, str(out.get("note", "")))
            if not refused:
                return out
            last_note = str(out.get("note", ""))
            logger.warning("[web_search] %s refused; %s", name,
                           "trying the next provider" if name != live[-1]
                           else "no provider left")
        except httpx.TimeoutException:
            logger.warning("[web_search] %s timed out", name)
            _mark(name, False, "timeout")
            last_note = f"The {name} search timed out."
        except Exception as exc:                   # noqa: BLE001 - reported, not raised
            # A failing search must not fail the step. The agent still has its
            # own knowledge, and the note tells it to use it.
            logger.warning("[web_search] %s: %s: %s", name, type(exc).__name__, exc)
            _mark(name, False, f"{type(exc).__name__}: {exc}")
            last_note = f"The {name} search failed ({type(exc).__name__})."

    # Every provider in the chain is refusing. Said once, plainly, so the agent
    # stops asking and writes what it knows -- with the caveat.
    skipped = [n for n in chain if n not in live]
    where = ", ".join(chain) + (f" (already refused: {', '.join(skipped)})" if skipped else "")
    # `configured` reports whether this deployment HAS a provider, which it
    # does -- they refused. The agent learns it cannot search from the note;
    # `provider_healthy()` is what the contract layer reads.
    return _unavailable(
        query,
        f"{last_note} Every configured search provider ({where}) is "
        f"unavailable. " + _SPENT,
        configured=True)
