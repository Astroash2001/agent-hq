import asyncio
import inspect
import json
import logging
import time
from typing import Any, Callable, Dict, List, Optional

from event_bus import event_bus
from schemas import Event, EventType
from tools.terminal import execute_terminal
from tools.file_ops import read_file, write_file, list_files
from tools.web_search import search_web
from tools.code_analysis import analyze_python_code
from tools.fetch_url import fetch_url
from tools.calculate import calculate
from tools.query_data import query_data
from tools.search_workspace import search_workspace

logger = logging.getLogger("AgentHQ.ToolRegistry")


class ToolRegistry:
    """
    Central Tool Hub.
    Manages schemas, dispatches tool execution, and emits telemetry events.
    """

    def __init__(self):
        self._tools: Dict[str, Dict[str, Any]] = {}
        self._register_default_tools()

    def register(
        self,
        name: str,
        description: str,
        parameters: Dict[str, Any],
        function: Callable[..., Any],
        risk: str = "read",
    ):
        """
        Register a new tool into the system.

        `risk` says what the tool can do to the world, in one word, and exists
        for the code that hands tools to an agent a model just invented: a
        catalogue that does not say which entries are dangerous forces that
        caller to keep its own list, which will drift. Three values:

          read    observes something and changes nothing
          write   changes state the mission owns (files, decisions)
          execute runs code, or reaches an address chosen at runtime

        MCP tools default to `execute`, because what a third-party server does
        behind its own tool name is not knowable from here.
        """
        self._tools[name] = {
            "name": name,
            "description": description,
            "parameters": parameters,
            "function": function,
            "risk": risk,
        }
        logger.info(f"Registered tool: {name}")

    def risk_of(self, tool_name: str) -> str:
        """What `tool_name` can do to the world. Unknown tools are `execute`."""
        tool = self._tools.get(tool_name)
        return tool["risk"] if tool else "execute"

    def catalogue(self) -> Dict[str, Dict[str, str]]:
        """
        Every registered tool, with its description and risk.

        The authority on what exists. Anything choosing tools -- the planner
        prompt, the agent foundry -- reads this rather than a hardcoded list,
        so MCP tools registered at startup are offered on the same footing as
        the built-ins and a tool that does not exist cannot be granted.
        """
        return {
            name: {"description": tool["description"], "risk": tool["risk"]}
            for name, tool in sorted(self._tools.items())
        }

    def get_tool_schema(self, tool_name: str) -> Optional[Dict[str, Any]]:
        """Return the tool schema formatted for OpenAI/LiteLLM function calling."""
        tool = self._tools.get(tool_name)
        if not tool:
            return None

        return {
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool["description"],
                "parameters": tool["parameters"]
            }
        }

    def get_schemas_for_agent(self, tool_names: List[str]) -> List[Dict[str, Any]]:
        """Return list of schemas for the tools assigned to a specific agent."""
        schemas = []
        for name in tool_names:
            schema = self.get_tool_schema(name)
            if schema:
                schemas.append(schema)
        return schemas

    async def execute(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
        agent_id: Optional[str] = None,
        task_id: Optional[str] = None,
        granted: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """
        Execute a tool, emit live events, and return result payload.

        `granted` is the caller's tool list. When given, a tool outside it is
        refused here rather than run.

        Until this check existed, a grant was enforced only by omission: an
        agent is shown schemas for its own tools, so in practice it only calls
        those. That is a habit of the model, not a property of the system --
        nothing stopped a reply naming `terminal` from running `terminal`,
        whatever the manifest said. It held up while every manifest was
        hand-written and reviewed. It stops holding the moment an agent's tool
        list is chosen at runtime, which is what the foundry does, so the
        boundary has to be real before that lands.

        `None` means an internal caller that is not acting on a model's
        instruction -- the meeting machinery, a test. It is not a way for an
        agent to opt out: `base_agent` always passes its list.
        """
        if tool_name not in self._tools:
            err_msg = f"Tool '{tool_name}' is not registered in ToolRegistry."
            logger.error(err_msg)
            return {"success": False, "error": err_msg}

        if granted is not None and tool_name not in granted:
            # Refused, and the reply says what IS available. A bare refusal
            # gets retried with a synonym until the step's budget is gone --
            # the failure mode `web_search` documents for filler results.
            logger.warning("%s called %s without a grant", agent_id or "?", tool_name)
            return {
                "success": False,
                "refused": True,
                "error": (f"'{tool_name}' is not one of your tools. "
                          f"You have: {', '.join(sorted(granted)) or 'none'}."),
            }

        tool = self._tools[tool_name]
        start_time = time.time()

        # 1. Emit TOOL_CALLED Event
        await event_bus.emit(Event(
            type=EventType.TOOL_CALLED,
            agent_id=agent_id,
            task_id=task_id,
            data={
                "tool": tool_name,
                "arguments": arguments
            }
        ))

        # 2. Emit TERMINAL_LOG Event for visual streaming
        args_summary = json.dumps(arguments, ensure_ascii=False)
        if len(args_summary) > 100:
            args_summary = args_summary[:100] + "..."
        await event_bus.emit(Event(
            type=EventType.TERMINAL_LOG,
            agent_id=agent_id,
            task_id=task_id,
            data={
                "level": "INFO",
                "message": f"[{agent_id or 'AGENT'}] Calling tool -> {tool_name}({args_summary})"
            }
        ))

        # 3. Execute Tool Function
        try:
            fn = tool["function"]
            if asyncio.iscoroutinefunction(fn):
                result = await fn(**arguments)
            else:
                result = fn(**arguments)
        except Exception as e:
            result = {"success": False, "error": f"Tool execution failed: {str(e)}"}

        duration_ms = (time.time() - start_time) * 1000

        # 4. Emit TOOL_RESULT Event
        await event_bus.emit(Event(
            type=EventType.TOOL_RESULT,
            agent_id=agent_id,
            task_id=task_id,
            data={
                "tool": tool_name,
                "duration_ms": round(duration_ms, 2),
                "result": result
            }
        ))

        # 5. Emit TERMINAL_LOG Event for result
        result_preview = str(result)
        if len(result_preview) > 150:
            result_preview = result_preview[:150] + "..."
        await event_bus.emit(Event(
            type=EventType.TERMINAL_LOG,
            agent_id=agent_id,
            task_id=task_id,
            data={
                "level": "SUCCESS" if result.get("success", True) else "ERROR",
                "message": f"[{agent_id or 'AGENT'}] Tool '{tool_name}' finished in {duration_ms:.1f}ms -> {result_preview}"
            }
        ))

        return result

    def _register_default_tools(self):
        """Register the standard built-in harness tools."""
        # 1. Terminal / Shell
        self.register(
            name="terminal",
            description="Execute a shell or PowerShell command on the host system to run scripts, tests, or check status.",
            parameters={
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "The exact shell command to execute"},
                    "cwd": {"type": "string", "description": "Optional working directory"}
                },
                "required": ["command"]
            },
            function=execute_terminal,
            risk="execute",
        )

        # 2. Web Search
        self.register(
            name="web_search",
            description=(
                "Search the live web for current facts, figures and sources. Returns "
                "ranked snippets with their URLs. "
                "You get a small, fixed number of searches per task, so make each one "
                "count: search only for facts you actually need for your deliverable, "
                "one specific question at a time. Do not re-search a topic from a "
                "different angle, chase background context, or verify a figure you "
                "already have from a source. When you have enough to write, stop "
                "searching and write. "
                "If the reply says no provider is configured, searching is unavailable "
                "here -- do not call this tool again; answer from your own knowledge "
                "and note that it was not verified against live sources."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search keyword or query string"},
                    "max_results": {"type": "integer",
                                    "description": "How many results to retrieve, 1-20. Omit for the default."}
                },
                "required": ["query"]
            },
            function=search_web,
            risk="read",
        )

        # 3. Read File
        self.register(
            name="read_file",
            description="Read text content from a file path in the workspace.",
            parameters={
                "type": "object",
                "properties": {
                    "filepath": {"type": "string", "description": "Absolute or relative file path to read"}
                },
                "required": ["filepath"]
            },
            function=read_file,
            risk="read",
        )

        # 4. Write File
        self.register(
            name="write_file",
            description="Write or overwrite code or text content to a destination file.",
            parameters={
                "type": "object",
                "properties": {
                    "filepath": {"type": "string", "description": "File path to write"},
                    "content": {"type": "string", "description": "Text content to save"}
                },
                "required": ["filepath", "content"]
            },
            function=write_file,
            risk="write",
        )

        # 5. List Files
        self.register(
            name="list_files",
            description="List files and directories in a given folder.",
            parameters={
                "type": "object",
                "properties": {
                    "directory": {"type": "string", "description": "Directory to inspect"}
                },
                "required": ["directory"]
            },
            function=list_files,
            risk="read",
        )

        # 6. Code Analysis
        self.register(
            name="code_analysis",
            description="Parse Python code using AST and run a test execution to detect bugs or syntax errors.",
            parameters={
                "type": "object",
                "properties": {
                    "code_snippet": {"type": "string", "description": "Python source code string to analyze"}
                },
                "required": ["code_snippet"]
            },
            function=analyze_python_code,
            risk="execute",
        )

        # 7. Fetch URL
        self.register(
            name="fetch_url",
            description=(
                "Fetch one web page or JSON API by its exact URL and return its text. "
                "Use this when you already have a URL -- from web_search, from the "
                "goal, or from another agent's artifact -- and need what it actually "
                "says rather than a snippet. web_search finds pages; this one reads "
                "them. Only public http/https addresses can be fetched."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "Full http/https URL to fetch"},
                    "max_chars": {"type": "integer",
                                  "description": "Trim the text to this many characters. Omit for the default."}
                },
                "required": ["url"]
            },
            function=fetch_url,
            risk="execute",
        )

        # 8. Calculate
        self.register(
            name="calculate",
            description=(
                "Evaluate an arithmetic expression exactly. Use this for every "
                "calculation that ends up in your deliverable -- totals, differences, "
                "percentages, unit conversions, growth rates -- rather than working it "
                "out yourself. Literal numbers and operators only: substitute values "
                "in, no variables or function names. Example: '(4200 - 3150) / 3150 * 100'."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "expression": {"type": "string",
                                   "description": "Arithmetic expression, e.g. '1250 * 0.87 + 40'"}
                },
                "required": ["expression"]
            },
            function=calculate,
            risk="read",
        )

        # 9. Query Data
        self.register(
            name="query_data",
            description=(
                "Run a read-only SQL SELECT against a CSV or JSON file. The file "
                "becomes a table called 'data' whose columns are its headers, "
                "lowercased with non-alphanumerics replaced by underscores. Use this "
                "instead of reading a data file and counting by hand: it is exact, and "
                "it works on files far larger than you can hold. Start with "
                "'SELECT * FROM data LIMIT 5' to see the columns."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "filepath": {"type": "string", "description": "Path to a .csv, .tsv or .json file"},
                    "query": {"type": "string",
                              "description": "A SELECT statement against the table 'data'"},
                    "max_rows": {"type": "integer",
                                 "description": "Cap on rows returned. Omit for the default."}
                },
                "required": ["filepath", "query"]
            },
            function=query_data,
            risk="read",
        )

        # 10. Search Workspace
        self.register(
            name="search_workspace",
            description=(
                "Search the mission's files for a string and return matching lines "
                "with their file and line number. Use this to find which artifact "
                "mentions something, instead of reading every file to look -- reading "
                "whole documents to find one line is the most expensive thing you can "
                "do. Searches the current mission's directory by default."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Text to find, or a regex if regex=true"},
                    "directory": {"type": "string",
                                  "description": "Where to search. Omit for the current mission's files."},
                    "regex": {"type": "boolean",
                              "description": "Treat the pattern as a regular expression. Default false."},
                    "max_results": {"type": "integer", "description": "Cap on matches returned."}
                },
                "required": ["pattern"]
            },
            function=search_workspace,
            risk="read",
        )

        # 11. Conference table. These two only work in the meeting room -- see
        # tools/meeting.py for why they are their own pair rather than reusing
        # read_file / write_file.
        from tools.meeting import TOOLS as MEETING_TOOLS
        for spec in MEETING_TOOLS:
            self.register(
                name=spec["name"],
                description=spec["description"],
                parameters=spec["parameters"],
                function=spec["function"],
                risk="write",
            )


# Global singleton Tool Registry instance
tool_registry = ToolRegistry()
