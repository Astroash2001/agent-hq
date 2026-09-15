"""
Sandboxing, hardening and containment tests (WP7).

Guarantees:
  - No registered tool, and no agent manifest, can run code
  - File operations (read, write, list) are jailed to the workspace / repo root
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


# ----------------------------------------------------------------- no code execution

def test_no_tool_in_the_catalogue_runs_code():
    """
    `terminal` and `code_analysis` were removed, not merely ungranted. Either
    one let a model run arbitrary code as the server's user -- a script opens
    `.env` itself, and the file sandbox cannot see that. The catalogue is what
    the planner offers and the foundry hires from, so absent here means no
    agent, staff or contractor, can be given one.
    """
    from tool_registry import tool_registry

    catalogue = tool_registry.catalogue()
    assert "terminal" not in catalogue
    assert "code_analysis" not in catalogue
    # "execute" is still a risk class -- fetch_url reaches an address chosen at
    # runtime -- but nothing registered runs code.
    runners = {"terminal", "code_analysis", "run_python", "shell", "exec"}
    assert not runners & set(catalogue)


@pytest.mark.asyncio
async def test_calling_a_removed_execution_tool_is_refused():
    from tool_registry import tool_registry

    for name, args in (("terminal", {"command": "python --version"}),
                       ("code_analysis", {"code_snippet": "print(1)"})):
        res = await tool_registry.execute(name, args, agent_id="coder",
                                          granted=[name])
        assert res["success"] is False
        assert "not registered" in res["error"]


def test_no_staff_manifest_grants_an_execution_tool():
    import json

    for filename in os.listdir(settings.agents_dir):
        if not filename.endswith(".json"):
            continue
        with open(os.path.join(settings.agents_dir, filename), encoding="utf-8") as fh:
            tools = set(json.load(fh).get("tools", []))
        assert not {"terminal", "code_analysis"} & tools, filename


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

    # The accepted submission must not start a real mission: that called the
    # configured model with real keys and wrote into the real missions folder.
    from orchestrator import orchestrator

    async def no_mission(*args, **kwargs):
        return None

    monkeypatch.setattr(orchestrator, "handle_user_input", no_mission)

    with test_client.websocket_connect("/ws") as ws:
        # 1. Consume handshake message
        handshake = ws.receive_json()
        assert handshake["type"] == "system.status"

        # 2. First submission is accepted
        ws.send_json({"action": "submit_task", "message": "Task 1"})

        # 3. Immediate second submission is throttled
        ws.send_json({"action": "submit_task", "message": "Task 2"})

        # The accepted mission streams its own events (task.created, walks,
        # speech) ahead of the reply, and how many land first depends on
        # scheduling -- so read past them rather than counting on five.
        found_throttle = False
        for _ in range(40):
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
