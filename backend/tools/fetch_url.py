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
from html import unescape
from typing import Any, Dict, List, Tuple
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

# Optional, like bs4 below: without it every page goes through `_strip_tags`.
try:
    import trafilatura  # type: ignore
    _TRAFILATURA_AVAILABLE = True
except ImportError as exc:
    # Logged, not silent: a missing transitive dependency (lxml_html_clean)
    # otherwise looks exactly like "trafilatura found nothing".
    logger.warning("trafilatura unavailable, fetch_url uses the tag strip: %s", exc)
    _TRAFILATURA_AVAILABLE = False


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


def _is_html(body: str, content_type: str) -> bool:
    return "html" in content_type.lower() or body.lstrip().startswith("<")


def _to_text(body: str, content_type: str) -> str:
    """
    HTML to something worth putting in a prompt. Plain text passes through.

    The tag strip alone keeps every menu, footer and cookie banner, and on a
    long page that boilerplate fills `fetch_url_max_chars` before the article
    does. trafilatura keeps the main content and its tables. It returns None
    when it finds no main content (tiny pages, app shells), and the tag strip
    takes over, so nothing that used to come back now comes back empty.
    """
    if not _is_html(body, content_type):
        return body
    if _TRAFILATURA_AVAILABLE:
        try:
            main = trafilatura.extract(body, include_tables=True, favor_recall=True)
        except Exception as exc:                            # noqa: BLE001
            logger.warning("trafilatura failed, using the tag strip: %s", exc)
            main = None
        if main and main.strip():
            return main.strip()
        logger.info("trafilatura found no main content (%d bytes of HTML); "
                    "using the tag strip", len(body))
    return _strip_tags(body)


# A `query` answer: the best passages, capped. A whole page is re-sent on every
# later turn of the step, and an observed three-page mission spent 344k of its
# 340k token budget mostly on 20,000-character slices it needed one line of.
MAX_QUERY_PASSAGES = 8
MAX_QUERY_CHARS = 6000


def _matching_passages(text: str, query: str) -> Tuple[str, int]:
    """
    The lines of `text` that best match `query`, in page order, and how many matched.

    Ranked by how many distinct query words a line holds, then by how often.
    Words of two letters or fewer are ignored unless they are all there is.
    """
    words = re.findall(r"\w+", query.lower())
    terms = [w for w in words if len(w) > 2] or words
    if not terms:
        return "", 0
    scored = []
    for i, line in enumerate(l.strip() for l in text.split("\n")):
        low = line.lower()
        hits = [low.count(t) for t in terms]
        distinct = sum(1 for h in hits if h)
        if distinct:
            scored.append((distinct, sum(hits), i, line))
    best = sorted(scored, key=lambda s: (-s[0], -s[1], s[2]))[:MAX_QUERY_PASSAGES]
    out, used = [], 0
    for *_, line in sorted(best, key=lambda s: s[2]):
        if used + len(line) > MAX_QUERY_CHARS:
            line = line[:max(0, MAX_QUERY_CHARS - used)]
        if line:
            out.append(line)
            used += len(line)
    return "\n\n".join(out), len(scored)


def _strip_tags(body: str) -> str:
    """Every visible word on the page, boilerplate included."""
    text = _DROP_TAGS.sub(" ", body)
    text = re.sub(r"<br\s*/?>|</p>|</div>|</li>|</h[1-6]>|</tr>", "\n", text, flags=re.I)
    text = _TAG.sub(" ", text)
    text = unescape(text).replace("\xa0", " ")
    text = _WS.sub(" ", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    return _BLANK_LINES.sub("\n\n", text).strip()


try:
    from bs4 import BeautifulSoup  # type: ignore
    _BS4_AVAILABLE = True
except ImportError:
    _BS4_AVAILABLE = False

from tools.file_ops import write_file


async def _read_through_service(url: str):
    """
    One attempt at the reader service, shaped as a `fetch_url` result.

    Returns None when it is not available or could not read the page, so the
    caller keeps whatever failure it already had. Never raises: this runs on a
    path that is already handling a failure, and a second exception there would
    replace a clear "HTTP 403" with a traceback.
    """
    try:
        from tools.render_page import render_available
        if render_available():
            return None                 # A local browser is better and first-party.
        from tools.reader_fallback import read_through_reader
        second = await read_through_reader(url)
    except Exception:                                   # noqa: BLE001
        return None
    if not second.get("success"):
        return None

    text = _to_text(second["text"], "text/plain")
    limit = settings.fetch_url_max_chars
    truncated = len(text) > limit
    return {
        "success": True,
        "url": url,
        "final_url": url,
        "status": 200,
        "content_type": "text/plain",
        "chars": len(text[:limit]),
        "total_chars": len(text),
        "offset": 0,
        "truncated": truncated,
        "text": text[:limit],
        "read_via": second.get("via", "reader"),
        "hint": (f"The site refused a direct fetch, so this was read through "
                 f"{second.get('via', 'the reader service')} instead. Treat it as "
                 f"the page's own text."),
    }

async def fetch_url(
    url: str,
    max_chars: int = 0,
    offset: int = 0,
    save_as: str = "",
    selector: str = "",
    query: str = "",
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
        query: Optional words to look for; returns only the best-matching
               passages of the page instead of a 20,000-character slice.
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
                    # A site that refuses this client is the commonest failure,
                    # commoner than a JavaScript shell: a live fare page answers
                    # AgentHQ's user agent with 403 and the reader service with
                    # 22KB of fares. So the second reader is tried here too,
                    # before an agent is told the page will not load and goes
                    # looking for a worse source.
                    salvaged = await _read_through_service(current)
                    if salvaged is not None:
                        return salvaged
                    return {
                        "success": False,
                        "url": current,
                        "status": response.status_code,
                        "error": f"Server returned HTTP {response.status_code}.",
                        "next_step": ("This page did not load. Find the same fact on "
                                      "a different site rather than reporting it as "
                                      "unavailable."),
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
                    elif is_html and not save_target.lower().endswith((".html", ".htm")):
                        # Raw HTML under a .txt/.md name reached a later step as
                        # "<!DOCTYPE html>..." and the agent failed to read it.
                        # Markup is kept only when the name asks for markup.
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

                # Judged on every word of the page, not the extract: trafilatura
                # drops boilerplate, and a short extract of a big page is not a shell.
                page_words = _strip_tags(body) if is_html else text
                if not hint and _looks_unrendered(page_words, body):
                    try:
                        from tools.render_page import render_available
                        can_render = render_available()
                    except Exception:                       # noqa: BLE001
                        can_render = False

                    # No browser here: try the reader service before giving the
                    # agent a shell. This is the only way a 512MB deployment
                    # reads a JavaScript page, and it is reached only now --
                    # after a first-party fetch came back looking unrendered --
                    # so an ordinary page is never disclosed to a third party.
                    read_via = ""
                    if not can_render:
                        from tools.reader_fallback import read_through_reader
                        second = await read_through_reader(current)
                        if second.get("success"):
                            rendered_text = _to_text(second["text"], "text/plain")
                            if len(rendered_text) > len(text):
                                text = rendered_text
                                total_chars = len(text)
                                slice_end = min(offset_val + limit, total_chars)
                                text_slice = text[offset_val:slice_end]
                                truncated = slice_end < total_chars
                                final_text = text_slice
                                read_via = second.get("via", "reader")

                    if read_via:
                        hint = (f"This page builds its content with JavaScript, so the "
                                f"text above was read through {read_via} rather than "
                                f"from the page's own HTML.")
                    else:
                        hint = _shell_hint(can_render)

                if query and query.strip():
                    passages, matched = _matching_passages(text, query)
                    q = query.strip()
                    final_text = text_slice = (
                        f"[{matched} passage(s) of this {total_chars:,}-character page "
                        f"match '{q}'; the best are shown below in page order.]\n\n{passages}"
                        if passages else
                        f"No passage on this page mentions any of '{q}'. Try other "
                        f"words, or omit query to read the page from the start.")
                    truncated = False

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
