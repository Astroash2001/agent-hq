import asyncio
import os
import subprocess
from typing import Dict, Any


async def execute_terminal(command: str, cwd: str = None, timeout_seconds: int = 15) -> Dict[str, Any]:
    """
    Execute a shell command asynchronously and return stdout, stderr, and exit code.
    Guardrail: Limits execution time to prevent hanging commands.
    """
    work_dir = cwd or os.getcwd()

    try:
        process = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=work_dir
        )

        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
            return {
                "success": process.returncode == 0,
                "exit_code": process.returncode,
                "stdout": stdout.decode("utf-8", errors="replace").strip(),
                "stderr": stderr.decode("utf-8", errors="replace").strip(),
            }
        except asyncio.TimeoutError:
            process.kill()
            return {
                "success": False,
                "exit_code": -1,
                "error": f"Command timed out after {timeout_seconds} seconds.",
                "stdout": "",
                "stderr": "Execution timeout"
            }
    except Exception as e:
        return {
            "success": False,
            "exit_code": -1,
            "error": str(e),
            "stdout": "",
            "stderr": str(e)
        }
