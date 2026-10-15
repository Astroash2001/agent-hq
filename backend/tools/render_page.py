"""
Read a page the way a browser reads it.

`fetch_url` downloads bytes. A site that builds its page in JavaScript sends
bytes that do not contain the answer: a recorded mission opened three Airbnb
listings, and each page's price area was still the string "loading Reserve"
when the HTML arrived. The agent reported "no stay could be confirmed under
2,500", which a reader takes as "none exist" when what happened is that the
instrument could not see. The same listing, rendered, shows 2,419.

So this tool runs the page. It is deliberately a SECOND tool rather than a flag
on `fetch_url`:

  * It is optional. Playwright and a Chromium build are ~540MB on disk and
    peaked at 829MB resident in testing, which does not fit a 512MB instance.
    Without them installed the tool is not registered at all, and the planner
    cannot route a step to it -- the foundry already refuses to hire for a tool
    the catalogue does not have, which is a better failure than a browser that
    dies on OOM halfway through a mission.
  * It is slower by an order of magnitude. A tool an agent reaches for only
    when the cheap one came back empty is worth the separation.

The launch flags come from reading crawl4ai's browser_manager, which is what
this was tested against. The one that matters is
`--disable-blink-features=AutomationControlled`; with the full Chromium build
it returns real prices, and with the smaller `chromium-headless-shell` the same
code gets an empty shell back. The stealth fork (patchright) turned out not to
be needed, so it is not a dependency.
"""
import asyncio
import os
from typing import Any, Dict

from config import settings
from tools.fetch_url import _check_url, _strip_tags

# Read once, at import: an agent cannot turn the browser on by asking for it.
try:                                                    # pragma: no cover
    from playwright.async_api import async_playwright   # type: ignore
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False

# From crawl4ai's browser_manager.py. Everything here either disables a feature
# a headless render does not need or removes a signal that says "automated".
LAUNCH_ARGS = [
    "--disable-gpu",
    "--disable-gpu-compositing",
    "--disable-software-rasterizer",
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-infobars",
    "--disable-blink-features=AutomationControlled",
    "--disable-renderer-backgrounding",
    "--disable-ipc-flooding-protection",
    "--disable-background-timer-throttling",
    "--disable-component-update",
    "--force-color-profile=srgb",
    "--mute-audio",
    "--window-size=1280,900",
]

MISSING = (
    "This deployment has no browser, so a JavaScript page cannot be rendered. "
    "Report what you could and could not read; do NOT report a fact as absent "
    "from the world when it was only absent from the page you received. "
    "Install with: pip install playwright && python -m playwright install chromium"
)


def _settle_seconds() -> float:
    """How long to let a rendered page finish its own requests."""
    return max(0.0, float(getattr(settings, "render_settle_sec", 10.0)))


async def render_page(url: str, wait_for: str = "", max_chars: int = 0) -> Dict[str, Any]:
    """
    Load one URL in a real browser, run its JavaScript, and return the text.

    Use this only when `fetch_url` returned a page whose figures were missing
    because the site fills them in after loading. It is much slower.

    Parameters:
        url: Full http/https URL to render.
        wait_for: Optional CSS selector to wait for before reading the page.
        max_chars: Maximum characters to return (capped at the config limit).
    """
    if not PLAYWRIGHT_AVAILABLE:
        return {"success": False, "refused": True, "error": MISSING}

    # The same policy `fetch_url` enforces. A browser that an agent points at
    # http://169.254.169.254/ is the identical SSRF hole, with JavaScript.
    refusal = _check_url(url)
    if refusal:
        return refusal

    limit = max_chars if max_chars > 0 else settings.fetch_url_max_chars
    limit = min(limit, settings.fetch_url_max_chars)
    timeout_ms = int(getattr(settings, "render_timeout_sec", 90.0) * 1000)

    browser = None
    try:
        async with async_playwright() as driver:
            browser = await driver.chromium.launch(args=LAUNCH_ARGS)
            page = await browser.new_page(viewport={"width": 1280, "height": 900})

            # Every navigation, including the ones the page starts itself, is
            # checked: a redirect into private address space is the hole the
            # first-hop check alone would leave open.
            async def _gate(route, request):
                if _check_url(request.url):
                    await route.abort()
                else:
                    await route.continue_()

            await page.route("**/*", _gate)
            await page.goto(url.strip(), wait_until="domcontentloaded", timeout=timeout_ms)

            if wait_for:
                try:
                    await page.wait_for_selector(wait_for, timeout=timeout_ms)
                except Exception:                       # noqa: BLE001
                    pass        # Reported below as "what was read", not as a crash.
            else:
                # The figure usually arrives in a request made after first paint.
                await asyncio.sleep(_settle_seconds())
                await page.mouse.wheel(0, 2400)
                await asyncio.sleep(2)

            html = await page.content()
            # Whole page, not a main-content extract: this tool exists to read
            # figures like a price in a booking widget, which an extractor drops.
            text = _strip_tags(html)
            title = await page.title()
            final_url = page.url
    except Exception as exc:                            # noqa: BLE001
        return {"success": False,
                "error": f"{type(exc).__name__}: {str(exc)[:300]}",
                "next_step": ("The page did not render. Try `fetch_url` on it, or "
                              "find the same fact on a different site. Do not "
                              "report the fact as unavailable in the world.")}
    finally:
        if browser is not None:
            try:
                await browser.close()
            except Exception:                           # noqa: BLE001
                pass

    truncated = len(text) > limit
    return {
        "success": True,
        "url": final_url,
        "title": title,
        "text": text[:limit],
        "total_chars": len(text),
        "truncated": truncated,
        "rendered": True,
        **({"hint": f"Showing {limit:,} of {len(text):,} characters."} if truncated else {}),
    }


def render_available() -> bool:
    """
    Whether this machine can actually render a page.

    Both halves are needed and they are installed separately: `pip install
    playwright` brings the package, `playwright install chromium` brings the
    ~430MB browser. Registering on the package alone gives the planner a tool
    that fails at launch time, which is the failure this whole module is
    written to avoid.
    """
    if not PLAYWRIGHT_AVAILABLE:
        return False
    root = os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or os.path.join(
        os.path.expanduser("~"), "AppData", "Local", "ms-playwright")
    if os.path.isdir(root):
        return any(name.startswith("chromium") for name in os.listdir(root))
    # Linux and macOS default caches.
    for path in (os.path.expanduser("~/.cache/ms-playwright"),
                 os.path.expanduser("~/Library/Caches/ms-playwright")):
        if os.path.isdir(path):
            return any(name.startswith("chromium") for name in os.listdir(path))
    return False
