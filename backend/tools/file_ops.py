"""
File tools with path containment and sandboxing.

`write_file` is bound to the running mission. Before that it wrote to whatever
path the model invented, relative to the process working directory -- so an
agent's actual deliverable ended up loose in backend/ while the workspace
recorded only the agent's closing sentence about it ("Task completed after 4
execution steps.", 39 bytes, standing in for a 2.4KB report). Downstream steps
then consumed the sentence instead of the work.

The sandbox extends this: `read_file`, `write_file`, and `list_files` are jailed
to the bound MissionWorkspace directory or the repository root (read-only).
Path traversal attempts ('..'), symlink escapes, and arbitrary absolute paths
outside the sandbox boundaries are refused outright.
"""
import os
from typing import Any, Dict, List, Optional, Sequence

from config import settings  # type: ignore
from workspace import _safe_name  # type: ignore

# Set by the orchestrator for the duration of a mission.
_active_workspace = None
_active_author = None


def bind_workspace(
    workspace,
    author_id: str = "",
    author_name: str = "",
    step_id: str = ""
) -> None:
    """Point write_file at the running mission's workspace."""
    global _active_workspace, _active_author
    _active_workspace = workspace
    _active_author = (author_id, author_name, step_id) if workspace else None


# ----------------------------------------------------------------- containment helpers

def _is_contained(target_path: str, allowed_roots: Sequence[Optional[str]]) -> bool:
    """
    Check whether target_path resolves strictly within one of the allowed_roots.

    Resolves symlinks and relative path segments via os.path.realpath. Catches
    ValueError on Windows when paths cross different drive letters (e.g. C: vs D:).
    """
    try:
        real_target = os.path.realpath(os.path.abspath(target_path))
        for root in allowed_roots:
            if not root:
                continue
            try:
                real_root = os.path.realpath(os.path.abspath(root))
                if os.path.commonpath([real_root, real_target]) == real_root:
                    return True
            except (ValueError, OSError):
                continue
        return False
    except (ValueError, OSError):
        return False


def _get_read_allowed_roots() -> List[str]:
    """Roots permitted for reading: the active mission, repo root, and missions path."""
    roots = [settings.jail_root]
    if _active_workspace is not None and getattr(_active_workspace, "dir", None):
        roots.append(_active_workspace.dir)
    if getattr(settings, "missions_path", None):
        roots.append(settings.missions_path)
        roots.append(os.path.dirname(settings.missions_path))
    return roots


def _get_unbound_write_allowed_roots() -> List[str]:
    """
    Roots permitted for writing when no mission is active (e.g. bare agent or test).
    Restricted to data/missions scratch paths; writing into source directories is blocked.
    """
    roots = []
    if getattr(settings, "missions_path", None):
        roots.append(settings.missions_path)
        roots.append(os.path.dirname(settings.missions_path))
    data_dir = os.path.join(settings.jail_root, "backend", "data")
    roots.append(data_dir)
    return roots


def resolve_read_path(filepath: str) -> str:
    """
    Robustly locate an existing file across permitted sandbox roots.
    Tries workspace, CWD, repo root, and data directories.
    """
    if not filepath or not str(filepath).strip():
        return ""
    cleaned = filepath.strip().replace("\\", "/")
    if cleaned.startswith("./"):
        cleaned = cleaned[2:]

    candidates = []
    # 1. Active workspace directory
    if _active_workspace is not None and getattr(_active_workspace, "dir", None):
        candidates.append(os.path.join(_active_workspace.dir, cleaned))
    # 2. Raw path as given
    candidates.append(cleaned)
    # 3. Path from repo root
    candidates.append(os.path.join(settings.jail_root, cleaned))
    # 4. If path starts with backend/, try stripping it
    if cleaned.startswith("backend/"):
        sub = cleaned[len("backend/"):]
        candidates.append(sub)
        candidates.append(os.path.join(settings.jail_root, sub))
    # 5. If path doesn't start with backend/, try adding backend/
    else:
        candidates.append(os.path.join("backend", cleaned))
        candidates.append(os.path.join(settings.jail_root, "backend", cleaned))
    # 6. Check data/ directory for bare filename
    base = os.path.basename(cleaned)
    candidates.append(os.path.join("data", base))
    candidates.append(os.path.join(settings.jail_root, "backend", "data", base))

    for c in candidates:
        if os.path.isfile(c):
            return os.path.abspath(c)

    if _active_workspace is not None and getattr(_active_workspace, "dir", None):
        return os.path.join(_active_workspace.dir, cleaned)
    return cleaned


# ------------------------------------------------------------------ paging

# Formats `query_data` can run SQL over. A truncated read of one of these is
# the case where paging is not merely expensive but the wrong tool entirely.
_QUERYABLE = (".csv", ".tsv", ".json")

# Enough of a file to name its columns and estimate its shape for the
# truncation hint, without keeping the file.
_HEAD_CHARS = 64 * 1024


def _shape_of(content: str, filepath: str) -> str:
    """
    What a structured file looks like, for an agent deciding how to read it.

    Returned instead of nothing when a read is truncated, because the agent's
    next move depends on facts it cannot get from the first 8,000 characters:
    how many rows there are, and what the columns are called. With those it can
    write one SQL query. Without them it pages, and paging is what loses the
    other 300 records.
    """
    suffix = os.path.splitext(filepath)[1].lower()
    if suffix not in _QUERYABLE:
        return ""

    lines = content.count("\n") + 1
    if suffix in (".csv", ".tsv"):
        header = content.split("\n", 1)[0].strip()
        sep = "\t" if suffix == ".tsv" else ","
        columns = [c.strip().strip('"') for c in header.split(sep)][:20]
        return (f" It is a {suffix.lstrip('.')} file with about {lines:,} rows "
                f"and these columns: {', '.join(columns)}.")
    return f" It is a JSON file of about {lines:,} lines."


def _truncation_hint(filepath: str, content: str, end: int, total: int,
                     limit: int) -> str:
    """
    What to do about a file that did not fit, with the arithmetic done.

    "Content truncated, call again with an offset" is true and not enough. An
    agent has `max_agent_steps` turns for the whole step -- eight -- and every
    chunk it reads stays in its history for all the remaining ones. Paging a
    45,000-character file is six of those eight turns, and at the end of it the
    prompt holds all 45,000 characters anyway. The agent that does this runs
    out of budget and reports what it managed to read, which is the 65-of-365
    failure arriving by a different route.

    So the numbers are spelled out rather than left for the model to work out,
    and the alternative is named with the file's own shape attached, so the
    next call can be the right one instead of the next chunk.
    """
    remaining = total - end
    calls = max(1, -(-remaining // max(1, limit)))      # ceil
    shape = _shape_of(content, filepath)
    suffix = os.path.splitext(filepath)[1].lower()

    base = (f"Content truncated: {end:,} of {total:,} characters shown. "
            f"Reading the rest by paging would take {calls} more call(s) of "
            f"your {settings.max_agent_steps}-turn budget, and every chunk "
            f"stays in your history afterwards.")

    if suffix in _QUERYABLE:
        return (base + shape +
                f" Do not page through it. Use query_data to run SQL over the "
                f"whole file in one call -- it reads every row, returns only "
                f"what you ask for, and is exact. Page with "
                f"read_file(offset={end}) only if you genuinely need the raw "
                f"text rather than the data.")

    return (base + f" To continue anyway, call read_file with offset={end}. To "
            f"process the whole file instead, write a script and run it with "
            f"terminal -- that reads all {total:,} characters without any of "
            f"them entering your history.")


def _read_slice(path: str, offset: int, limit: int) -> tuple:
    """
    `limit` characters from `offset`, plus the file's true length, in memory
    proportional to the slice rather than to the file.

    The obvious implementation is `f.read()` and then a slice, and it was the
    implementation: returning 8,000 characters of a 50MB log meant holding
    52 million of them in RAM first. Fast enough to look fine -- 0.13s -- and
    it scales with the wrong number, so the first multi-gigabyte file an agent
    points at takes the process out.

    Streaming keeps the exact character count, which matters more than it
    sounds: `total_chars` is what the truncation hint does its arithmetic with,
    and os.path.getsize is bytes, which is a different number the moment a file
    holds one non-ASCII character.

    The head is also collected, because a truncated read of a CSV reports its
    columns and that means reading the first line whatever the offset is.
    """
    # 256KB. The peak is a transient of the decoder working on one block, so
    # it is set by this and not by the file: measured flat at ~6.4MB with 1MB
    # blocks across 6MB, 24MB and 60MB files, and a quarter of that here. Small
    # enough to be cheap, large enough that a 60MB file is still one pass.
    chunk_size = 256 * 1024
    total = 0
    collected = []
    collected_len = 0
    head = []
    head_len = 0

    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        while True:
            block = fh.read(chunk_size)
            if not block:
                break
            block_start = total
            total += len(block)

            if head_len < _HEAD_CHARS:
                head.append(block[:_HEAD_CHARS - head_len])
                head_len += len(head[-1])

            # Only the part of this block that falls inside the window is kept.
            if collected_len < limit:
                start = max(0, offset - block_start)
                if start < len(block):
                    take = block[start:start + (limit - collected_len)]
                    collected.append(take)
                    collected_len += len(take)

    return "".join(collected), total, "".join(head)


def _paged_stream(path: str, filepath: str, max_chars: int, offset: int) -> dict:
    """`_paged`, fed by `_read_slice` so the whole file is never resident."""
    limit = max_chars if max_chars and max_chars > 0 else settings.read_file_max_chars
    limit = max(1, min(int(limit), settings.read_file_max_chars))
    start = max(0, int(offset or 0))

    chunk, total, head = _read_slice(path, start, limit)

    if total and start >= total:
        return {
            "success": True, "filepath": filepath,
            "total_chars": total, "size_bytes": total,
            "offset": start, "chars_returned": 0,
            "truncated": False, "end_of_content": True, "content": "",
            "hint": (f"Offset {start} is at or past the end of {filepath}, which "
                     f"is {total} characters long. Nothing further to read."),
        }

    end = min(start + limit, total)
    truncated = end < total
    result = {
        "success": True, "filepath": filepath,
        "total_chars": total, "size_bytes": total,
        "offset": start, "chars_returned": len(chunk),
        "truncated": truncated, "content": chunk,
    }
    if truncated:
        # The head, not the slice: a CSV's columns are on line one whatever
        # offset this call asked for.
        result["next_offset"] = end
        result["hint"] = _truncation_hint(filepath, head, end, total, limit)
        result["content"] = chunk + f"\n\n[TRUNCATED] {result['hint']}"
    return result


def _paged(content: str, filepath: str, max_chars: int, offset: int) -> dict:
    """
    One slice of a file, and enough metadata to ask for the next one.

    `read_file` used to end with `content[:8000]` and a comment about context
    safety. Nothing in the reply said a cut had happened, so a 45,000-character
    page arrived as 8,000 characters that looked complete -- which is how an
    agent extracts 65 of 365 records and reports finishing. The cap is still
    there, because an agent's history is re-sent on every remaining turn of a
    step; what is new is that the reply says so and says what to do about it.

    Shared with `_read_file_unconstrained` so the kill-switch path cannot drift
    back to a silent truncation.
    """
    limit = max_chars if max_chars and max_chars > 0 else settings.read_file_max_chars
    limit = max(1, min(int(limit), settings.read_file_max_chars))
    start = max(0, int(offset or 0))

    total = len(content)

    # Asking past the end is answered, not left as an empty string. The same
    # lesson as fetch_url: an agent that reads "" decides for itself what it
    # means, and what it decides is usually that the file is empty.
    if total and start >= total:
        return {
            "success": True,
            "filepath": filepath,
            "total_chars": total,
            "size_bytes": total,
            "offset": start,
            "chars_returned": 0,
            "truncated": False,
            "end_of_content": True,
            "content": "",
            "hint": (f"Offset {start} is at or past the end of {filepath}, which is "
                     f"{total} characters long. Nothing further to read."),
        }

    end = min(start + limit, total)
    chunk = content[start:end]
    truncated = end < total

    result = {
        "success": True,
        "filepath": filepath,
        # Kept under its old name as well as the accurate one: callers and
        # tests written against `size_bytes` still work, and it always meant
        # the whole file rather than the slice.
        "total_chars": total,
        "size_bytes": total,
        "offset": start,
        "chars_returned": len(chunk),
        "truncated": truncated,
        "content": chunk,
    }
    if truncated:
        result["next_offset"] = end
        result["hint"] = _truncation_hint(filepath, content, end, total, limit)
        # Said in the content too. A hint in a sibling field is one an agent
        # can skim past; the whole failure this guards against is an agent
        # treating a partial read as a complete one.
        result["content"] = chunk + f"\n\n[TRUNCATED] {result['hint']}"
    return result


# ----------------------------------------------------------------- tools

async def read_file(filepath: str, max_chars: int = 0, offset: int = 0) -> Dict[str, Any]:
    """
    Read a text file within the sandbox jail, one slice at a time.

    `filepath` alone behaves as it always did for any file that fits: the whole
    content, under `content`. A file that does not fit now says so, in
    `truncated`, `next_offset` and `hint`, instead of arriving short and
    looking whole.
    """
    # Kill switch bypass
    if not getattr(settings, "file_ops_sandbox_enabled", True):
        return await _read_file_unconstrained(filepath, max_chars=max_chars, offset=offset)

    if not filepath or not str(filepath).strip():
        return {"success": False, "refused": True, "error": "File path cannot be empty."}

    candidate = resolve_read_path(filepath)

    allowed_roots = _get_read_allowed_roots()
    if not _is_contained(candidate, allowed_roots):
        return {
            "success": False,
            "refused": True,
            "error": f"Path '{filepath}' resolves outside the allowed sandbox jail root.",
        }

    try:
        if not os.path.exists(candidate):
            return {"success": False, "error": f"File not found: {filepath}"}

        # Streamed rather than read whole: this returns at most `max_chars`,
        # and the memory it uses should track that rather than the file.
        return _paged_stream(candidate, filepath, max_chars, offset)
    except Exception as e:
        return {"success": False, "error": str(e)}


async def write_file(filepath: str, content: str) -> Dict[str, Any]:
    """
    Write a file into the mission workspace with path containment.

    When bound to a mission, writes must resolve inside that mission's directory.
    Attempts to climb out with '..' or target absolute paths elsewhere are refused.
    When unbound (tests or bare agents), direct writes are permitted only inside
    designated mission output and data paths.
    """
    if not getattr(settings, "file_ops_sandbox_enabled", True):
        return await _write_file_unconstrained(filepath, content)

    if not filepath or not str(filepath).strip():
        return {"success": False, "refused": True, "error": "File path cannot be empty."}

    # ------------------------------------------------------------- bound workspace
    if _active_workspace is not None:
        ws_dir = getattr(_active_workspace, "dir", None)
        # Check containment against workspace directory if one is on disk
        if ws_dir:
            target = filepath if os.path.isabs(filepath) else os.path.join(ws_dir, filepath)
            if not _is_contained(target, [ws_dir]):
                return {
                    "success": False,
                    "refused": True,
                    "error": f"Path '{filepath}' resolves outside the active mission workspace.",
                }
        else:
            # Memory-only workspace (persistence disabled or unwritable): check for traversal
            # If path was absolute or contained traversal components, refuse
            normalized = filepath.replace("\\", "/")
            if os.path.isabs(filepath) or "../" in normalized or normalized.startswith(".."):
                return {
                    "success": False,
                    "refused": True,
                    "error": f"Path '{filepath}' escapes the active mission workspace.",
                }

        author_id, author_name, step_id = _active_author or ("", "", "")
        safe_filename = _safe_name(os.path.basename(filepath or "untitled.md"))
        artifact = await _active_workspace.put(
            name=safe_filename,
            content=content,
            author_id=author_id or "agent",
            author_name=author_name or "Agent",
            step_id=step_id,
        )
        return {
            "success": True,
            "filepath": artifact.name,
            "bytes_written": artifact.size_chars,
            "message": (
                f"Filed {artifact.size_chars} chars to the mission workspace "
                f"as '{artifact.name}'."
            ),
        }

    # ------------------------------------------------------------- unbound write
    # Permitted only within designated scratch / mission output directories
    allowed_roots = _get_unbound_write_allowed_roots()
    if not _is_contained(filepath, allowed_roots):
        return {
            "success": False,
            "refused": True,
            "error": f"Path '{filepath}' resolves outside allowed write directories.",
        }

    try:
        abs_path = os.path.abspath(filepath)
        os.makedirs(os.path.dirname(abs_path), exist_ok=True)
        with open(abs_path, "w", encoding="utf-8") as f:
            f.write(content)

        return {
            "success": True,
            "filepath": filepath,
            "bytes_written": len(content),
            "message": f"Successfully wrote {len(content)} bytes to {filepath}",
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


async def list_files(directory: str = ".") -> Dict[str, Any]:
    """List files in a directory within the sandbox jail."""
    if not getattr(settings, "file_ops_sandbox_enabled", True):
        return await _list_files_unconstrained(directory)

    allowed_roots = _get_read_allowed_roots()
    if not _is_contained(directory, allowed_roots):
        return {
            "success": False,
            "refused": True,
            "error": f"Directory '{directory}' resolves outside the allowed sandbox jail root.",
        }

    try:
        if not os.path.exists(directory):
            return {"success": False, "error": f"Directory not found: {directory}"}

        items = []
        for entry in os.listdir(directory):
            full_path = os.path.join(directory, entry)
            is_dir = os.path.isdir(full_path)
            items.append({
                "name": entry,
                "is_dir": is_dir,
                "size_bytes": os.path.getsize(full_path) if not is_dir else 0,
            })

        return {
            "success": True,
            "directory": directory,
            "count": len(items),
            "items": items[:50],
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


# ----------------------------------------------------------------- unconstrained fallbacks

async def _read_file_unconstrained(filepath: str, max_chars: int = 0,
                                   offset: int = 0) -> Dict[str, Any]:
    try:
        if not os.path.exists(filepath):
            return {"success": False, "error": f"File not found: {filepath}"}
        # Same slicer as the jailed path. Turning the sandbox off relaxes where
        # a file may be read from, not whether a truncation is admitted.
        return _paged_stream(filepath, filepath, max_chars, offset)
    except Exception as e:
        return {"success": False, "error": str(e)}


async def _write_file_unconstrained(filepath: str, content: str) -> Dict[str, Any]:
    try:
        os.makedirs(os.path.dirname(os.path.abspath(filepath)), exist_ok=True)
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(content)
        return {
            "success": True,
            "filepath": filepath,
            "bytes_written": len(content),
            "message": f"Successfully wrote {len(content)} bytes to {filepath}",
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


async def _list_files_unconstrained(directory: str = ".") -> Dict[str, Any]:
    try:
        if not os.path.exists(directory):
            return {"success": False, "error": f"Directory not found: {directory}"}
        items = []
        for entry in os.listdir(directory):
            full_path = os.path.join(directory, entry)
            is_dir = os.path.isdir(full_path)
            items.append({
                "name": entry,
                "is_dir": is_dir,
                "size_bytes": os.path.getsize(full_path) if not is_dir else 0,
            })
        return {
            "success": True,
            "directory": directory,
            "count": len(items),
            "items": items[:50],
        }
    except Exception as e:
        return {"success": False, "error": str(e)}
