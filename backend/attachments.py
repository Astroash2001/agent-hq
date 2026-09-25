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

Text is still what reaches a prompt, and that has not changed -- an agent reads
an attachment into its history, and a 2MB binary would be half a million tokens
of noise. What changed is that two binary formats are now accepted at the door
and converted to something an agent can actually use:

  **PDF** is extracted to text here, at staging time, and staged as the `.md`
  it becomes. Nothing downstream learns a new trick: `read()` returns text, the
  mission seeds it like any other artifact, and `read_file` reads it. The
  binary is not kept, because nothing in the office can do anything with a PDF
  that is not its text -- keeping it would mean two files in the tray where the
  user attached one.

  **Images** are kept as bytes, because the one tool that can read them --
  `describe_image` -- needs the actual pixels. They are the exception to "text
  only", and they earn it by having a reader.

Everything else binary is still rejected with a message saying so, rather than
silently mangled into replacement characters.
"""
import io
import logging
import os
import time
from typing import Any, Dict, List

import sessions
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

# Formats that are not text but have a reader in the office. Kept apart from
# TEXT_SUFFIXES because they take a different path through `save`: a PDF is
# converted, an image is stored as bytes.
PDF_SUFFIXES = {".pdf"}

# Raster formats the vision models accept. No .gif or .bmp: the first is
# usually an animation and the second is uncompressed enough to blow the size
# limit on a screenshot.
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}

# A file with no suffix at all is usually a README or a config; allowed, since
# the content check below is what actually decides.
_MAX_NAME = 120

# Every PDF starts with this. Checked on the bytes rather than the extension,
# for the same reason `is_probably_text` is: the extension is chosen by
# whoever uploads.
_PDF_MAGIC = b"%PDF-"


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


def extract_pdf_text(raw: bytes) -> Dict[str, Any]:
    """
    A PDF's text, page by page, or a readable reason there is none.

    Pages are numbered in the output because that is what a citation needs: an
    agent asked where a figure came from can say "page 7" instead of quoting a
    sentence and hoping the reader finds it.

    A PDF with no extractable text is the common failure and it is not an
    error in the file -- it is a scan, a photograph of paper. Saying that
    plainly is worth more than an empty artifact, because an empty artifact
    reads as "this document says nothing".
    """
    try:
        from pypdf import PdfReader
    except ImportError:
        return {"ok": False, "reason": (
            "PDF support needs the `pypdf` package, which is not installed. "
            "Run: pip install -r requirements.txt")}

    try:
        reader = PdfReader(io.BytesIO(raw))
    except Exception as exc:                                   # noqa: BLE001
        return {"ok": False, "reason": f"that PDF could not be opened ({exc})"}

    if getattr(reader, "is_encrypted", False):
        # An empty-password decrypt covers the common "protected but not
        # really" case; a real password is not something to guess at.
        try:
            if reader.decrypt("") == 0:
                return {"ok": False, "reason": (
                    "that PDF is password-protected. Remove the password and "
                    "attach it again.")}
        except Exception:                                      # noqa: BLE001
            return {"ok": False, "reason": "that PDF is password-protected."}

    pages: List[str] = []
    for number, page in enumerate(reader.pages, start=1):
        try:
            body = (page.extract_text() or "").strip()
        except Exception as exc:                               # noqa: BLE001
            logger.warning("page %d of a PDF would not extract: %s", number, exc)
            body = ""
        if body:
            pages.append(f"## Page {number}\n\n{body}")

    if not pages:
        return {"ok": False, "reason": (
            f"no text could be extracted from that PDF ({len(reader.pages)} "
            f"page(s)). It is most likely a scan rather than a text document; "
            f"an OCR pass would be needed, which this harness does not do.")}

    return {"ok": True, "text": "\n\n".join(pages),
            "pages": len(reader.pages), "pages_with_text": len(pages)}


def staging_dir() -> str:
    """
    The caller's own tray: a folder named after its session.

    Per session so one browser cannot list, delete or attach another's staged
    file by name. `sessions.valid` limits the id to [A-Za-z0-9_-], so it is
    safe as a path component. A header-less caller uses the root, as before.
    """
    session_id = sessions.current()
    path = settings.attachments_path
    if session_id:
        path = os.path.join(path, session_id)
    os.makedirs(path, exist_ok=True)
    return path


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

    # A PDF is converted at the door and staged as the text it becomes, so
    # nothing downstream has to know a PDF was ever involved.
    if suffix in PDF_SUFFIXES:
        if not raw.startswith(_PDF_MAGIC):
            raise ValueError(
                "that file is named .pdf but does not look like one. Attach "
                "the real file, or rename it to its actual format.")
        extracted = extract_pdf_text(raw)
        if not extracted["ok"]:
            raise ValueError(extracted["reason"])

        stem = os.path.splitext(name)[0] or "document"
        text_name = f"{stem}.md"
        header = (f"# {stem}\n\n*Extracted from {name} "
                  f"({extracted['pages']} page(s)).*\n\n")
        body = header + extracted["text"]

        path = os.path.join(staging_dir(), text_name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(body)
        logger.info("staged %s as %s (%d pages, %d chars)",
                    name, text_name, extracted["pages"], len(body))
        return {
            "name": text_name,
            "size_bytes": len(body.encode("utf-8")),
            "kind": "pdf",
            "source_name": name,
            "pages": extracted["pages"],
            "pages_with_text": extracted["pages_with_text"],
            "note": (f"{name} was converted to {text_name} "
                     f"({extracted['pages_with_text']} of {extracted['pages']} "
                     f"page(s) had text)."),
        }

    # An image is kept as bytes. It is the one binary the office can read,
    # through describe_image, and that tool needs the pixels.
    if suffix in IMAGE_SUFFIXES:
        path = os.path.join(staging_dir(), name)
        with open(path, "wb") as fh:
            fh.write(raw)
        logger.info("staged image %s (%d bytes)", name, len(raw))
        return {"name": name, "size_bytes": len(raw), "kind": "image",
                "note": (f"{name} is an image. Use the describe_image tool to "
                         f"read it; it cannot be read as text.")}

    if suffix and suffix not in TEXT_SUFFIXES:
        raise ValueError(
            f"'{suffix}' is not a format an agent can read. Attach text "
            f"(csv, json, md, or code), a PDF, or an image (png/jpg/webp).")
    if not is_probably_text(raw):
        raise ValueError(
            "that file is not readable as UTF-8 text. Agents read an "
            "attachment as text; a binary would arrive as noise.")

    path = os.path.join(staging_dir(), name)
    with open(path, "wb") as fh:
        fh.write(raw)
    logger.info("staged attachment %s (%d bytes)", name, len(raw))
    return {"name": name, "size_bytes": len(raw), "kind": "text"}


def is_image(name: str) -> bool:
    """Whether this staged file is one of the formats kept as bytes."""
    return os.path.splitext(safe_name(name))[1].lower() in IMAGE_SUFFIXES


def path_of(name: str) -> str:
    """Where a staged file sits, or "" if it is not there."""
    path = os.path.join(staging_dir(), safe_name(name))
    return path if os.path.isfile(path) else ""


def read(name: str) -> str:
    """
    One staged file's text, or "" if it is not there.

    An image returns "" rather than its bytes decoded as characters. The caller
    that seeds a mission checks `is_image` first; anything else asking for an
    image's text is asking the wrong question, and "" is the answer that makes
    it skip rather than file a page of mojibake.
    """
    path = path_of(name)
    if not path or is_image(name):
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
