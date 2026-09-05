"""
MCP in both directions, without a real server.

Inbound (`mcp_client`), the claim is that a tool from somewhere else is not a
special case: it lands in the same registry under a namespaced name, is called
through `tool_registry.execute` so it emits the same events, and is gated on a
station so the agent still walks somewhere to use it. The stub here is an
in-process object with `list_tools`/`call_tool` -- a subprocess would be
testing the `mcp` package, which is not this repo's code.

Outbound (`mcp_server`), the capabilities are plain async functions and the MCP
wiring is built around them, so what a client would call is what is called
here.

Most of this needs no `mcp` package at all -- it is dev-optional, so an
install without it must still work, and an unreachable or unimportable server
must arrive as a logged warning rather than a failed startup. The two tests at
the end do need it, and are skipped without it: one holds the declared schemas
against the ones the SDK derives, and one drives this repo's own server over a
real stdio subprocess, which is the only way to find out whether the SDK API
this code was written against is the one that shipped.
"""
import asyncio
import json
import os
import shutil
import sys
import tempfile

import pytest

import mcp_client
import mcp_server
from config import settings
from event_bus import event_bus
from office import office
from schemas import AgentManifest, Event, EventType
from tool_registry import tool_registry


class StubTool:
    def __init__(self, name, description="", schema=None):
        self.name = name
        self.description = description
        self.inputSchema = schema or {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        }


class StubBlock:
    type = "text"

    def __init__(self, text):
        self.text = text


class StubResult:
    def __init__(self, text):
        self.content = [StubBlock(text)]


class StubServer:
    """An MCP server, minus the protocol."""

    def __init__(self, name="demo", fail=False):
        self.name = name
        self.fail = fail
        self.calls = []

    async def list_tools(self):
        return [StubTool("echo", "Echo the text back."),
                StubTool("slow", "Takes its time.")]

    async def call_tool(self, tool_name, arguments):
        self.calls.append((tool_name, arguments))
        if self.fail:
            raise RuntimeError("server exploded")
        if tool_name == "slow":
            await asyncio.sleep(5)
        return StubResult(f"{tool_name}:{arguments.get('text', '')}")


def _mcp_tools(agent):
    """Just the MCP grants -- an agent arrives with its own built-in tools."""
    return [t for t in agent.tools if t.startswith("mcp__")]


def _agent(agent_id, name):
    from base_agent import BaseAgent
    return BaseAgent(AgentManifest(
        id=agent_id, name=name, role="Specialist",
        system_prompt="You are a specialist.", tools=["read_file"],
    ))


@pytest.fixture
def stub():
    """A registered stub server, cleaned out of the global registry after."""
    server = StubServer()
    names = []

    def _register(agents=None, grant_to=None):
        # The descriptors a real `session.list_tools()` would have returned.
        # Registration is the half under test; listing them is the SDK's job.
        registered = mcp_client.register_server(
            server.name,
            [StubTool("echo", "Echo the text back."), StubTool("slow", "Takes its time.")],
            server.call_tool, agents=agents, grant_to=grant_to)
        names.extend(registered)
        return registered

    yield server, _register

    for name in names:
        tool_registry._tools.pop(name, None)
        office._tool_index.pop(name, None)
    station = office.get_station(mcp_client.STATION_ID)
    if station:
        station.tools = [t for t in station.tools if t not in names]


# -------------------------------------------------------------------- inbound

def test_tools_are_namespaced_and_bound_to_the_integrations_station(stub):
    server, register = stub
    names = register()

    assert names == ["mcp__demo__echo", "mcp__demo__slow"]
    # The namespace is what stops an MCP `read_file` shadowing this repo's.
    assert tool_registry.get_tool_schema("read_file")["function"]["description"] \
        != tool_registry.get_tool_schema("mcp__demo__echo")["function"]["description"]

    station = office.station_for_tool("mcp__demo__echo")
    assert station is not None and station.id == mcp_client.STATION_ID
    assert "mcp__demo__echo" in station.tools


@pytest.mark.asyncio
async def test_an_mcp_tool_runs_through_the_registry_and_emits_its_events(stub):
    server, register = stub
    register()

    seen = []

    async def _listen(event: Event):
        seen.append(event)

    event_bus.subscribe(EventType.TOOL_CALLED, _listen)
    event_bus.subscribe(EventType.TOOL_RESULT, _listen)
    try:
        result = await tool_registry.execute(
            "mcp__demo__echo", {"text": "hello"}, agent_id="coder", task_id="t1")
        await asyncio.sleep(0.01)   # let the last emitted event reach the listener
    finally:
        event_bus.unsubscribe(EventType.TOOL_CALLED, _listen)
        event_bus.unsubscribe(EventType.TOOL_RESULT, _listen)

    assert result["success"] is True
    assert result["result"] == "echo:hello"
    assert server.calls == [("echo", {"text": "hello"})]
    assert [e.type for e in seen] == [EventType.TOOL_CALLED, EventType.TOOL_RESULT]
    assert seen[1].data["tool"] == "mcp__demo__echo"


@pytest.mark.asyncio
async def test_a_failing_tool_returns_a_readable_refusal_not_an_exception(stub):
    server, register = stub
    server.fail = True
    register()

    result = await tool_registry.execute("mcp__demo__echo", {"text": "hi"})
    assert result["success"] is False
    assert "server exploded" in result["error"]


@pytest.mark.asyncio
async def test_a_tool_that_never_answers_times_out_with_a_message(stub, monkeypatch):
    server, register = stub
    register()
    monkeypatch.setattr(settings, "mcp_tool_timeout", 0.05)

    result = await tool_registry.execute("mcp__demo__slow", {"text": "hi"})
    assert result["success"] is False
    assert "did not answer" in result["error"]


def test_a_tool_reaches_only_the_agents_it_was_granted_to(stub):
    server, register = stub
    agents = {"coder": _agent("coder", "Bob"), "creative": _agent("creative", "Eve")}
    register(agents=agents, grant_to=["coder"])

    bob_schemas = [s["function"]["name"]
                   for s in tool_registry.get_schemas_for_agent(agents["coder"].tools)]
    eve_schemas = [s["function"]["name"]
                   for s in tool_registry.get_schemas_for_agent(agents["creative"].tools)]

    assert "mcp__demo__echo" in bob_schemas
    assert "mcp__demo__echo" not in eve_schemas


def test_a_dict_grant_hands_over_only_the_named_tools(stub):
    """
    The list form is all-or-nothing: an agent granted a filesystem server gets
    its `delete_file` along with its `read_file`, which is the wrong default for
    the server type most likely to be pointed at a real disk.
    """
    server, register = stub
    agents = {"coder": _agent("coder", "Bob"), "creative": _agent("creative", "Eve")}
    register(agents=agents, grant_to={"coder": ["echo"]})

    assert "mcp__demo__echo" in agents["coder"].tools
    assert "mcp__demo__slow" not in agents["coder"].tools, "granted one, given two"
    assert _mcp_tools(agents["creative"]) == []


def test_a_star_in_a_dict_grant_means_every_tool(stub):
    """So the two shapes can be mixed in one config without a second server."""
    server, register = stub
    agents = {"coder": _agent("coder", "Bob"), "creative": _agent("creative", "Eve")}
    register(agents=agents, grant_to={"coder": "*", "creative": ["slow"]})

    assert "mcp__demo__echo" in agents["coder"].tools
    assert "mcp__demo__slow" in agents["coder"].tools
    assert _mcp_tools(agents["creative"]) == ["mcp__demo__slow"]


def test_a_grant_may_name_the_prefixed_tool_instead(stub):
    """Whichever name someone copied -- the server's own, or one out of a log."""
    server, register = stub
    agents = {"coder": _agent("coder", "Bob")}
    register(agents=agents, grant_to={"coder": ["mcp__demo__echo"]})

    assert _mcp_tools(agents["coder"]) == ["mcp__demo__echo"]


def test_an_unknown_tool_in_a_grant_costs_that_line_not_the_server(stub):
    """A typo in one line of mcp_servers.json must not lose the whole server."""
    server, register = stub
    agents = {"coder": _agent("coder", "Bob")}
    registered = register(agents=agents,
                          grant_to={"coder": ["echo", "no_such_tool"]})

    assert len(registered) == 2, "both tools are still on the floor"
    assert _mcp_tools(agents["coder"]) == ["mcp__demo__echo"]


def test_an_unknown_agent_in_a_dict_grant_is_skipped(stub):
    server, register = stub
    agents = {"coder": _agent("coder", "Bob")}
    register(agents=agents, grant_to={"ghost": ["echo"], "coder": ["echo"]})

    assert _mcp_tools(agents["coder"]) == ["mcp__demo__echo"]


def test_an_omitted_grant_list_means_every_specialist(stub):
    server, register = stub
    agents = {"coder": _agent("coder", "Bob"), "creative": _agent("creative", "Eve")}
    register(agents=agents)

    assert "mcp__demo__echo" in agents["coder"].tools
    assert "mcp__demo__echo" in agents["creative"].tools


# --------------------------------------------------------------- the config

def test_a_missing_config_is_not_an_error(monkeypatch):
    monkeypatch.setattr(settings, "mcp_config_path", "does-not-exist.json")
    assert mcp_client.load_config() == {}


def test_the_config_is_read_in_the_standard_mcpservers_shape(monkeypatch):
    scratch = tempfile.mkdtemp(prefix="agenthq_mcp_")
    try:
        path = os.path.join(scratch, "mcp_servers.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"mcpServers": {
                "fs": {"command": "npx", "args": ["-y", "server"], "grant_to": ["coder"]},
                "web": {"url": "https://example.com/mcp"},
            }}, f)
        monkeypatch.setattr(settings, "mcp_config_path", path)

        config = mcp_client.load_config()
        assert set(config) == {"fs", "web"}
        assert config["fs"]["grant_to"] == ["coder"]
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def test_the_example_config_this_repo_ships_parses(monkeypatch):
    example = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "mcp_servers.example.json")
    monkeypatch.setattr(settings, "mcp_config_path", example)
    config = mcp_client.load_config()
    assert "filesystem" in config
    assert config["filesystem"]["command"] == "npx"


@pytest.mark.asyncio
async def test_an_unreachable_server_logs_and_startup_still_succeeds(monkeypatch, caplog):
    """
    The `mcp` package is dev-optional and may not be installed at all, which
    arrives here as an ImportError. Either way the office opens.
    """
    scratch = tempfile.mkdtemp(prefix="agenthq_mcp_")
    try:
        path = os.path.join(scratch, "mcp_servers.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"mcpServers": {
                "nope": {"command": "definitely-not-a-real-binary-xyz", "args": []}}}, f)
        monkeypatch.setattr(settings, "mcp_config_path", path)
        monkeypatch.setattr(settings, "mcp_enabled", True)

        connected = await mcp_client.connect_all(agents={})
        assert connected == []
        assert any("unavailable" in r.message or "unavailable" in r.getMessage()
                   for r in caplog.records)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


@pytest.mark.asyncio
async def test_disabled_mcp_connects_nothing(monkeypatch):
    monkeypatch.setattr(settings, "mcp_enabled", False)

    def _boom(*a, **k):
        raise AssertionError("disabled MCP must not read a config")

    monkeypatch.setattr(mcp_client, "load_config", _boom)
    assert await mcp_client.connect_all() == []


# ------------------------------------------------------------------- outbound

@pytest.mark.asyncio
async def test_the_server_exposes_three_capabilities_by_name():
    assert [t["name"] for t in mcp_server.TOOLS] == [
        "run_mission", "get_mission", "list_agents"]
    for tool in mcp_server.TOOLS:
        assert tool["description"]
        assert tool["inputSchema"]["type"] == "object"


@pytest.mark.asyncio
async def test_list_agents_returns_the_roster():
    agents = await mcp_server.dispatch("list_agents", {})
    ids = {a["id"] for a in agents}
    assert {"coder", "researcher", "creative"} <= ids
    assert all("role" in a and "tools" in a for a in agents)


@pytest.mark.asyncio
async def test_an_unknown_mission_is_an_error_not_a_crash():
    got = await mcp_server.dispatch("get_mission", {"mission_id": "task_nope"})
    assert "error" in got


@pytest.mark.asyncio
async def test_an_unknown_tool_is_rejected_by_name():
    with pytest.raises(KeyError):
        await mcp_server.dispatch("drop_database", {})


@pytest.mark.asyncio
async def test_run_mission_returns_the_briefing_not_every_artifact(monkeypatch):
    """The client asked for a mission, not for a copy of its whole workspace."""
    from orchestrator import orchestrator

    async def _fake(goal, resume_from=None):
        return {
            "task_id": "task_abc", "status": "completed", "result": "The briefing.",
            "subtasks": [{"step_id": "s1", "agent_name": "Bob", "status": "completed",
                          "produces": "notes.md", "output": "x" * 5000}],
            "artifacts": ["notes.md"], "mission_dir": "/tmp/x",
            "cost_usd": 0.01, "tokens_estimated": False,
        }

    monkeypatch.setattr(orchestrator, "handle_user_input", _fake)
    got = await mcp_server.dispatch("run_mission", {"goal": "do a thing"})

    assert got["mission_id"] == "task_abc"
    assert got["briefing"] == "The briefing."
    assert got["steps"] == [{"step_id": "s1", "agent": "Bob",
                             "status": "completed", "produces": "notes.md"}]
    assert "x" * 5000 not in json.dumps(got)


# ------------------------------------------------ against the real SDK, if present

mcp_installed = pytest.mark.skipif(
    __import__("importlib").util.find_spec("mcp") is None,
    reason="the mcp package is dev-optional and is not installed")


@mcp_installed
@pytest.mark.asyncio
async def test_the_declared_schema_matches_what_the_sdk_derives():
    """
    `TOOLS` documents the input schema; the SDK derives its own from each
    handler's annotations. Two descriptions of one contract drift unless
    something compares them.
    """
    server = mcp_server.build_server()
    derived = {t.name: t.input_schema for t in await server.list_tools()}

    for tool in mcp_server.TOOLS:
        got = derived[tool["name"]]
        assert set(got["properties"]) == set(tool["inputSchema"]["properties"])
        assert sorted(got.get("required", [])) == sorted(tool["inputSchema"].get("required", []))


@mcp_installed
@pytest.mark.asyncio
async def test_the_client_can_drive_this_repos_own_server_over_stdio():
    """
    Both halves at once, over a real subprocess and a real protocol: the client
    launches `python -m mcp_server`, its three capabilities arrive as
    `mcp__agenthq__*` tools on the integrations bench, and calling one through
    `tool_registry.execute` returns the roster.
    """
    backend = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    entry = await mcp_client.connect("agenthq", {
        "command": sys.executable,
        "args": ["-m", "mcp_server"],
        "cwd": backend,
    })
    if entry is None:
        pytest.skip("could not launch the stdio server in this environment")

    try:
        assert set(entry.tools) == {"mcp__agenthq__run_mission",
                                    "mcp__agenthq__get_mission",
                                    "mcp__agenthq__list_agents"}
        assert office.station_for_tool("mcp__agenthq__list_agents").id == \
            mcp_client.STATION_ID

        result = await tool_registry.execute("mcp__agenthq__list_agents", {})
        assert result["success"] is True
        roster = result["result"]
        assert {"coder", "researcher", "creative"} <= {a["id"] for a in roster}
    finally:
        await mcp_client.shutdown()
        for name in entry.tools:
            tool_registry._tools.pop(name, None)
            office._tool_index.pop(name, None)
        station = office.get_station(mcp_client.STATION_ID)
        if station:
            station.tools = [t for t in station.tools if t not in entry.tools]


@pytest.mark.asyncio
async def test_an_empty_goal_is_refused_before_a_mission_starts(monkeypatch):
    from orchestrator import orchestrator

    async def _boom(goal, resume_from=None):
        raise AssertionError("an empty goal must not reach the orchestrator")

    monkeypatch.setattr(orchestrator, "handle_user_input", _boom)
    assert "error" in await mcp_server.dispatch("run_mission", {"goal": "   "})
