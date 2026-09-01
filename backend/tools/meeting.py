"""
Tools that only work at the conference table.

Every other station gates tools that an agent could in principle run anywhere --
a terminal is a terminal. These two are different: they are about the room. They
read what the people in it brought, and they write down what the room decided.

They are bound to `conference_table` in office.py. Because the tool -> station
index is one-to-one, a meeting could not simply borrow `read_file` or
`write_file` without taking them away from the library and the archive, so the
table has its own pair instead.

Both reach the live mission workspace through a setter rather than an import:
the workspace is created per mission by the orchestrator, so there is nothing to
import at module load. When no mission is running they degrade to a clear error
instead of raising.
"""
from typing import Any, Dict, List, Optional

# Set by the orchestrator for the duration of a mission. Module-level because a
# tool function receives only its own arguments -- there is no context object
# threaded through the registry to hang this on.
_active_workspace = None


def bind_workspace(workspace) -> None:
    """Point the meeting tools at the running mission's workspace."""
    global _active_workspace
    _active_workspace = workspace


async def review_artifacts(names: Optional[List[str]] = None) -> Dict[str, Any]:
    """
    Read what the attendees brought to the table.

    With no `names`, every artifact filed so far is summarised -- which is the
    normal case, since the point of a meeting is that nobody has to know in
    advance what the others produced.
    """
    if _active_workspace is None:
        return {"success": False, "error": "No mission is running, so there is nothing to review."}

    available = _active_workspace.names()
    wanted = [n for n in (names or available) if n in available]
    missing = [n for n in (names or []) if n not in available]

    if not wanted:
        return {
            "success": True,
            "reviewed": [],
            "missing": missing,
            "note": "Nothing has been filed to the workspace yet."
        }

    return {
        "success": True,
        "reviewed": wanted,
        "missing": missing,
        "content": _active_workspace.render_context(wanted),
    }


async def record_decision(decision: str, title: str = "meeting_notes.md") -> Dict[str, Any]:
    """
    Write down what the room agreed, as a normal workspace artifact.

    Filed the same way any step's output is filed, so a later step can declare
    it in `consumes` and receive it exactly like anything else -- a meeting's
    conclusion is not a special kind of thing once it exists.
    """
    if _active_workspace is None:
        return {"success": False, "error": "No mission is running, so there is nothing to record against."}
    if not decision or not decision.strip():
        return {"success": False, "error": "A decision needs some text."}

    name = title if title.endswith(".md") else f"{title}.md"
    await _active_workspace.put(
        name=name,
        content=decision.strip(),
        author_id="conference_table",
        author_name="Conference Room",
        step_id="",
    )
    return {"success": True, "recorded": name, "size_chars": len(decision.strip())}


TOOLS = [
    {
        "name": "review_artifacts",
        "description": (
            "Read the work the other attendees brought to the meeting. Call with no "
            "arguments to see everything filed so far. Only available at the conference table."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "names": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Specific artifact filenames. Omit to review everything."
                }
            },
            "required": []
        },
        "function": review_artifacts,
    },
    {
        "name": "record_decision",
        "description": (
            "Write down what the meeting agreed, as an artifact later steps can consume. "
            "Only available at the conference table."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "decision": {"type": "string", "description": "What was agreed, and why."},
                "title": {"type": "string", "description": "Artifact filename, defaults to meeting_notes.md"}
            },
            "required": ["decision"]
        },
        "function": record_decision,
    },
]
