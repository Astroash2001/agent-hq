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
    # Enough to ask the server what it offers a second time. Nothing did that,
    # so a server that gained a tool while the office was running stayed
    # invisible until a restart -- which is the normal case for an MCP server
    # under active development, and exactly when you want to see the new tool.
    relist: Optional[Callable[[], Awaitable[Any]]] = None
    spec: Dict[str, Any] = field(default_factory=dict)
    # The session's own call function, held so a refresh can register a NEW
    # tool against the live session. Reaching back through the registry instead
    # would hand the new tool a wrapper that calls itself.
    call: Optional[Callable[[str, Dict[str, Any]], Awaitable[Any]]] = None


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


def _resolve_grants(
    server_name: str,
    grant_to: Optional[Any],
    agents: Dict[str, Any],
    registered: List[str],
    by_raw: Dict[str, str],
) -> Dict[str, List[str]]:
    """
    Work out which prefixed tool names each agent may call.

    Kept separate from `register_server` so the grant rules can be read and
    tested without a tool registry, an office or a server. Unknown agents and
    unknown tool names are warned about and skipped rather than raising: a typo
    in one line of `mcp_servers.json` should cost that line, not the server.
    """
    if grant_to is None:
        return {aid: list(registered) for aid in agents}

    if isinstance(grant_to, dict):
        wanted = grant_to
    else:
        wanted = {aid: "*" for aid in grant_to}

    out: Dict[str, List[str]] = {}
    for agent_id, tools in wanted.items():
        if agent_id not in agents:
            logger.warning(f"MCP server '{server_name}' grants tools to unknown "
                           f"agent '{agent_id}'; ignoring that grant")
            continue
        if tools == "*" or tools is None:
            out[agent_id] = list(registered)
            continue
        if isinstance(tools, str):
            tools = [tools]

        granted: List[str] = []
        for raw in tools:
            # Written either way round: the server's own name, or the prefixed
            # one somebody copied out of a log.
            name = by_raw.get(raw) or (raw if raw in registered else None)
            if name is None:
                logger.warning(f"MCP server '{server_name}' grants unknown tool "
                               f"'{raw}' to '{agent_id}'; ignoring that grant")
                continue
            granted.append(name)
        if granted:
            out[agent_id] = granted

    return out


def register_server(
    server_name: str,
    tools: List[Dict[str, Any]],
    call: Callable[[str, Dict[str, Any]], Awaitable[Any]],
    agents: Optional[Dict[str, Any]] = None,
    grant_to: Optional[Any] = None,
) -> List[str]:
    """
    Put one server's tools on the floor.

    Separate from connecting on purpose: this half is pure -- a list of tool
    descriptors and something async to call -- so it can be exercised against
    an in-process stub without a subprocess, a socket or the `mcp` package.

    `grant_to` names who may call them, in either of two shapes:

        ["coder", "data_analyst"]                    every tool, to those agents
        {"coder": ["read_file"], "researcher": "*"}  named tools, per agent

    The list form was all there was, and it is all-or-nothing: an agent granted
    a filesystem server got its `delete_file` along with its `read_file`, which
    is the wrong default for the one server type most likely to be pointed at a
    real disk. The dict form grants per tool. Both are honoured, because the
    list form is right whenever a server's tools are uniformly safe and is
    shorter to write.

    Omitted means every specialist gets everything, which is the useful default
    once someone has gone to the trouble of enabling MCP and configuring a
    server; the boss is excluded because it plans rather than executes.
    """
    registered: List[str] = []
    # Raw name as the server calls it -> prefixed name as the office knows it.
    # A grant is written against the server's own names, because that is what
    # the server's documentation lists.
    by_raw: Dict[str, str] = {}
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
        by_raw[raw_name] = name

    if agents:
        for agent_id, names in _resolve_grants(
                server_name, grant_to, agents, registered, by_raw).items():
            agent = agents[agent_id]
            for name in names:
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
    async def _relist():
        return await session.list_tools()

    entry = MCPServer(name=server_name, transport=transport, tools=names,
                      close=stack.aclose, relist=_relist, spec=dict(spec),
                      call=_call)
    _servers[server_name] = entry
    return entry


async def refresh(agents: Optional[Dict[str, Any]] = None,
                  server_name: Optional[str] = None) -> Dict[str, List[str]]:
    """
    Ask the connected servers what they offer now, and register anything new.

    Nothing did this, so a server that gained a tool while the office was
    running stayed invisible until a restart -- the normal case for a server
    under active development, and exactly when you want to see the new tool.

    Re-uses the live session rather than reconnecting: a reconnect would drop
    whatever is mid-call, and the point is to add a tool without disturbing a
    mission.

    Returns {server: [newly registered tool names]}. A server whose listing
    fails is logged and skipped, for the same reason a server that will not
    connect does not stop startup.
    """
    added: Dict[str, List[str]] = {}
    targets = ([server_name] if server_name else list(_servers))

    for name in targets:
        entry = _servers.get(name)
        if entry is None or entry.relist is None or entry.call is None:
            logger.warning("MCP server '%s' cannot be refreshed (not connected)", name)
            continue
        try:
            listing = await entry.relist()
        except Exception as e:
            logger.warning("MCP server '%s' would not re-list (%s: %s); keeping "
                           "the tools it registered at startup",
                           name, type(e).__name__, e)
            continue

        before = set(entry.tools)
        names = register_server(
            name, list(getattr(listing, "tools", listing) or []),
            entry.call, agents=agents,
            grant_to=entry.spec.get("grant_to"))
        entry.tools = names

        # A tool the server has DROPPED is left registered on purpose. Removing
        # it mid-mission would turn a step that planned around it into a step
        # calling a tool that no longer exists, which fails worse than calling
        # one the server has quietly retired.
        fresh = [n for n in names if n not in before]
        if fresh:
            logger.info("MCP server '%s' gained %d tool(s): %s",
                        name, len(fresh), ", ".join(fresh))
        added[name] = fresh

    return added




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
