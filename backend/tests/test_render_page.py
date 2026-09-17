"""
The optional browser.

Written after a mission read three Airbnb listings with `fetch_url`, got pages
whose price area still said "loading Reserve", and reported "no stay could be
confirmed under 2,500" -- which a reader takes as a fact about the world when
it was a fact about the instrument. The same listing, rendered, shows 2,419.

Nothing here launches a browser. What is worth pinning is the policy around it:
the tool must not exist when it cannot work, it must refuse the addresses
`fetch_url` refuses, and its absence must read as a limit of this deployment
rather than as an answer.
"""
import sys

import pytest

import tool_registry as registry_mod
from tools import render_page as rp


@pytest.mark.asyncio
async def test_an_absent_browser_refuses_rather_than_reporting_nothing_found(monkeypatch):
    """The refusal has to tell the agent what NOT to conclude. An agent told
    only 'failed' writes the fact off as unavailable in the world."""
    monkeypatch.setattr(rp, "PLAYWRIGHT_AVAILABLE", False)
    result = await rp.render_page("https://example.com/listing")

    assert result["success"] is False
    assert result["refused"] is True
    assert "do NOT report a fact as absent from the world" in result["error"]
    assert "pip install playwright" in result["error"], "say how to get the capability"


@pytest.mark.asyncio
@pytest.mark.parametrize("url", [
    "http://169.254.169.254/latest/meta-data/",      # cloud metadata
    "http://localhost:8080/api/missions",            # this server, through its own tool
    "file:///etc/passwd",
    "ftp://example.com/x",
])
async def test_the_browser_refuses_what_fetch_url_refuses(url, monkeypatch):
    """A browser pointed at the metadata service is the same SSRF hole with
    JavaScript. The check runs before anything launches."""
    monkeypatch.setattr(rp, "PLAYWRIGHT_AVAILABLE", True)
    launched = []
    monkeypatch.setattr(rp, "async_playwright", lambda: launched.append(1), raising=False)

    result = await rp.render_page(url)

    assert result["success"] is False
    assert result["refused"] is True
    assert not launched, "policy must be checked before a browser starts"


def test_the_tool_is_absent_when_no_browser_is_installed(monkeypatch):
    """
    Registered conditionally, because the catalogue is what the planner and the
    foundry read. A tool that exists and cannot work gets planned into a step
    and hired for, then fails mid-mission; an absent one makes the planner route
    the step elsewhere and the foundry say plainly that nothing here can do it.
    """
    monkeypatch.setattr(registry_mod, "render_available", lambda: False)
    empty = registry_mod.ToolRegistry()
    assert "render_page" not in empty.catalogue()

    monkeypatch.setattr(registry_mod, "render_available", lambda: True)
    stocked = registry_mod.ToolRegistry()
    assert "render_page" in stocked.catalogue()
    assert stocked.catalogue()["render_page"]["risk"] == "execute", \
        "it makes an outbound request an LLM chose"


def test_availability_needs_the_browser_not_just_the_package(monkeypatch):
    """`pip install playwright` and `playwright install chromium` are separate
    steps, and only the first one is a Python import."""
    monkeypatch.setattr(rp, "PLAYWRIGHT_AVAILABLE", False)
    assert rp.render_available() is False


def test_the_description_tells_the_agent_to_try_the_cheap_tool_first(monkeypatch):
    """Rendering is ~10x slower. An agent that reaches for it first spends the
    step budget on pages plain HTTP would have answered."""
    monkeypatch.setattr(registry_mod, "render_available", lambda: True)
    spec = registry_mod.ToolRegistry().catalogue()["render_page"]
    assert "ONLY after `fetch_url`" in spec["description"]
    assert "slower" in spec["description"]


# ------------------------------------------------- telling the two apart

def test_a_javascript_shell_is_reported_as_unread_not_as_nothing_found():
    """
    The Airbnb pages arrived whole and empty: markup present, price absent.
    `fetch_url` used to return that as an ordinary success, so the agent read a
    complete-looking page, found no figure, and wrote that no stay could be
    confirmed under the budget.
    """
    from tools.fetch_url import _looks_unrendered, _shell_hint

    shell_html = "<div>" * 4000 + "Loading…" + "</div>" * 4000
    assert _looks_unrendered("Loading…", shell_html) is True
    assert _looks_unrendered("You need to enable JavaScript to run this app.", "<html>") is True

    prose = "The cheapest sleeper fare from Pune to Goa is 690 rupees per person. " * 40
    assert _looks_unrendered(prose, "<html>" + prose + "</html>") is False

    with_browser = _shell_hint(True)
    assert "render_page" in with_browser

    without = _shell_hint(False)
    assert "do NOT report the figure as unavailable in the world" in without
    assert "render_page" not in without, "do not name a tool this deployment lacks"
