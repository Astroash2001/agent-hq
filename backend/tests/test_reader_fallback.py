"""
Reading a JavaScript page on a machine with no browser.

`render_page` needs Chromium -- 829MB resident in testing against a 512MB
instance -- so the deployment cannot have one, and a page that writes its
figures in with JavaScript arrives empty there. That is the failure that made a
mission report "no Airbnb stay could be confirmed under 2,500" when the price
had simply never reached it.

The reader service renders the page elsewhere and returns text. These pin the
three things that make it safe to rely on: it is never the first call, a stub
is not mistaken for a page, and a site that refuses it still ends in an honest
refusal rather than an empty success.
"""
import pytest

from config import settings
from tools import reader_fallback as rf


def test_a_tracker_stub_is_not_mistaken_for_a_page():
    """
    Airbnb answers this path with a 207-byte ad-tracker probe. Handing that to
    an agent as the listing is worse than handing back nothing, because it
    reads like evidence.
    """
    stub = ("Title: https://match.adsrvr.org/track/cmf/google\n\n"
            "URL Source: https://www.airbnb.com/rooms/40594183\n\n"
            "Markdown Content:\nA 1x1 image, likely be a tacker probe")
    assert rf.looks_like_a_stub(stub) is True
    assert rf.looks_like_a_stub("") is True
    assert rf.looks_like_a_stub("Please enable JavaScript to continue. " * 40) is True

    real = "Pune to Goa bus fares start at 500 rupees for a non-AC sleeper. " * 30
    assert rf.looks_like_a_stub(real) is False


@pytest.mark.asyncio
async def test_a_refusing_site_ends_in_a_refusal_not_an_empty_success(monkeypatch):
    async def _stub(url):
        return {"success": False, "stub": True,
                "error": "The reader service returned a stub, not the page: this "
                         "site refuses it. The figure could not be read."}

    monkeypatch.setattr(rf, "read_through_reader", _stub)
    result = await rf.read_through_reader("https://www.airbnb.com/rooms/1")

    assert result["success"] is False
    assert "could not be read" in result["error"]


@pytest.mark.asyncio
async def test_it_can_be_switched_off_entirely(monkeypatch):
    """Every URL sent here is disclosed to a third party. A deployment that
    does not want that must be able to say so."""
    monkeypatch.setattr(settings, "jina_reader_enabled", False)
    result = await rf.read_through_reader("https://example.com/page")

    assert result["success"] is False
    assert "disabled" in result["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://example.com/x", "not a url"])
async def test_only_http_urls_are_sent_to_the_reader(url, monkeypatch):
    monkeypatch.setattr(settings, "jina_reader_enabled", True)
    result = await rf.read_through_reader(url)
    assert result["success"] is False
    assert "http" in result["error"]


def test_the_reader_address_keeps_the_original_url(monkeypatch):
    monkeypatch.setattr(settings, "jina_reader_base", "https://r.jina.ai/")
    built = rf.reader_url("https://www.abhibus.com/buses/55/Pune-Goa")
    assert built == "https://r.jina.ai/https://www.abhibus.com/buses/55/Pune-Goa"


@pytest.mark.asyncio
async def test_the_reader_is_not_tried_when_a_browser_is_installed(monkeypatch):
    """
    Order matters for two reasons: a local browser is first-party and does not
    disclose the URL, and it reads the sites the service cannot.
    """
    import tools.fetch_url as fu

    called = []

    async def _never(url):
        called.append(url)
        return {"success": True, "text": "x" * 5000, "via": "r.jina.ai"}

    monkeypatch.setattr(rf, "read_through_reader", _never)
    monkeypatch.setattr("tools.render_page.render_available", lambda: True)

    # A shell page, with a browser available: the hint names render_page and
    # the service is never contacted.
    hint = fu._shell_hint(True)
    assert "render_page" in hint
    assert called == []


@pytest.mark.asyncio
async def test_a_site_that_refuses_this_client_is_salvaged(monkeypatch):
    """
    The commonest failure is not a JavaScript shell, it is a refusal: a live
    fare page answers AgentHQ's user agent with HTTP 403 and the reader service
    with 22,463 characters of fares. Before this the agent was told the page
    would not load and went looking for a worse source.
    """
    import tools.fetch_url as fu

    monkeypatch.setattr("tools.render_page.render_available", lambda: False)

    async def _reads(url):
        return {"success": True, "via": "r.jina.ai",
                "text": "Pune to Goa sleeper from 1,070 rupees. " * 40}

    monkeypatch.setattr("tools.reader_fallback.read_through_reader", _reads)
    result = await fu._read_through_service("https://www.abhibus.com/buses/55/Pune-Goa")

    assert result is not None
    assert result["success"] is True
    assert result["read_via"] == "r.jina.ai"
    assert "1,070" in result["text"]
    assert "refused a direct fetch" in result["hint"], \
        "the agent must know this text did not come from the site directly"


@pytest.mark.asyncio
async def test_a_local_browser_wins_over_the_service(monkeypatch):
    """First-party and better: it discloses no URL and reads sites the service
    cannot."""
    import tools.fetch_url as fu

    called = []

    async def _never(url):
        called.append(url)
        return {"success": True, "text": "x" * 4000, "via": "r.jina.ai"}

    monkeypatch.setattr("tools.render_page.render_available", lambda: True)
    monkeypatch.setattr("tools.reader_fallback.read_through_reader", _never)

    assert await fu._read_through_service("https://example.com") is None
    assert called == [], "the service must not be contacted when a browser exists"
