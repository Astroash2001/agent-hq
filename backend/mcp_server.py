"""
Model Context Protocol, outbound: this office as somebody else's tool.

`mcp_client.py` lets agents here use other people's servers. This is the other
direction -- Claude Code, or any MCP client, driving the harness: dispatch a
mission, read one back, list the roster.

Run it over stdio:

    python -m mcp_server        # from backend/, or `python backend/mcp_server.py`

The three capabilities are plain async functions, and the MCP wiring is built
around them in `build_server()`. That split is deliberate: the functions are
the contract and are tested directly, while the `mcp` package -- dev-optional
and imported lazily -- is only needed by the process that actually serves.

`run_mission` is synchronous in the protocol sense: it returns when the mission
finishes, because an MCP client has nowhere to put a progress stream. A long
mission is a long call, and the caller can watch the office at
http://localhost:8080 while it runs.
"""
import json
import logging
import sys
from typing import Any, Dict, List

import runstore
from config import settings

logger = logging.getLogger("AgentHQ.MCP.Server")

SERVER_NAME = "agenthq"


# ------------------------------------------------------------- capabilities

async def run_mission(goal: str) -> Dict[str, Any]:
    """Dispatch a goal through the office and return the finished briefing."""
    from orchestrator import orchestrator

    if not (goal or "").strip():
        return {"error": "goal is required"}
    result = await orchestrator.handle_user_input(goal)
    # `subtasks` carries every step's full artifact text, which an MCP client
    # did not ask for and would pay for in its own context window. The briefing,
    # the cost, and the id to fetch the rest with is the useful reply.
    return {
        "mission_id": result.get("task_id"),
        "status": result.get("status"),
        "briefing": result.get("result", ""),
        "mission_dir": result.get("mission_dir"),
        "cost_usd": result.get("cost_usd"),
        "tokens_estimated": result.get("tokens_estimated"),
        "artifacts": result.get("artifacts", []),
        "steps": [
            {"step_id": s.get("step_id"), "agent": s.get("agent_name"),
             "status": s.get("status"), "produces": s.get("produces")}
            for s in (result.get("subtasks") or [])
        ],
    }


async def get_mission(mission_id: str) -> Dict[str, Any]:
    """One mission from the run store: its plan, its steps and their status."""
    mission = runstore.get_store().get_mission(mission_id)
    if not mission:
        return {"error": f"no mission '{mission_id}'"}
    return mission


async def list_agents() -> List[Dict[str, Any]]:
    """The roster: who is on the floor, what they do, what they hold."""
    from orchestrator import orchestrator

    return [
        {"id": m["id"], "name": m["name"], "role": m["role"],
         "tools": m.get("tools", []), "stats": m.get("stats", {})}
        for m in orchestrator.get_manifests()
    ]


TOOLS: List[Dict[str, Any]] = [
    {
        "name": "run_mission",
        "description": ("Dispatch a goal to the AgentHQ office. The boss plans it into "
                        "a DAG, specialists execute it, and the finished briefing comes "
                        "back. Returns when the mission finishes."),
        "inputSchema": {
            "type": "object",
            "properties": {"goal": {"type": "string",
                                    "description": "What you want the office to do"}},
            "required": ["goal"],
        },
        "handler": run_mission,
    },
    {
        "name": "get_mission",
        "description": "Read one past or in-flight mission: its plan, steps and status.",
        "inputSchema": {
            "type": "object",
            "properties": {"mission_id": {"type": "string"}},
            "required": ["mission_id"],
        },
        "handler": get_mission,
    },
    {
        "name": "list_agents",
        "description": "List the agents on the floor, their roles and their tools.",
        "inputSchema": {"type": "object", "properties": {}},
        "handler": list_agents,
    },
]


async def dispatch(tool_name: str, arguments: Dict[str, Any]) -> Any:
    """
    Call one capability by name.

    The single place a tool name becomes a function, so the MCP handler and the
    tests exercise the same lookup rather than two that can drift.
    """
    for tool in TOOLS:
        if tool["name"] == tool_name:
            return await tool["handler"](**(arguments or {}))
    raise KeyError(f"unknown tool '{tool_name}'")


# ------------------------------------------------------------------ the server

def build_server():
    """
    Wire the capabilities into an MCP server. Needs the `mcp` package.

    The input schema each client sees is derived by the SDK from the handler's
    annotated signature, which is why the handlers carry real annotations. The
    `inputSchema` in `TOOLS` states the same contract for anything reading this
    module directly; `test_mcp.py` asserts the two agree, so they cannot drift.
    """
    from mcp.server import MCPServer

    server = MCPServer(SERVER_NAME, instructions=__doc__)
    for tool in TOOLS:
        server.add_tool(tool["handler"], name=tool["name"],
                        description=tool["description"])
    return server


async def serve_stdio():
    """Serve on stdin/stdout, which is how MCP clients launch a local server."""
    await build_server().run_stdio_async()


def main():
    import asyncio

    # stderr, never stdout: stdout IS the protocol channel on this transport,
    # and one stray log line there corrupts the stream for the client.
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    logger.info(f"AgentHQ MCP server on stdio (missions: {settings.missions_path})")
    asyncio.run(serve_stdio())


if __name__ == "__main__":
    main()
