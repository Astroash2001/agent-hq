"""
File tools.

`write_file` is bound to the running mission. Before that it wrote to whatever
path the model invented, relative to the process working directory -- so an
agent's actual deliverable ended up loose in backend/ while the workspace
recorded only the agent's closing sentence about it ("Task completed after 4
execution steps.", 39 bytes, standing in for a 2.4KB report). Downstream steps
then consumed the sentence instead of the work.

The binding follows the same shape as tools/meeting.py: the orchestrator sets
the active workspace for the length of a mission, because the workspace is
created per mission and there is nothing to import at module load.
"""
import os
from typing import Dict, Any, List, Optional

# Set by the orchestrator for the duration of a mission.
_active_workspace = None
_active_author = None


def bind_workspace(workspace, author_id: str = "", author_name: str = "", step_id: str = "") -> None:
    """Point write_file at the running mission's workspace."""
    global _active_workspace, _active_author
    _active_workspace = workspace
    _active_author = (author_id, author_name, step_id) if workspace else None


async def read_file(filepath: str) -> Dict[str, Any]:
    """Read contents of a text file."""
    try:
        if not os.path.exists(filepath):
            return {"success": False, "error": f"File not found: {filepath}"}

        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()

        return {
            "success": True,
            "filepath": filepath,
            "size_bytes": len(content),
            "content": content[:8000]  # Cap length for LLM context safety
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


async def write_file(filepath: str, content: str) -> Dict[str, Any]:
    """
    Write a file into the mission workspace.

    Filed as a workspace artifact rather than dropped on disk at an arbitrary
    path, so the thing the agent actually produced is what downstream steps
    receive and what ends up in the mission directory. With no mission running
    (a bare agent, or a test) it falls back to the old direct write.
    """
    if _active_workspace is not None:
        author_id, author_name, step_id = _active_author or ("", "", "")
        artifact = await _active_workspace.put(
            name=os.path.basename(filepath or "untitled.md"),
            content=content,
            author_id=author_id or "agent",
            author_name=author_name or "Agent",
            step_id=step_id,
        )
        return {
            "success": True,
            "filepath": artifact.name,
            "bytes_written": artifact.size_chars,
            "message": f"Filed {artifact.size_chars} chars to the mission workspace "
                       f"as '{artifact.name}'.",
        }
    try:
        os.makedirs(os.path.dirname(os.path.abspath(filepath)), exist_ok=True)
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(content)

        return {
            "success": True,
            "filepath": filepath,
            "bytes_written": len(content),
            "message": f"Successfully wrote {len(content)} bytes to {filepath}"
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


async def list_files(directory: str = ".") -> Dict[str, Any]:
    """List files in a directory."""
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
                "size_bytes": os.path.getsize(full_path) if not is_dir else 0
            })

        return {
            "success": True,
            "directory": directory,
            "count": len(items),
            "items": items[:50]
        }
    except Exception as e:
        return {"success": False, "error": str(e)}
