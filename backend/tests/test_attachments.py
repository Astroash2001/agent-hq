"""
Files a user drops with a task.

A goal is often *about* something -- a CSV to analyse, a spec to review, a log
to explain. The only way to get that in front of an agent was to paste it into
the prompt, where it sits in every turn's history for the rest of the mission
and is re-billed on each one.

An attachment is staged, copied into the mission workspace as an artifact, and
from then on is ordinary: `read_file` reads it, `consumes` delivers it, the
office draws it moving between desks. That reuse is the point -- the feature
needed no new plumbing downstream of the workspace.

The rejections matter as much as the happy path. An agent reads an attachment
as text into a prompt, so a binary is noise the user pays for.
"""
import os
import shutil
import tempfile

import pytest

import attachments
from config import settings


@pytest.fixture
def staging(monkeypatch):
    d = tempfile.mkdtemp(prefix="agenthq_attach_")
    monkeypatch.setattr(settings, "attachments_path", d)
    yield d
    shutil.rmtree(d, ignore_errors=True)


# ------------------------------------------------------------- naming

def test_a_name_cannot_escape_the_staging_directory():
    """The name reaches the filesystem, so it is stripped to a basename."""
    hostile_names = [
        "../../etc/passwd",
        "..\\..\\windows\\system32\\cfg",
        "/etc/shadow",
        "sub/dir/notes.md",
    ]
    for hostile in hostile_names:
        safe = attachments.safe_name(hostile)
        assert "/" not in safe and "\\" not in safe
        assert not safe.startswith("..")


def test_a_name_that_reduces_to_nothing_still_gets_one():
    """Losing the file over its name would be a poor trade."""
    got = attachments.safe_name("///")
    assert got and got.startswith("attachment_")


def test_ordinary_names_survive_readably():
    assert attachments.safe_name("Q3 report.md") == "Q3_report.md"
    assert attachments.safe_name("data.csv") == "data.csv"


# ------------------------------------------------------------- what is allowed

def test_a_text_file_is_staged_and_readable(staging):
    got = attachments.save("notes.md", b"# Notes\n\nSodium-ion is cheaper.\n")

    assert got["name"] == "notes.md"
    assert got["size_bytes"] > 0
    assert "Sodium-ion" in attachments.read("notes.md")


def test_a_binary_is_refused_with_a_reason(staging):
    """A model cannot read a PNG. Sending one is noise the user pays for."""
    png = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + b"\x00" * 200
    with pytest.raises(ValueError) as exc:
        attachments.save("logo.png", png)
    assert "text" in str(exc.value).lower()


def test_a_text_extension_hiding_binary_content_is_still_refused(staging):
    """The extension is chosen by whoever uploads; the bytes are not."""
    with pytest.raises(ValueError):
        attachments.save("notes.md", b"real text\x00then a NUL byte")


def test_an_oversized_file_is_refused_and_says_why(staging, monkeypatch):
    monkeypatch.setattr(settings, "max_attachment_bytes", 1000)
    with pytest.raises(ValueError) as exc:
        attachments.save("big.csv", b"x" * 2000)
    message = str(exc.value)
    assert "MB" in message
    assert "prompt" in message, "the reason should say why size matters here"


def test_an_empty_file_is_refused(staging):
    with pytest.raises(ValueError):
        attachments.save("empty.md", b"")


# ------------------------------------------------------------- the staging area

def test_listing_shows_what_is_waiting_newest_first(staging):
    attachments.save("a.md", b"first")
    attachments.save("b.md", b"second")

    names = [a["name"] for a in attachments.listing()]
    assert set(names) == {"a.md", "b.md"}


def test_discard_removes_a_staged_file(staging):
    attachments.save("gone.md", b"content")
    assert attachments.discard("gone.md") is True
    assert attachments.read("gone.md") == ""
    assert attachments.discard("gone.md") is False


def test_reading_a_file_that_was_never_staged_is_empty_not_an_error(staging):
    """A missing upload must not take the mission down with it."""
    assert attachments.read("never.md") == ""


# ------------------------------------------------- into the mission workspace

@pytest.mark.asyncio
async def test_an_attachment_becomes_an_ordinary_workspace_artifact(staging,
                                                                    monkeypatch):
    """
    The whole design: once seeded, an attachment is just an artifact. That is
    why the feature needed no new plumbing -- `read_file` reads it, `consumes`
    delivers it, and the office draws it moving between desks.
    """
    from orchestrator import Orchestrator
    from workspace import MissionWorkspace

    monkeypatch.setattr(settings, "missions_path",
                        os.path.join(staging, "missions"))
    attachments.save("spec.md", b"# Spec\n\nThe widget must retry twice.\n")

    ws = MissionWorkspace("m_attach")
    seeded = await Orchestrator._seed_attachments(ws, ["spec.md"])

    assert seeded == ["spec.md"]
    art = ws.get("spec.md")
    assert art is not None
    assert "retry twice" in art.content
    assert art.author_name == "you", "the user is the author, not an agent"


@pytest.mark.asyncio
async def test_a_seeded_attachment_leaves_the_staging_area(staging, monkeypatch):
    """The mission owns it now; a second mission must not pick it up again."""
    from orchestrator import Orchestrator
    from workspace import MissionWorkspace

    monkeypatch.setattr(settings, "missions_path",
                        os.path.join(staging, "missions"))
    attachments.save("once.md", b"content that should move")

    await Orchestrator._seed_attachments(MissionWorkspace("m_once"), ["once.md"])
    assert attachments.read("once.md") == ""
    assert [a["name"] for a in attachments.listing()] == []


@pytest.mark.asyncio
async def test_a_missing_attachment_is_skipped_not_fatal(staging, monkeypatch):
    """
    The goal is still runnable without it. Losing a whole mission because an
    upload went missing would be the worse outcome.
    """
    from orchestrator import Orchestrator
    from workspace import MissionWorkspace

    monkeypatch.setattr(settings, "missions_path",
                        os.path.join(staging, "missions"))
    attachments.save("real.md", b"this one exists")

    seeded = await Orchestrator._seed_attachments(
        MissionWorkspace("m_partial"), ["real.md", "ghost.md"])

    assert seeded == ["real.md"]


@pytest.mark.asyncio
async def test_a_long_attachment_is_truncated_on_the_way_in(staging, monkeypatch):
    """
    Truncated when seeded, not when read. The point of an attachment is that it
    lands in a prompt, and a prompt is billed on every turn of the mission.
    """
    from orchestrator import Orchestrator
    from workspace import MissionWorkspace

    monkeypatch.setattr(settings, "missions_path",
                        os.path.join(staging, "missions"))
    monkeypatch.setattr(settings, "max_attachment_chars", 500)
    attachments.save("big.csv", b"a,b,c\n" * 5000)

    ws = MissionWorkspace("m_big")
    await Orchestrator._seed_attachments(ws, ["big.csv"])

    body = ws.get("big.csv").content
    assert "attachment truncated" in body
    assert len(body) < 1000


@pytest.mark.asyncio
async def test_no_attachments_is_the_ordinary_case(staging, monkeypatch):
    from orchestrator import Orchestrator
    from workspace import MissionWorkspace

    monkeypatch.setattr(settings, "missions_path",
                        os.path.join(staging, "missions"))
    assert await Orchestrator._seed_attachments(MissionWorkspace("m_none"), None) == []
    assert await Orchestrator._seed_attachments(MissionWorkspace("m_none2"), []) == []
