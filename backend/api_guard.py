"""
Who may use the API, and how much of it at once.

A deployed office is a public URL attached to somebody's model keys. Before
this, the token in `settings.api_token` guarded two things -- the kill switch
and the socket -- and was optional even for those. Everything else was open:
resuming a mission (which spends), wiping memory, uploading files, and reading
every mission's plan, trace and artifacts. And with no token set, which is the
default, the socket was open too, so anyone who found the URL could run
missions on the owner's bill.

Three rules, all here so there is one place to read them:

1. **Every `/api/` route needs the token, except `/api/health`.** The health
   check is what the host polls, and the static frontend is not under `/api/`.
   REST takes the token from a header only -- a query string lands in access
   logs. The socket also accepts `?token=`, because a browser cannot set
   headers on a WebSocket.
2. **Production refuses to start without a token** (`check_startup`). An open
   deployment is the failure; making it impossible by accident is the fix.
   `allow_unauthenticated=true` is the explicit, logged way out.
3. **One mission at a time by default** (`MissionSlots`). A per-connection rate
   limit does nothing against a second connection, and each mission is real
   spend.

With no token configured outside production, nothing changes: a local clone
works exactly as it did.
"""
import asyncio
import hmac
import logging
import re
from typing import Optional

from fastapi import HTTPException, Request, status
from fastapi.responses import JSONResponse

from config import settings

logger = logging.getLogger("AgentHQ.APIGuard")

# Routes that answer without a token. Kept to what a host's health probe needs.
PUBLIC_API_PATHS = {"/api/health"}

# What a mission id may look like: the orchestrator mints `task_<slug>_<hex>`.
# Checked before an id reaches the filesystem, because a path segment of `..`
# (sent as `%2e%2e`) otherwise names the directory above every mission.
_MISSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]{0,199}$")


def token_matches(provided: Optional[str]) -> bool:
    """Constant-time comparison against the configured token."""
    expected = settings.api_token or ""
    if not expected:
        return True
    return bool(provided) and hmac.compare_digest(provided.encode(), expected.encode())


def token_from_headers(headers) -> str:
    """`Authorization: Bearer <t>` or `X-API-Token: <t>`."""
    auth = (headers.get("authorization") or "").strip()
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return (headers.get("x-api-token") or "").strip()


def check_startup() -> None:
    """Refuse to serve a production deployment that anyone could use."""
    if settings.environment != "production" or settings.api_token:
        return
    if settings.allow_unauthenticated:
        logger.warning(
            "ENVIRONMENT=production with no AGENTHQ_API_TOKEN and "
            "ALLOW_UNAUTHENTICATED=true: every API route is open to anyone who "
            "can reach this server, including running missions on your keys.")
        return
    raise RuntimeError(
        "Refusing to start: ENVIRONMENT=production but AGENTHQ_API_TOKEN is not "
        "set, so anyone who can reach this server could run missions with your "
        "model keys. Set AGENTHQ_API_TOKEN, or set ALLOW_UNAUTHENTICATED=true if "
        "this server is only reachable by you.")


async def require_token_middleware(request: Request, call_next):
    """Gate every `/api/` route but the health check."""
    path = request.url.path
    if (path.startswith("/api/") or path == "/api") and path not in PUBLIC_API_PATHS:
        # CORS preflight carries no credentials by design; the real request does.
        if request.method != "OPTIONS" and not token_matches(token_from_headers(request.headers)):
            return JSONResponse(
                status_code=status.HTTP_401_UNAUTHORIZED,
                content={"detail": "Unauthorized: invalid or missing API token."})
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("X-Frame-Options", "DENY")
    return response


def safe_mission_id(mission_id: str) -> str:
    """The id, or a 400 if it could name anything but one mission directory."""
    if not _MISSION_ID.match(mission_id or ""):
        raise HTTPException(status_code=400, detail="invalid mission id")
    return mission_id


class MissionSlots:
    """
    A cap on missions running at once, across every connection and endpoint.

    `orchestrator.active_tasks` is never pruned, so it cannot be the count; this
    is incremented when a mission is admitted and decremented when it ends,
    however it ends.
    """

    def __init__(self):
        self._running = 0

    @property
    def running(self) -> int:
        return self._running

    def try_acquire(self) -> bool:
        limit = settings.max_concurrent_missions
        if limit > 0 and self._running >= limit:
            return False
        self._running += 1
        return True

    def release(self) -> None:
        self._running = max(0, self._running - 1)

    async def run(self, coro):
        """Await `coro` holding a slot the caller already acquired."""
        try:
            return await coro
        finally:
            self.release()

    def start(self, coro) -> Optional[asyncio.Task]:
        """Schedule `coro` in a slot, or return None when every slot is taken."""
        if not self.try_acquire():
            coro.close()
            return None
        return asyncio.create_task(self.run(coro))


mission_slots = MissionSlots()
