"""
Terminal tool execution with sandboxing and hardening.

The previous terminal implementation passed arbitrary command strings to
`asyncio.create_subprocess_shell` with no allowlist, shell metacharacter
filtering, or working directory containment. Any LLM prompt injection reaching
an agent with terminal access could execute arbitrary code with the privileges
and full environment (including all API keys) of the parent harness.

This hardened version restricts execution to an explicit allowlist, forbids
shell metacharacters outright, jails the working directory to the repository
root, scrubs secrets from the child environment, caps output, and hard-kills
timed-out processes.
"""
import asyncio
import os
import shlex
import shutil
from typing import Any, Dict, List, Optional

from config import settings  # type: ignore

# ----------------------------------------------------------------- metacharacters
# Metacharacters that enable command chaining, pipeline execution, file redirection,
# or subshell expansion. Checking raw command text before splitting prevents
# shell injection vectors entirely since commands are executed with shell=False.
FORBIDDEN_METACHARACTERS: List[str] = [
    ";",
    "&",
    "|",
    "`",
    "$(",
    ">",
    "<",
    "\n",
    "\r",
]


async def execute_terminal(
    command: str,
    cwd: Optional[str] = None,
    timeout_seconds: int = 15
) -> Dict[str, Any]:
    """
    Execute an allowlisted command asynchronously with containment and output capping.

    Guardrails:
      - Command allowlist matching on binary basename and subcommand
      - Rejection of shell metacharacters
      - Working directory jailed to settings.jail_root
      - Scrubbed child environment (PATH and SYSTEMROOT only)
      - Hard timeout killing and reaping the child
      - Output capped at settings.terminal_max_output_chars
    """
    # ------------------------------------------------------------- kill switch
    # When disabled via settings, retain execution but log warning.
    if not getattr(settings, "terminal_sandbox_enabled", True):
        return await _execute_unconstrained(command, cwd, timeout_seconds)

    # ------------------------------------------------------------- validation
    stripped_cmd = (command or "").strip()
    if not stripped_cmd:
        return {
            "success": False,
            "refused": True,
            "error": "Command is empty or whitespace.",
        }

    for meta in FORBIDDEN_METACHARACTERS:
        if meta in stripped_cmd:
            return {
                "success": False,
                "refused": True,
                "error": f"Command contains forbidden shell metacharacter: '{meta}'.",
            }

    # ------------------------------------------------------------- argument parsing
    # Parse with posix=False so Windows backslashes in paths are not swallowed
    # as escape characters.
    try:
        argv = shlex.split(stripped_cmd, posix=False)
    except Exception as exc:
        return {
            "success": False,
            "refused": True,
            "error": f"Failed to parse command arguments: {exc}",
        }

    if not argv:
        return {
            "success": False,
            "refused": True,
            "error": "Command parsed to empty argument list.",
        }

    # posix=False preserves Windows backslashes in paths, but keeps surrounding
    # quotes on quoted tokens. Strip matching enclosing quotes so subprocess_exec
    # and list2cmdline don't escape them a second time.
    clean_argv: List[str] = []
    for arg in argv:
        if (arg.startswith('"') and arg.endswith('"')) or (arg.startswith("'") and arg.endswith("'")):
            clean_argv.append(arg[1:-1])
        else:
            clean_argv.append(arg)
    argv = clean_argv

    # ------------------------------------------------------------- working directory jail
    target_dir = cwd or os.getcwd()
    jail_root = os.path.realpath(os.path.abspath(settings.jail_root))
    try:
        real_target = os.path.realpath(os.path.abspath(target_dir))
        # commonpath raises ValueError on different Windows drives or invalid paths
        is_contained = (os.path.commonpath([jail_root, real_target]) == jail_root)
    except (ValueError, OSError):
        is_contained = False

    if not is_contained:
        return {
            "success": False,
            "refused": True,
            "error": (
                f"Working directory '{target_dir}' resolves outside sandbox jail root "
                f"('{jail_root}')."
            ),
        }

    if not os.path.exists(real_target):
        return {
            "success": False,
            "refused": True,
            "error": f"Working directory '{target_dir}' does not exist.",
        }

    # ------------------------------------------------------------- allowlist matching
    # Empty allowlist explicitly means refuse everything, never allow everything.
    allowed_list = getattr(settings, "terminal_allowed_commands", [])
    if not allowed_list:
        return {
            "success": False,
            "refused": True,
            "error": f"Command '{argv[0]}' refused: terminal allowlist is empty.",
        }

    raw_prog = argv[0]
    base_prog = os.path.basename(raw_prog).lower()
    if base_prog.endswith(".exe"):
        base_prog = base_prog[:-4]

    matched = False
    for entry in allowed_list:
        parts = entry.strip().split()
        if not parts:
            continue
        entry_base = os.path.basename(parts[0]).lower()
        if entry_base.endswith(".exe"):
            entry_base = entry_base[:-4]

        if base_prog == entry_base:
            if len(parts) == 1:
                # Single-word entry (e.g. "python", "pytest") allows any subcommands
                matched = True
                break
            else:
                # Multi-word entry (e.g. "git status", "git diff")
                # argv[0] and subsequent entry subcommands must match exactly.
                if len(argv) >= len(parts):
                    if all(argv[i].lower() == parts[i].lower() for i in range(1, len(parts))):
                        matched = True
                        break

    if not matched:
        allowed_repr = ", ".join(repr(c) for c in allowed_list)
        return {
            "success": False,
            "refused": True,
            "error": (
                f"Command '{stripped_cmd}' is not in the allowed commands list: "
                f"[{allowed_repr}]."
            ),
        }

    # ------------------------------------------------------------- executable resolution
    executable = shutil.which(argv[0])
    if not executable:
        return {
            "success": False,
            "refused": True,
            "error": (
                f"Executable '{argv[0]}' not found on PATH. Shell builtins like 'dir' "
                f"or 'type' have no binary on disk and cannot be run without a shell."
            ),
        }

    # ------------------------------------------------------------- environment scrubbing
    # Only forward PATH and SYSTEMROOT. Windows fails to initialise many standard
    # binaries without SYSTEMROOT. No LLM credentials or private env vars may reach
    # child processes.
    child_env = {}
    for key in ("PATH", "SYSTEMROOT"):
        val = os.environ.get(key)
        if val:
            child_env[key] = val

    # ------------------------------------------------------------- subprocess execution
    try:
        process = await asyncio.create_subprocess_exec(
            executable,
            *argv[1:],
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=real_target,
            env=child_env,
        )

        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                process.communicate(), timeout=timeout_seconds
            )
            out_str = stdout_b.decode("utf-8", errors="replace").strip()
            err_str = stderr_b.decode("utf-8", errors="replace").strip()

            max_chars = getattr(settings, "terminal_max_output_chars", 5000)
            if len(out_str) > max_chars:
                out_str = (
                    out_str[:max_chars]
                    + f"\n... [output truncated at {max_chars} characters]"
                )
            if len(err_str) > max_chars:
                err_str = (
                    err_str[:max_chars]
                    + f"\n... [output truncated at {max_chars} characters]"
                )

            return {
                "success": process.returncode == 0,
                "exit_code": process.returncode,
                "stdout": out_str,
                "stderr": err_str,
            }
        except asyncio.TimeoutError:
            try:
                process.kill()
                await asyncio.wait_for(process.communicate(), timeout=2.0)
            except Exception:
                pass
            return {
                "success": False,
                "refused": True,
                "exit_code": -1,
                "error": f"Command timed out after {timeout_seconds} seconds and was hard-killed.",
                "stdout": "",
                "stderr": f"Execution timeout after {timeout_seconds}s",
            }
    except Exception as e:
        return {
            "success": False,
            "refused": False,
            "exit_code": -1,
            "error": str(e),
            "stdout": "",
            "stderr": str(e),
        }


async def _execute_unconstrained(
    command: str,
    cwd: Optional[str] = None,
    timeout_seconds: int = 15
) -> Dict[str, Any]:
    """Fallback execution path used only when terminal_sandbox_enabled is False."""
    work_dir = cwd or os.getcwd()
    try:
        process = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=work_dir,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=timeout_seconds
            )
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
                "stderr": "Execution timeout",
            }
    except Exception as e:
        return {
            "success": False,
            "exit_code": -1,
            "error": str(e),
            "stdout": "",
            "stderr": str(e),
        }
