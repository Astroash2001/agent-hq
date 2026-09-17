"""
Fetch one named URL and return its text.

`web_search` answers "what is out there about X" and returns ranked snippets.
Nothing could answer "what does THIS page say" -- an agent handed a URL by the
search tool, by the user, or by another agent's artifact had no way to open it,
and the observed behaviour is the one this project keeps finding: the model
writes what the page probably says.

The guardrail here is a kind this repo did not have. `file_ops` has path
containment, and `web_search` is safe in a way
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


# A page whose content is written by JavaScript arrives as a shell: the markup
# is there, the figures are not. Observed on three Airbnb listings whose price
# area was still the string "loading" when the HTML landed -- the agent read a
# complete-looking page, found no price, and reported that no stay could be
# confirmed under the budget. That is a fact about the instrument written as a
# fact about the world.
_SHELL_SIGNS = (
    "enable javascript", "javascript is required", "loading…", "loading...",
    "please wait while", "you need to enable javascript",
)


def _looks_unrendered(text: str, html: str) -> bool:
    """Whether this page probably keeps its content behind JavaScript."""
    body = (text or "").strip()
    low = body.lower()
    if any(sign in low for sign in _SHELL_SIGNS):
        return True
    # Lots of markup, almost no words: the shape of an app that has not run yet.
    return bool(html) and len(html) > 40_000 and len(body) < 0.02 * len(html)


def _shell_hint(rendered_available: bool) -> str:
    if rendered_available:
        return ("This page appears to build its content with JavaScript, so figures "
                "such as prices may be missing from what you just read. Call "
                "`render_page` on the same URL before concluding anything is "
                "unavailable.")
    return ("This page appears to build its content with JavaScript, so figures "
            "such as prices are probably missing from what you just read, and this "
            "deployment has no browser to render it. Say that the page could not be "
            "read; do NOT report the figure as unavailable in the world.")


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


try:
    from bs4 import BeautifulSoup  # type: ignore
    _BS4_AVAILABLE = True
except ImportError:
    _BS4_AVAILABLE = False

from tools.file_ops import write_file


async def fetch_url(
    url: str,
    max_chars: int = 0,
    offset: int = 0,
    save_as: str = "",
    selector: str = "",
) -> Dict[str, Any]:
    """
    Fetch one URL and return its text, page through it, or save it directly.

    Never raises: every failure comes back as a result the agent can read and
    act on, for the reason `web_search` documents at length -- a tool that
    throws, or that returns something which merely reads like a page, gets
    called again with different wording until the step's budget is gone.

    Parameters:
        url: Full http/https URL to fetch.
        max_chars: Maximum characters to return in text (capped at config limit).
        offset: Character index to start slice from (for paging large documents).
        save_as: Workspace filename to save complete raw or selected content into,
                 bypassing LLM prompt token limits.
        selector: Optional CSS selector to extract specific HTML elements.
    """
    refusal = _check_url(url)
    if refusal:
        return refusal

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
                content_type_clean = content_type.split(";")[0].strip()
                if response.status_code >= 400:
                    return {
                        "success": False,
                        "url": current,
                        "status": response.status_code,
                        "error": f"Server returned HTTP {response.status_code}.",
                    }

                body = response.text
                is_html = "html" in content_type.lower() or body.lstrip().startswith("<")

                # Targeted CSS selector extraction.
                #
                # `selector_applied` is tracked rather than inferred from the
                # text, because every way a selector could fail used to end
                # with `text` holding a sentence ABOUT the failure -- and a
                # sentence is indistinguishable from content once it is written
                # to a file or handed to the next turn. Saving "No elements
                # matched '.store'" into stores.html and reporting
                # "Successfully saved 51 bytes" is this repo's oldest bug
                # wearing a CSS selector.
                matched_elements = []
                selector_applied = False
                selector_note = ""
                if selector and selector.strip():
                    sel = selector.strip()
                    if not _BS4_AVAILABLE:
                        selector_note = (
                            f"Selector '{sel}' was NOT applied: beautifulsoup4 is not "
                            f"installed, so the whole page text is returned instead.")
                        text = _to_text(body, content_type)
                    elif not is_html:
                        selector_note = (
                            f"Selector '{sel}' was NOT applied: this response is "
                            f"{content_type_clean or 'not HTML'}, so the body is "
                            f"returned as-is.")
                        text = _to_text(body, content_type)
                    else:
                        try:
                            soup = BeautifulSoup(body, "html.parser")
                            matched_elements = soup.select(sel)
                        except Exception as exc:              # noqa: BLE001
                            logger.warning("CSS selector %r failed on %s: %s",
                                           sel, current, exc)
                            matched_elements = []
                            selector_note = (
                                f"Selector '{sel}' is not valid CSS ({exc}); the whole "
                                f"page text is returned instead.")
                            text = _to_text(body, content_type)
                        else:
                            items = [
                                el.get_text(separator=" ", strip=True)
                                for el in matched_elements
                                if el.get_text(separator=" ", strip=True)
                            ]
                            if items:
                                selector_applied = True
                                text = "\n\n".join(items)
                            elif matched_elements:
                                selector_note = (
                                    f"Selector '{sel}' matched {len(matched_elements)} "
                                    f"element(s), but none of them contain any text.")
                                text = ""
                            else:
                                selector_note = (
                                    f"Selector '{sel}' matched no elements on this page.")
                                text = ""
                else:
                    text = _to_text(body, content_type)

                # Direct workspace file staging to prevent LLM context bloat
                if save_as and save_as.strip():
                    save_target = save_as.strip()

                    # A selector that did not apply must never be written to
                    # disk. The content in `text` is then a sentence about the
                    # failure, and saving it under the name the agent chose for
                    # its data -- then reporting "Successfully saved 51 bytes"
                    # -- hands the next step an error message where it expects
                    # records. Refusing is the honest outcome: the agent still
                    # has the note, and can fix the selector or drop it.
                    if selector and selector.strip() and not selector_applied:
                        return {
                            "success": False,
                            "url": current,
                            "status": response.status_code,
                            "content_type": content_type_clean,
                            "selector_applied": False,
                            "error": (
                                f"Nothing was saved to '{save_target}': {selector_note} "
                                f"Fix the selector, or omit it to save the whole page."
                            ),
                        }

                    if selector and matched_elements and save_target.lower().endswith((".html", ".htm")):
                        content_to_save = "\n\n".join(str(el) for el in matched_elements)
                    elif selector:
                        content_to_save = text
                    else:
                        content_to_save = body

                    file_res = await write_file(filepath=save_target, content=content_to_save)
                    if not file_res.get("success"):
                        return {
                            "success": False,
                            "url": current,
                            "error": f"Failed to save content to '{save_target}': {file_res.get('error', 'write refused')}",
                        }

                    saved_name = file_res.get("filepath", save_target)
                    bytes_saved = file_res.get("bytes_written", len(content_to_save))
                    preview_len = min(500, len(text))
                    preview = text[:preview_len]

                    return {
                        "success": True,
                        "url": current,
                        "final_url": current,
                        "status": response.status_code,
                        "content_type": content_type_clean,
                        "saved_to": saved_name,
                        "bytes_saved": bytes_saved,
                        "selector_applied": selector_applied,
                        "chars": len(text),
                        "total_chars": len(text),
                        "truncated": False,
                        "preview": preview,
                        "redirects": visited[:-1],
                        "text": (
                            f"Successfully saved {bytes_saved} bytes to '{saved_name}' in workspace. "
                            f"Total extracted text length: {len(text)} characters.\n\n"
                            f"--- CONTENT PREVIEW (first {preview_len} chars) ---\n"
                            f"{preview}\n\n"
                            f"Guidance: The full page/data is stored safely on disk. Use query_data, read_file, "
                            f"or write a Python script to process all records without context window truncation."
                        ),
                    }

                # Paginated text output for prompt consumption
                offset_val = max(0, int(offset or 0))
                limit = max_chars if max_chars and max_chars > 0 else settings.fetch_url_max_chars
                limit = min(limit, settings.fetch_url_max_chars)

                total_chars = len(text)

                # Asking past the end used to return success with an empty
                # string and no explanation -- the silent result this whole
                # tool exists to stop, reached by the pagination that was added
                # to stop it. Said plainly instead, and never with empty text:
                # an agent that reads "" decides for itself what it means.
                if total_chars and offset_val >= total_chars:
                    return {
                        "success": True,
                        "url": current,
                        "final_url": current,
                        "status": response.status_code,
                        "content_type": content_type_clean,
                        "chars": 0,
                        "total_chars": total_chars,
                        "offset": offset_val,
                        "truncated": False,
                        "end_of_content": True,
                        "selector_applied": selector_applied,
                        "redirects": visited[:-1],
                        "text": (
                            f"End of content: offset {offset_val} is at or past the end "
                            f"of this document, which is {total_chars} characters long. "
                            f"Nothing further to read here."
                        ),
                    }

                slice_end = min(offset_val + limit, total_chars)
                text_slice = text[offset_val:slice_end]
                truncated = slice_end < total_chars
                next_offset = slice_end if truncated else None

                if truncated:
                    hint = (
                        f"Content truncated ({slice_end}/{total_chars} characters shown). "
                        f"To read further, call fetch_url with offset={next_offset}, or use "
                        f"save_as='page.html' to save the complete response directly to the workspace."
                    )
                    final_text = (
                        text_slice
                        + f"\n\n[...content truncated at {slice_end}/{total_chars} chars. Call fetch_url with offset={next_offset} or save_as='page.html' to save full content to workspace]"
                    )
                else:
                    hint = ""
                    final_text = text_slice

                if not hint and _looks_unrendered(text, body):
                    try:
                        from tools.render_page import render_available
                        can_render = render_available()
                    except Exception:                       # noqa: BLE001
                        can_render = False
                    hint = _shell_hint(can_render)

                result: Dict[str, Any] = {
                    "success": True,
                    "url": current,
                    "final_url": current,
                    "status": response.status_code,
                    "content_type": content_type_clean,
                    "chars": len(text_slice),
                    "total_chars": total_chars,
                    "offset": offset_val,
                    "truncated": truncated,
                    "selector_applied": selector_applied,
                    "redirects": visited[:-1],
                    "text": final_text,
                }
                if selector_note:
                    # Said in `text` as well as in a field: a caller reading
                    # only the prose must not think a selector was applied when
                    # it silently was not.
                    result["selector_note"] = selector_note
                    result["text"] = f"[{selector_note}]" + "\n\n" + result["text"]
                if truncated:
                    result["next_offset"] = next_offset
                    result["hint"] = hint

                return result

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
