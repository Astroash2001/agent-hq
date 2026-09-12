"""
Find a string across the mission's files, without reading them all.

An agent late in a mission wants one fact out of four artifacts it did not
write. Its only route was `list_files` then `read_file` on each, which puts
every artifact in full into a context that `base_agent` re-sends on every
remaining turn of the step -- the same compounding cost `web_search`'s search
cap was added to stop. Four 3KB reports read to find one sentence is 12KB paid
for on every subsequent turn.

This returns matching lines with their file and line number, so the agent can
read one file deliberately, or often just cite the line.

Containment is `file_ops`'s, reused: the same `_is_contained` that `read_file`
is tested on, over the same roots. A search tool that could read outside the
jail would be a way round `read_file`'s containment rather than a tool.
"""
import logging
import os
import re
from typing import Any, Dict, List

from config import settings  # type: ignore
from tools.file_ops import _get_read_allowed_roots, _is_contained  # type: ignore

logger = logging.getLogger("AgentHQ.SearchWorkspace")

# Text this tool will open. A search that walked into a .png would return a
# screenful of mojibake and cost a turn to recover from.
TEXT_EXTENSIONS = (
    ".md", ".txt", ".json", ".csv", ".tsv", ".yaml", ".yml",
    ".py", ".js", ".html", ".css", ".log", ".ini", ".cfg", ".toml", "",
)

_SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv", ".pytest_cache"}


async def search_workspace(
    pattern: str,
    directory: str = "",
    regex: bool = False,
    max_results: int = 0,
) -> Dict[str, Any]:
    """
    Search files under `directory` for `pattern`.

    Plain substring by default, case-insensitive. `regex=True` treats the
    pattern as one. Never raises.
    """
    needle = (pattern or "").strip()
    if not needle:
        return {"success": False, "refused": True, "error": "No search pattern given."}

    root = (directory or "").strip()
    if not root:
        # The running mission's own directory, which is what "the workspace"
        # means to an agent mid-step. Falls back to the missions root so the
        # tool still works when nothing is bound (a bare agent, or a test).
        from tools import file_ops
        active = getattr(file_ops, "_active_workspace", None)
        root = getattr(active, "dir", None) or settings.missions_path

    if getattr(settings, "file_ops_sandbox_enabled", True):
        if not _is_contained(root, _get_read_allowed_roots()):
            return {"success": False, "refused": True,
                    "error": f"'{root}' is outside the workspace and the repository."}

    if not os.path.isdir(root):
        return {"success": False, "error": f"Not a directory: {root}"}

    if regex:
        try:
            matcher = re.compile(needle, re.I)
        except re.error as exc:
            return {"success": False, "refused": True,
                    "error": f"Invalid regular expression: {exc}"}
    else:
        matcher = re.compile(re.escape(needle), re.I)

    limit = max_results if max_results and max_results > 0 else settings.search_workspace_max_results
    limit = min(limit, settings.search_workspace_max_results)

    matches: List[Dict[str, Any]] = []
    files_searched = 0
    truncated = False

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]
        for filename in sorted(filenames):
            if os.path.splitext(filename)[1].lower() not in TEXT_EXTENSIONS:
                continue
            path = os.path.join(dirpath, filename)
            try:
                if os.path.getsize(path) > settings.search_workspace_max_file_bytes:
                    continue
                with open(path, "r", encoding="utf-8", errors="replace") as fh:
                    lines = fh.readlines()
            except OSError:
                continue

            files_searched += 1
            for number, line in enumerate(lines, start=1):
                if not matcher.search(line):
                    continue
                if len(matches) >= limit:
                    truncated = True
                    break
                text = line.strip()
                matches.append({
                    "file": os.path.relpath(path, root).replace("\\", "/"),
                    "line": number,
                    "text": text[:300] + ("..." if len(text) > 300 else ""),
                })
            if truncated:
                break
        if truncated:
            break

    return {
        "success": True,
        "pattern": needle,
        "root": root,
        "files_searched": files_searched,
        "match_count": len(matches),
        "truncated": truncated,
        # Said plainly rather than left as an empty list. "No file contains
        # this" is a finding an agent should act on -- and one it will
        # otherwise narrate around.
        "summary": (f"{len(matches)} match(es) in {files_searched} file(s)."
                    if matches else
                    f"No match for '{needle}' in {files_searched} file(s) under {root}."),
        "matches": matches,
    }
