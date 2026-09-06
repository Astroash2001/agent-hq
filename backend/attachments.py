"""
Files a user drops alongside a task.

A goal is often *about* something -- a CSV to analyse, a spec to review, a log
to explain. Until now the only way to get that in front of an agent was to
paste it into the prompt, which puts it in every turn's history for the rest of
the mission and costs accordingly.

An attachment is staged here first, then copied into the mission workspace at
dispatch and handed to the first step the way any upstream artifact is. From
that point it is an ordinary artifact: `read_file` reads it, `consumes` delivers
it, and the office draws it moving between desks.

Staged before the mission exists because that is the order the browser does
things in -- a file is chosen before the goal is submitted, and a mission id is
not minted until dispatch. A browser that uploads and then never submits leaves
rubbish in the staging directory rather than in a half-made mission.

Text only, deliberately. An agent reads an attachment into its prompt, and a
prompt is what costs money: a model that cannot see an image gains nothing from
being sent one, and a 2MB binary is half a million tokens of noise. Binary
formats are rejected with a message saying so rather than silently mangled into
replacement characters.
"""
import logging
import os
import time
from typing import Any, Dict, List

from config import settings

logger = logging.getLogger("AgentHQ.Attachments")

# Extensions an agent can actually read. Everything an LLM does with an
# attachment is read it as text, so this is the honest list rather than a
# permissive one.
TEXT_SUFFIXES = {
    ".md", ".txt", ".csv", ".tsv", ".json", ".yaml", ".yml", ".log",
    ".py", ".js", ".ts", ".html", ".css", ".sql", ".sh", ".ini", ".toml",
    ".xml", ".rst", ".env", ".cfg", ".conf",
}

# A file with no suffix at all is usually a README or a config; allowed, since
# the content check below is what actually decides.
_MAX_NAME = 120


def safe_name(name: str) -> str:
    """
    A filename that cannot leave the staging directory.

    Only the basename survives, and only characters that mean nothing to a
    filesystem. A name that reduces to nothing gets a generated one rather than
    being rejected -- losing the file over its name would be a poor trade.
    """
    base = os.path.basename((name or "").replace("\\", "/")).strip()
    cleaned = "".join(c for c in base if c.isalnum() or c in "._- ")
    cleaned = cleaned.strip(". ").replace(" ", "_")[:_MAX_NAME]
    return cleaned or f"attachment_{int(time.time())}.txt"


def is_probably_text(raw: bytes) -> bool:
    """
    Whether this is text an agent could read.

    A NUL byte is the giveaway for a binary format, and a decode failure is the
    other. Checked on the bytes rather than trusting the extension, because the
    extension is chosen by whoever uploads.
    """
    if b"\x00" in raw[:8192]:
        return False
    try:
        raw.decode("utf-8")
        return True
    except UnicodeDecodeError:
        return False


def staging_dir() -> str:
    os.makedirs(settings.attachments_path, exist_ok=True)
    return settings.attachments_path


def save(filename: str, raw: bytes) -> Dict[str, Any]:
    """
    Stage one uploaded file. Raises ValueError with a readable reason.

    The reasons are returned to the browser verbatim, so they are written for
    someone who just dragged a file in and wants to know why it bounced.
    """
    if not raw:
        raise ValueError("that file is empty")
    if len(raw) > settings.max_attachment_bytes:
        raise ValueError(
            f"that file is {len(raw) / 1024 / 1024:.1f}MB; the limit is "
            f"{settings.max_attachment_bytes / 1024 / 1024:.0f}MB. An agent "
            f"reads an attachment into its prompt, so a large one costs real "
            f"money on every turn of the mission.")

    name = safe_name(filename)
    suffix = os.path.splitext(name)[1].lower()
    if suffix and suffix not in TEXT_SUFFIXES:
        raise ValueError(
            f"'{suffix}' is not a text format an agent can read. Attach the "
            f"data as text -- csv, json, md, or code.")
    if not is_probably_text(raw):
        raise ValueError(
            "that file is not readable as UTF-8 text. Agents read an "
            "attachment as text; a binary would arrive as noise.")

    path = os.path.join(staging_dir(), name)
    with open(path, "wb") as fh:
        fh.write(raw)
    logger.info("staged attachment %s (%d bytes)", name, len(raw))
    return {"name": name, "size_bytes": len(raw)}


def read(name: str) -> str:
    """One staged file's text, or "" if it is not there."""
    path = os.path.join(staging_dir(), safe_name(name))
    if not os.path.isfile(path):
        return ""
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return fh.read()


def listing() -> List[Dict[str, Any]]:
    """Everything currently staged, newest first."""
    d = staging_dir()
    out = []
    for name in os.listdir(d):
        path = os.path.join(d, name)
        if os.path.isfile(path):
            out.append({"name": name,
                        "size_bytes": os.path.getsize(path),
                        "staged_at": os.path.getmtime(path)})
    out.sort(key=lambda a: a["staged_at"], reverse=True)
    return out


def discard(name: str) -> bool:
    """Remove one staged file. Used when a mission has taken its copy."""
    path = os.path.join(staging_dir(), safe_name(name))
    if os.path.isfile(path):
        os.remove(path)
        return True
    return False
