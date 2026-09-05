"""
Model Context Protocol, inbound: other people's tools, on this floor.

The tool layer is a private registry -- six functions this repo happens to
have written. MCP is how everyone else's tools are described, and speaking it
means an agent here can use a filesystem server, a Postgres server or a GitHub
server without any of them knowing this project exists.

The integration point is deliberately shallow. A discovered MCP tool is
registered into the same `tool_registry` under a namespaced name
(`mcp__<server>__<tool>`) and is called through `tool_registry.execute` like
any other, so it emits the same `tool.called` / `tool.result` events, is timed
the same way, and makes the agent walk to a station first -- the `integrations`
bench, added to `office.py` for exactly this. Nothing downstream needs to know
which tools came from where.

Off by default (`settings.mcp_enabled`), because it needs servers that are not
in this repo, and the `mcp` package is imported lazily so it stays out of
`requirements.txt`. A server that will not connect is a logged warning and
nothing more: the office starting without its integrations is a worse office,
not a broken one.
"""
import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

from config import settings
from office import office
from tool_registry import tool_registry

logger = logging.getLogger("AgentHQ.MCP")

# The station MCP tools are bound to. Declared here rather than in office.py's
# STATIONS list with a hardcoded tool set, because which tools exist is not
# known until the servers answer.
STATION_ID = "integrations"

# Namespaced so an MCP `read_file` can never shadow this repo's `read_file`.
# Two underscores, matching the convention MCP clients already use.
NAME_TEMPLATE = "mcp__{server}__{tool}"


@dataclass
class MCPServer:
    """One connected server and what it brought."""
    name: str
    transport: str                       # stdio | http
    tools: List[str] = field(default_factory=list)
    close: Optional[Callable[[], Awaitable[None]]] = None


# Connected servers, by name. Also the shutdown list.
_servers: Dict[str, MCPServer] = {}


# ------------------------------------------------------------------- the config

def load_config(path: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
    """
    Server definitions, in the shape every other MCP client already uses.

    ```json
    {"mcpServers": {
       "filesystem": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "."],
                      "grant_to": ["coder"]},
       "docs":       {"url": "https://example.com/mcp"}
    }}
    ```

    A missing file is not an error -- it is the ordinary state of a clone that
    has not configured any servers.
    """
    path = path or settings.mcp_config_path
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, ValueError) as e:
        logger.warning(f"Could not read MCP config {path}: {e}. No servers configured.")
        return {}

    servers = payload.get("mcpServers", payload)
    if not isinstance(servers, dict):
        logger.warning(f"MCP config {path} has no 'mcpServers' object. Ignoring it.")
        return {}
    return {name: spec for name, spec in servers.items() if isinstance(spec, dict)}


# --------------------------------------------------------------- registration

def _wrap(server_name: str, tool_name: str,
          call: Callable[[str, Dict[str, Any]], Awaitable[Any]]):
    """
    The callable `tool_registry` will execute.

    Returns a payload rather than raising, because the registry hands whatever
    comes back to the model: a refusal or an error the agent can read is worth
    more than an exception it retries blindly, which is the same reasoning
    `tools/terminal.py` gives for its structured refusals.
    """
    async def _invoke(**arguments: Any) -> Dict[str, Any]:
        try:
            result = await asyncio.wait_for(
                call(tool_name, arguments), timeout=settings.mcp_tool_timeout)
        except asyncio.TimeoutError:
            return {"success": False,
                    "error": f"MCP server '{server_name}' did not answer "
                             f"'{tool_name}' within {settings.mcp_tool_timeout}s"}
        except Exception as e:
            return {"success": False,
                    "error": f"MCP tool '{tool_name}' on '{server_name}' failed: "
                             f"{type(e).__name__}: {e}"}
        return {"success": True, "server": server_name, "tool": tool_name,
                "result": _plain(result)}

    return _invoke


def _plain(result: Any) -> Any:
    """
    An MCP result as something JSON-serialisable.

    The SDK returns a `CallToolResult` carrying typed content blocks, and, when
    the server declared an output type, the value itself under
    `structuredContent`. Prefer that: a list of five agents arrives as five
    separate text blocks, and reassembling the list from its printed pieces is
    worse than reading the list. The registry puts whatever comes back into an
    event payload and into the model's message history, so it has to survive
    `json.dumps`.
    """
    structured = (getattr(result, "structured_content", None)
                  or getattr(result, "structuredContent", None))
    if isinstance(structured, dict):
        # A handler returning something that is not an object is wrapped under
        # "result" by the SDK; unwrap it so the caller sees what was returned.
        return structured.get("result", structured)

    content = getattr(result, "content", None)
    if content is None:
        return result if isinstance(result, (str, int, float, bool, list, dict, type(None))) \
            else str(result)

    out: List[Any] = []
    for block in content:
        text = getattr(block, "text", None)
        if text is not None:
            out.append(text)
            continue
        data = getattr(block, "data", None)
        out.append(f"<{getattr(block, 'type', 'content')} block>" if data is not None
                   else str(block))
    if len(out) == 1:
        return out[0]
    return out


def register_server(
    server_name: str,
    tools: List[Dict[str, Any]],
    call: Callable[[str, Dict[str, Any]], Awaitable[Any]],
    agents: Optional[Dict[str, Any]] = None,
    grant_to: Optional[List[str]] = None,
) -> List[str]:
    """
    Put one server's tools on the floor.

    Separate from connecting on purpose: this half is pure -- a list of tool
    descriptors and something async to call -- so it can be exercised against
    an in-process stub without a subprocess, a socket or the `mcp` package.

    `grant_to` names the agents allowed to call them. Omitted means every
    specialist, which is the useful default once someone has gone to the
    trouble of enabling MCP and configuring a server; the boss is excluded
    because it plans rather than executes.
    """
    registered: List[str] = []
    for tool in tools:
        raw_name = tool.get("name") if isinstance(tool, dict) else getattr(tool, "name", None)
        if not raw_name:
            continue
        description = (tool.get("description") if isinstance(tool, dict)
                       else getattr(tool, "description", "")) or ""
        schema = (tool.get("inputSchema") if isinstance(tool, dict)
                  else getattr(tool, "inputSchema", None)) or {
            "type": "object", "properties": {}}

        name = NAME_TEMPLATE.format(server=server_name, tool=raw_name)
        tool_registry.register(
            name=name,
            description=f"[{server_name} via MCP] {description}".strip(),
            parameters=schema,
            function=_wrap(server_name, raw_name, call),
        )
        office.bind_tool(name, STATION_ID)
        registered.append(name)

    if agents:
        allowed = grant_to if grant_to is not None else list(agents.keys())
        for agent_id in allowed:
            agent = agents.get(agent_id)
            if agent is None:
                logger.warning(f"MCP server '{server_name}' grants tools to unknown "
                               f"agent '{agent_id}'; ignoring that grant")
                continue
            for name in registered:
                if name not in agent.tools:
                    agent.tools.append(name)

    logger.info(f"MCP server '{server_name}' registered {len(registered)} tool(s)")
    return registered


# ---------------------------------------------------------------- connecting

async def _open_stdio(spec: Dict[str, Any]):
    """A server this process launches and talks to over its stdio."""
    from mcp import ClientSession, StdioServerParameters      # lazy: dev-optional
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(
        command=spec["command"],
        args=list(spec.get("args", [])),
        # The child gets only what the config names. Same reasoning as
        # tools/terminal.py: a subprocess an agent can reach should not inherit
        # this process's API keys.
        env={**{"PATH": os.environ.get("PATH", "")}, **dict(spec.get("env", {}))},
        cwd=spec.get("cwd") or None,
    )
    return stdio_client(params), ClientSession


async def _open_http(spec: Dict[str, Any]):
    """A server already running somewhere, over streamable HTTP."""
    from mcp import ClientSession                              # lazy: dev-optional
    from mcp.client.streamable_http import (create_mcp_http_client,
                                            streamable_http_client)

    headers = dict(spec.get("headers", {}))
    # Headers are carried by the http client rather than passed to the
    # transport: the SDK moved them there, and building one here is also where
    # a per-server auth token belongs.
    client = create_mcp_http_client(headers=headers) if headers else None
    return streamable_http_client(spec["url"], http_client=client), ClientSession


async def connect(
    server_name: str,
    spec: Dict[str, Any],
    agents: Optional[Dict[str, Any]] = None,
) -> Optional[MCPServer]:
    """
    Connect one server, list its tools, register them. None on any failure.

    The session is held open for the life of the process behind an
    `AsyncExitStack`, because an MCP session is a live connection, not a
    request: closing it after listing the tools would leave every registered
    tool pointing at a dead pipe.
    """
    from contextlib import AsyncExitStack

    transport = "http" if spec.get("url") else "stdio"
    stack = AsyncExitStack()
    try:
        opener, session_cls = await (
            _open_http(spec) if transport == "http" else _open_stdio(spec))
        streams = await stack.enter_async_context(opener)
        # stdio yields (read, write); streamable HTTP yields a third element.
        read, write = streams[0], streams[1]
        session = await stack.enter_async_context(session_cls(read, write))
        await session.initialize()
        listing = await session.list_tools()
    except Exception as e:
        # Never fatal. A missing `mcp` package arrives here as ImportError, an
        # unreachable server as a connection error, and neither is a reason for
        # the office not to open.
        await stack.aclose()
        logger.warning(f"MCP server '{server_name}' unavailable "
                       f"({type(e).__name__}: {e}); continuing without it")
        return None

    async def _call(tool_name: str, arguments: Dict[str, Any]):
        return await session.call_tool(tool_name, arguments)

    names = register_server(
        server_name, list(getattr(listing, "tools", listing) or []), _call,
        agents=agents, grant_to=spec.get("grant_to"),
    )
    entry = MCPServer(name=server_name, transport=transport, tools=names,
                      close=stack.aclose)
    _servers[server_name] = entry
    return entry


async def connect_all(agents: Optional[Dict[str, Any]] = None) -> List[str]:
    """
    Connect every configured server. Returns the names that came up.

    Called from the app's lifespan. Failures are logged, never raised: startup
    completing is worth more than any one integration.
    """
    if not settings.mcp_enabled:
        return []

    config = load_config()
    if not config:
        logger.info("MCP is enabled but no servers are configured "
                    f"({settings.mcp_config_path})")
        return []

    connected: List[str] = []
    for name, spec in config.items():
        entry = await connect(name, spec, agents=agents)
        if entry is not None:
            connected.append(name)
    logger.info(f"MCP: {len(connected)}/{len(config)} server(s) connected")
    return connected


async def shutdown():
    """Close every session. Safe to call when nothing ever connected."""
    for name, entry in list(_servers.items()):
        if entry.close is not None:
            try:
                await entry.close()
            except Exception as e:
                logger.warning(f"MCP server '{name}' did not close cleanly: "
                               f"{type(e).__name__}: {e}")
        _servers.pop(name, None)


def connected_servers() -> Dict[str, MCPServer]:
    """What is currently on the floor. Read by `/api/office` and the tests."""
    return dict(_servers)
