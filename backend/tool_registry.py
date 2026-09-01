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
        function: Callable[..., Any]
    ):
        """Register a new tool into the system."""
        self._tools[name] = {
            "name": name,
            "description": description,
            "parameters": parameters,
            "function": function
        }
        logger.info(f"Registered tool: {name}")

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
        task_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Execute a tool, emit live events, and return result payload.
        """
        if tool_name not in self._tools:
            err_msg = f"Tool '{tool_name}' is not registered in ToolRegistry."
            logger.error(err_msg)
            return {"success": False, "error": err_msg}

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
            function=execute_terminal
        )

        # 2. Web Search
        self.register(
            name="web_search",
            description="Search online documentation, technical articles, and job resources for factual data.",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search keyword or query string"},
                    "max_results": {"type": "integer", "description": "Number of results to retrieve (default 3)"}
                },
                "required": ["query"]
            },
            function=search_web
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
            function=read_file
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
            function=write_file
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
            function=list_files
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
            function=analyze_python_code
        )

        # 7. Conference table. These two only work in the meeting room -- see
        # tools/meeting.py for why they are their own pair rather than reusing
        # read_file / write_file.
        from tools.meeting import TOOLS as MEETING_TOOLS
        for spec in MEETING_TOOLS:
            self.register(
                name=spec["name"],
                description=spec["description"],
                parameters=spec["parameters"],
                function=spec["function"]
            )


# Global singleton Tool Registry instance
tool_registry = ToolRegistry()
