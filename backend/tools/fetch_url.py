"""
Fetch one named URL and return its text.

`web_search` answers "what is out there about X" and returns ranked snippets.
Nothing could answer "what does THIS page say" -- an agent handed a URL by the
search tool, by the user, or by another agent's artifact had no way to open it,
and the observed behaviour is the one this project keeps finding: the model
writes what the page probably says.

The guardrail here is a kind this repo did not have. `terminal` has a command
allowlist, `file_ops` has path containment, and `web_search` is safe in a way
that needed no policy at all -- it only ever talks to api.tavily.com, a single
hardcoded host. This tool talks to whatever address a model names, which makes
it the first outbound request in the harness whose destination is chosen by an
LLM. So it needs egress policy: scheme allowlist, refusal of anything that
resolves into private address space, a redirect cap that re-checks every hop,
and a byte ceiling.

The private-address block is the point of it. Without it, "fetch
http://169.254.169.254/latest/meta-data/" reads the cloud metadata service and
hands an agent the host's credentials, and "http://localhost:8080/api/..."
lets an agent drive this very server through its own tool. Neither is exotic;
both are the first things an injected prompt tries.
"""
import ipaddress
import logging
import re
import socket
from typing import Any, Dict, List
from urllib.parse import urlparse

import httpx  # type: ignore

from config import settings  # type: ignore

logger = logging.getLogger("AgentHQ.FetchURL")

# Only ever these two. `file://` would read the disk through a tool whose whole
# contract is "this is the network", sidestepping file_ops containment, and
# `ftp://`/`gopher://` are request-forgery primitives more than they are ways to
# read a document.
ALLOWED_SCHEMES = ("http", "https")

# Tags whose contents are markup or code rather than text. Dropped whole --
# taking their text out is what stops a page's stylesheet arriving as prose.
_DROP_TAGS = re.compile(
    r"<(script|style|noscript|template|svg)\b[^>]*>.*?</\1>",
    re.I | re.S,
)
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"[ \t\r\f\v]+")
_BLANK_LINES = re.compile(r"\n{3,}")

_ENTITIES = {
    "&nbsp;": " ", "&amp;": "&", "&lt;": "<", "&gt;": ">",
    "&quot;": '"', "&#39;": "'", "&apos;": "'",
}


def _is_public_address(host: str) -> bool:
    """
    Whether every address `host` resolves to is public.

    Every address, not the first: a name that returns one public and one
    loopback address is a DNS rebinding attempt, and answering on the public
    one is exactly what it is counting on.

    Resolution failure is not public. An unresolvable name cannot be fetched
    anyway, and treating "I could not tell" as "it is fine" is the wrong
    default for the one check standing between a model-chosen string and this
    machine's private network.
    """
    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, UnicodeError, OSError):
        return False
    if not infos:
        return False

    for info in infos:
        raw = info[4][0]
        try:
            addr = ipaddress.ip_address(raw)
        except ValueError:
            return False
        # `is_global` is false for every reserved range at once -- loopback,
        # link-local (which is what 169.254.169.254 is), the RFC1918 blocks,
        # CGNAT, multicast and the unspecified address. Enumerating them by
        # hand is how one gets missed.
        if not addr.is_global:
            return False
    return True


def _check_url(url: str) -> Dict[str, Any]:
    """Refusal dict if `url` is not fetchable under policy, else empty."""
    parsed = urlparse((url or "").strip())

    if parsed.scheme.lower() not in ALLOWED_SCHEMES:
        return {
            "success": False,
            "refused": True,
            "error": (f"Only {' and '.join(ALLOWED_SCHEMES)} URLs can be fetched; "
                      f"got '{parsed.scheme or 'no scheme'}'."),
        }

    if not parsed.hostname:
        return {"success": False, "refused": True, "error": "URL has no host."}

    if not _is_public_address(parsed.hostname):
        return {
            "success": False,
            "refused": True,
            "error": (f"'{parsed.hostname}' does not resolve to a public address. "
                      f"Internal and loopback addresses cannot be fetched."),
        }
    return {}


def _to_text(body: str, content_type: str) -> str:
    """HTML to something worth putting in a prompt. Plain text passes through."""
    if "html" not in content_type.lower() and not body.lstrip().startswith("<"):
        return body
    text = _DROP_TAGS.sub(" ", body)
    text = re.sub(r"<br\s*/?>|</p>|</div>|</li>|</h[1-6]>", "\n", text, flags=re.I)
    text = _TAG.sub(" ", text)
    for entity, char in _ENTITIES.items():
        text = text.replace(entity, char)
    text = _WS.sub(" ", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    return _BLANK_LINES.sub("\n\n", text).strip()


async def fetch_url(url: str, max_chars: int = 0) -> Dict[str, Any]:
    """
    Fetch one URL and return its text.

    Never raises: every failure comes back as a result the agent can read and
    act on, for the reason `web_search` documents at length -- a tool that
    throws, or that returns something which merely reads like a page, gets
    called again with different wording until the step's budget is gone.
    """
    refusal = _check_url(url)
    if refusal:
        return refusal

    limit = max_chars if max_chars and max_chars > 0 else settings.fetch_url_max_chars
    limit = min(limit, settings.fetch_url_max_chars)

    visited: List[str] = []
    current = url.strip()

    try:
        # Redirects are followed by hand rather than by httpx, because the
        # policy check has to run on every hop. `follow_redirects=True` would
        # check only the URL the model named and then happily follow a 302 into
        # 127.0.0.1, which is the standard way round an allowlist.
        async with httpx.AsyncClient(follow_redirects=False,
                                     timeout=settings.fetch_url_timeout) as client:
            for _ in range(settings.fetch_url_max_redirects + 1):
                visited.append(current)
                response = await client.get(
                    current,
                    headers={"User-Agent": settings.fetch_url_user_agent},
                )

                if response.status_code in (301, 302, 303, 307, 308):
                    location = response.headers.get("location")
                    if not location:
                        break
                    current = str(httpx.URL(current).join(location))
                    hop_refusal = _check_url(current)
                    if hop_refusal:
                        hop_refusal["error"] = f"Redirected to a blocked URL: {hop_refusal['error']}"
                        return hop_refusal
                    continue

                content_type = response.headers.get("content-type", "")
                if response.status_code >= 400:
                    return {
                        "success": False,
                        "url": current,
                        "status": response.status_code,
                        "error": f"Server returned HTTP {response.status_code}.",
                    }

                body = response.text
                text = _to_text(body, content_type)
                truncated = len(text) > limit
                return {
                    "success": True,
                    "url": current,
                    "final_url": current,
                    "status": response.status_code,
                    "content_type": content_type.split(";")[0].strip(),
                    "chars": len(text),
                    "truncated": truncated,
                    "redirects": visited[:-1],
                    "text": text[:limit] + ("\n\n[truncated]" if truncated else ""),
                }

        return {
            "success": False,
            "url": url,
            "error": (f"Stopped after {settings.fetch_url_max_redirects} redirects "
                      f"without reaching a page."),
        }

    except httpx.TimeoutException:
        return {
            "success": False,
            "url": current,
            "error": f"Timed out after {settings.fetch_url_timeout}s.",
        }
    except httpx.HTTPError as exc:
        return {"success": False, "url": current, "error": f"Request failed: {exc}"}
    except Exception as exc:                                   # noqa: BLE001
        logger.warning("fetch_url failed on %s: %s", current, exc)
        return {"success": False, "url": current, "error": f"Fetch failed: {exc}"}
