"""
Read a JavaScript page without a browser of your own.

`render_page` needs Chromium: ~430MB on disk and 829MB resident in testing,
against a 512MB instance. So the deployed harness cannot run one, and a page
that writes its figures in with JavaScript arrives empty -- the failure that
made a mission report "no Airbnb stay could be confirmed under 2,500" when
what happened was that the price never reached it.

r.jina.ai renders the page on their machines and returns text. No key, no
install, no memory. It is strictly a fallback:

  * It is a third party, so every URL sent here is disclosed to them. Only
    reached after a first-party fetch came back looking unrendered.
  * It is slower than a direct fetch.
  * The hostile sites bounce it. Airbnb returns a tracker stub through this
    path, so the honest refusal still has to exist behind it.

Measured on a JS fare page that plain `fetch_url` could not read: 22,565 bytes
back, with the fares in them.
"""
import re
from typing import Any, Dict
from urllib.parse import urlparse

import httpx

from config import settings

# What Jina returns when it was served a redirect or a stub rather than the
# page: a title, the source URL, and almost nothing else.
_TOO_THIN = 900


def reader_url(url: str) -> str:
    """The r.jina.ai address for a page, with the scheme kept."""
    return settings.jina_reader_base + url.strip()


def looks_like_a_stub(text: str) -> bool:
    """
    Whether what came back is a page or an apology for one.

    Airbnb answers this path with a 207-byte ad-tracker probe. Returning that
    to an agent as though it were the listing is worse than returning nothing,
    because it reads like evidence.
    """
    body = (text or "").strip()
    if len(body) < _TOO_THIN:
        return True
    lowered = body.lower()
    return bool(re.search(r"\b(1x1 image|tracker probe|are you a human|"
                          r"enable javascript|access denied)\b", lowered))


async def read_through_reader(url: str) -> Dict[str, Any]:
    """
    Fetch `url` through the reader service. Never raises.

    Returns {"success": True, "text": ...} or a result saying why not, in the
    shape the rest of the tools use.
    """
    if not settings.jina_reader_enabled:
        return {"success": False, "error": "The reader fallback is disabled."}

    parsed = urlparse((url or "").strip())
    if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
        return {"success": False, "error": "Only http and https URLs can be read."}

    try:
        async with httpx.AsyncClient(
            timeout=settings.jina_reader_timeout, follow_redirects=True,
            headers={"User-Agent": settings.fetch_url_user_agent},
        ) as client:
            response = await client.get(reader_url(url))
    except Exception as exc:                            # noqa: BLE001
        return {"success": False, "error": f"{type(exc).__name__}: {str(exc)[:200]}"}

    if response.status_code != 200:
        return {"success": False,
                "error": f"The reader service answered HTTP {response.status_code}."}

    text = response.text or ""
    if looks_like_a_stub(text):
        return {"success": False, "stub": True,
                "error": ("The reader service returned a stub, not the page: this "
                          "site refuses it. The figure could not be read.")}

    return {"success": True, "text": text, "via": "r.jina.ai"}
