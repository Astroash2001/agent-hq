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


# ----------------------------------------------------------------- tools

async def read_file(filepath: str) -> Dict[str, Any]:
    """Read contents of a text file within the sandbox jail."""
    # Kill switch bypass
    if not getattr(settings, "file_ops_sandbox_enabled", True):
        return await _read_file_unconstrained(filepath)

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

        with open(candidate, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()

        return {
            "success": True,
            "filepath": filepath,
            "size_bytes": len(content),
            "content": content[:8000],  # Cap length for LLM context safety
        }
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

async def _read_file_unconstrained(filepath: str) -> Dict[str, Any]:
    try:
        if not os.path.exists(filepath):
            return {"success": False, "error": f"File not found: {filepath}"}
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        return {
            "success": True,
            "filepath": filepath,
            "size_bytes": len(content),
            "content": content[:8000],
        }
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
