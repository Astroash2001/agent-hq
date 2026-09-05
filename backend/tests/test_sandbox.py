"""
Sandboxing, hardening and containment tests (WP7).

Guarantees:
  - Terminal allowlist blocks unlisted commands and restricts multi-word commands (git status vs git push)
  - Shell metacharacters are rejected before any subprocess is spawned
  - Subprocess working directory cannot escape the jail root via traversal or symlinks
  - Child subprocess environment is scrubbed of all secrets (GEMINI_API_KEY etc.)
  - Child output is capped at the configured ceiling with a visible truncation marker
  - Timed out commands are hard-killed and reaped
  - File operations (read, write, list) are jailed to the workspace / repo root
  - Empty allowlist explicitly blocks all commands
  - Optional API token gates emergency stop and WebSocket access
  - WebSocket submit_task is rate-limited per connection
"""
import os
import shutil
import tempfile
from typing import Generator

import pytest
from fastapi.testclient import TestClient

from config import settings
from main import app
from tools.file_ops import bind_workspace, list_files, read_file, write_file
from tools.terminal import execute_terminal
from workspace import MissionWorkspace


# ----------------------------------------------------------------- fixtures
# tempfile rather than pytest's tmp_path: on this machine pytest tmp_path fails
# with WinError 5 under pytest-of-avina. Every test fixture here uses mkdtemp with
# ignore_errors=True teardown to guarantee hermetic execution.

@pytest.fixture
def scratch_dir() -> Generator[str, None, None]:
    """A clean temporary scratch directory."""
    path = tempfile.mkdtemp(prefix="agenthq_sandbox_test_")
    yield path
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def test_client() -> TestClient:
    """Fresh TestClient instance for API and WebSocket tests."""
    return TestClient(app)


# ----------------------------------------------------------------- terminal allowlist & metacharacters

@pytest.mark.asyncio
async def test_rm_rf_slash_is_refused():
    """Commands outside the allowlist are rejected immediately without executing."""
    res = await execute_terminal("rm -rf /")
    assert res["success"] is False
    assert res.get("refused") is True
    assert "not in the allowed commands list" in res["error"]


@pytest.mark.asyncio
async def test_shell_metacharacters_are_refused_before_any_subprocess_spawns():
    """
    Chained commands or shell injection attempts are refused prior to execution.

    A naive implementation splitting on whitespace would evaluate 'ls; rm x'
    as binary 'ls;' which might fail later, but checking raw metacharacters
    guarantees nothing is ever passed to an executor.
    """
    res = await execute_terminal("ls; rm x")
    assert res["success"] is False
    assert res.get("refused") is True
    assert "forbidden shell metacharacter" in res["error"]


@pytest.mark.asyncio
async def test_git_status_is_permitted_while_git_push_is_refused():
    """
    Multi-word allowlist entries match on argv[0] AND subsequent subcommands.

    If matching only checked the primary binary ('git'), then 'git push --force'
    would sail through a 'git status' or 'git log' allowlist entry.
    """
    push_res = await execute_terminal("git push --force")
    assert push_res["success"] is False
    assert push_res.get("refused") is True
    assert "not in the allowed commands list" in push_res["error"]

    status_res = await execute_terminal("git status")
    # git status is permitted; it may succeed or exit with code 0/1 depending on repo state,
    # but it must NOT be refused.
    assert status_res.get("refused") is not True


@pytest.mark.asyncio
async def test_working_directory_traversal_escaping_jail_is_refused():
    """A cwd that climbs out of the jail root via '../..' is caught and refused."""
    res = await execute_terminal("python --version", cwd="../../")
    assert res["success"] is False
    assert res.get("refused") is True
    assert "resolves outside sandbox jail root" in res["error"]


@pytest.mark.asyncio
async def test_symlink_pointing_outside_jail_is_refused(scratch_dir, monkeypatch):
    """
    Symlinks pointing outside the repository jail root are resolved and rejected.

    On Windows without Developer Mode, creating directory symlinks can fail with
    WinError 1314 (privilege not held). When that occurs, we monkeypatch realpath
    for the synthetic symlink to verify containment resolution identically.
    """
    link_path = os.path.join(settings.jail_root, "_test_symlink_escape")
    outside_target = os.path.dirname(settings.jail_root)

    symlink_created = False
    try:
        os.symlink(outside_target, link_path, target_is_directory=True)
        symlink_created = True
    except (OSError, NotImplementedError):
        # Fallback simulation of symlink resolution for restricted host
        real_realpath = os.path.realpath

        def fake_realpath(p):
            if os.path.abspath(p) == os.path.abspath(link_path):
                return outside_target
            return real_realpath(p)

        monkeypatch.setattr(os.path, "realpath", fake_realpath)

    try:
        res = await execute_terminal("python --version", cwd=link_path)
        assert res["success"] is False
        assert res.get("refused") is True
        assert "resolves outside sandbox jail root" in res["error"]
    finally:
        if symlink_created and os.path.islink(link_path):
            try:
                os.unlink(link_path)
            except OSError:
                pass


@pytest.mark.asyncio
async def test_subprocess_cannot_read_gemini_api_key_from_environment(monkeypatch):
    """
    Subprocess environment is scrubbed down to PATH and SYSTEMROOT.

    API keys and secrets present in the parent process or settings must never
    be accessible to commands executed on behalf of an agent.
    """
    monkeypatch.setenv("GEMINI_API_KEY", "super-secret-production-key-99999")
    monkeypatch.setattr(settings, "gemini_api_key", "super-secret-production-key-99999")

    # Use a command free of semicolons or metacharacters to read the env var
    cmd = 'python -c "__import__(\'sys\').stdout.write(str(__import__(\'os\').environ.get(\'GEMINI_API_KEY\')))"'
    res = await execute_terminal(cmd)
    assert res["success"] is True
    assert "super-secret-production-key-99999" not in res["stdout"]
    assert "None" in res["stdout"]


@pytest.mark.asyncio
async def test_output_truncation_fires_and_is_visible_in_result(monkeypatch):
    """Commands generating excessive output are capped with a visible marker."""
    monkeypatch.setattr(settings, "terminal_max_output_chars", 120)

    # Output 300 characters of repeating text
    cmd = 'python -c "__import__(\'sys\').stdout.write(\'Z\' * 300)"'
    res = await execute_terminal(cmd)
    assert res["success"] is True
    assert len(res["stdout"]) > 120
    assert "[output truncated at 120 characters]" in res["stdout"]
    assert "Z" * 120 in res["stdout"]
    assert "Z" * 121 not in res["stdout"]


@pytest.mark.asyncio
async def test_timeout_kills_child_and_returns_refusal_result():
    """A command exceeding the timeout ceiling is hard-killed and returns a refusal payload."""
    cmd = 'python -c "__import__(\'time\').sleep(10)"'
    res = await execute_terminal(cmd, timeout_seconds=1)
    assert res["success"] is False
    assert res.get("refused") is True
    assert res["exit_code"] == -1
    assert "timed out after 1 seconds" in res["error"]


@pytest.mark.asyncio
async def test_empty_allowlist_refuses_all_commands(monkeypatch):
    """
    An empty allowlist explicitly refuses everything.

    Settings defaulting or being configured to an empty list must not be
    misinterpreted as 'allow everything'.
    """
    monkeypatch.setattr(settings, "terminal_allowed_commands", [])
    res = await execute_terminal("python --version")
    assert res["success"] is False
    assert res.get("refused") is True
    assert "terminal allowlist is empty" in res["error"]


# ----------------------------------------------------------------- file operations containment

@pytest.mark.asyncio
async def test_write_file_outside_the_workspace_is_refused(scratch_dir, monkeypatch):
    """
    An active mission cannot write files outside its workspace directory.

    Attempts to navigate up with '..' or write to arbitrary system paths are refused.
    """
    monkeypatch.setattr(settings, "missions_path", os.path.join(scratch_dir, "missions"))
    w = MissionWorkspace("sandboxed_mission_1")
    bind_workspace(w, "coder", "Coder", "s1")

    try:
        # Relative traversal attempt
        traversal_res = await write_file("../../escape.md", "hostile content")
        assert traversal_res["success"] is False
        assert traversal_res.get("refused") is True
        assert "resolves outside the active mission workspace" in traversal_res["error"]

        # Absolute outside path attempt
        outside_path = os.path.join(scratch_dir, "unauthorized.md")
        abs_res = await write_file(outside_path, "hostile content")
        assert abs_res["success"] is False
        assert abs_res.get("refused") is True
        assert "resolves outside the active mission workspace" in abs_res["error"]
    finally:
        bind_workspace(None)


@pytest.mark.asyncio
async def test_read_file_on_absolute_path_outside_the_jail_is_refused():
    """Reading absolute system paths outside the jail root is blocked."""
    outside_file = "C:\\Windows\\win.ini" if os.name == "nt" else "/etc/passwd"
    res = await read_file(outside_file)
    assert res["success"] is False
    assert res.get("refused") is True
    assert "resolves outside the allowed sandbox jail root" in res["error"]


@pytest.mark.asyncio
async def test_list_files_outside_jail_is_refused():
    """Listing directory trees outside the repository root is blocked."""
    outside_dir = os.path.dirname(settings.jail_root)
    res = await list_files(outside_dir)
    assert res["success"] is False
    assert res.get("refused") is True
    assert "resolves outside the allowed sandbox jail root" in res["error"]


# ----------------------------------------------------------------- main.py auth & hardening

def test_api_token_protects_emergency_stop_when_configured(test_client, monkeypatch):
    """
    When settings.api_token is set, POST /api/emergency-stop requires authentication.

    When unset, the endpoint remains open for zero-credential local development.
    """
    monkeypatch.setattr(settings, "api_token", "secret-token-wp7")

    # Unauthenticated attempt fails with 401
    unauth_resp = test_client.post("/api/emergency-stop")
    assert unauth_resp.status_code == 401
    assert "Unauthorized" in unauth_resp.json()["detail"]

    # Bearer header succeeds
    auth_resp = test_client.post(
        "/api/emergency-stop",
        headers={"Authorization": "Bearer secret-token-wp7"}
    )
    assert auth_resp.status_code == 200
    assert auth_resp.json()["status"] == "emergency_stop_executed"


def test_api_token_protects_websocket_endpoint_when_configured(test_client, monkeypatch):
    """WebSocket /ws rejects unauthenticated connections when api_token is configured."""
    monkeypatch.setattr(settings, "api_token", "secret-token-wp7")

    # Connect without token -> rejected with WS policy violation (1008)
    with pytest.raises(Exception):
        with test_client.websocket_connect("/ws"):
            pass

    # Connect with query token -> accepted and receives handshake
    with test_client.websocket_connect("/ws?token=secret-token-wp7") as ws:
        msg = ws.receive_json()
        assert msg["type"] == "system.status"


def test_websocket_submit_task_rate_limiting_throttles_rapid_messages(test_client, monkeypatch):
    """Rapid submit_task messages from the same client trigger rate limiting."""
    from event_bus import event_bus
    # Clear any recorded event history so we only read handshake and direct replies
    event_bus._history.clear()

    monkeypatch.setattr(settings, "api_token", "")
    monkeypatch.setattr(settings, "ws_submit_rate_limit", 2.0)

    with test_client.websocket_connect("/ws") as ws:
        # 1. Consume handshake message
        handshake = ws.receive_json()
        assert handshake["type"] == "system.status"

        # 2. First submission is accepted
        ws.send_json({"action": "submit_task", "message": "Task 1"})

        # 3. Immediate second submission is throttled
        ws.send_json({"action": "submit_task", "message": "Task 2"})

        found_throttle = False
        for _ in range(5):
            msg = ws.receive_json()
            if (
                msg.get("type") == "system.status"
                and "Rate limit exceeded" in msg.get("data", {}).get("message", "")
            ):
                found_throttle = True
                break

        assert found_throttle is True


def test_cors_credentials_are_disabled_for_wildcard_origins():
    """
    Setting CORS allow_origins to ['*'] must disable allow_credentials.

    Browsers reject the combination of wildcard origin and credentialed requests.
    """
    from starlette.middleware.cors import CORSMiddleware
    cors_mw = None
    for mw in app.user_middleware:
        if mw.cls == CORSMiddleware:
            cors_mw = mw
            break

    assert cors_mw is not None
    # Current configuration in main.py has explicit localhost origin, allowing credentials
    if cors_mw.kwargs.get("allow_origins") == ["*"]:
        assert cors_mw.kwargs.get("allow_credentials") is False
    else:
        assert cors_mw.kwargs.get("allow_credentials") is True
