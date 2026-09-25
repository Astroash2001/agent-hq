"""
Which browser is asking.

One API token is shared by everyone a deployment is handed to, so the token
alone cannot say whose mission is whose -- and before this module, it did not
matter: every event went to every socket, every mission was listed to every
caller, and one person's staged attachment sat in the same tray as another's.

Each browser now mints a random session id, keeps it in localStorage, and sends
it with every request (`X-Session-Id`) and on the socket (`?session=`). Anything
a mission produces is stamped with the id of the session that started it, and
every read is filtered to the caller's own.

The id is carried in a ContextVar rather than threaded through every call. A
mission runs as an asyncio task created inside the socket handler, and a task
copies the context it was created in -- so every event, episode and run-store
row a mission writes, however deep, sees the session that started it without a
single signature changing.

This is separation, not authentication. The id is an unguessable bearer value,
so one user cannot read another's work without being handed their id; but
anyone holding the API token can still run missions and press the emergency
stop. A caller that sends no id at all (curl, the tests, a stale cached UI) is
the `None` session, which is also who owns everything recorded before this
existed.
"""
import re
import secrets
from contextvars import ContextVar
from typing import Optional

from fastapi import Request
from fastapi.responses import JSONResponse

HEADER = "X-Session-Id"
QUERY_PARAM = "session"

# Long enough to be unguessable, and a shape that is safe as a directory name:
# attachments are staged under a folder named after the session.
_SHAPE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")

_current: ContextVar[Optional[str]] = ContextVar("agenthq_session", default=None)


def valid(raw: Optional[str]) -> bool:
    return bool(raw) and bool(_SHAPE.match(raw))


def new() -> str:
    """A fresh id, for a socket that arrived without one."""
    return secrets.token_urlsafe(24)


def current() -> Optional[str]:
    """The session the running code is serving, or None."""
    return _current.get()


def set_current(session_id: Optional[str]):
    """Bind a session to this context. Returns the token `ContextVar.reset` takes."""
    return _current.set(session_id)


def owns(owner: Optional[str]) -> bool:
    """
    Whether the current session may see something recorded under `owner`.

    Exact match, None included: a header-less caller sees only what was
    recorded header-less, and a browser never sees another browser's work or
    the unowned history that predates sessions.
    """
    return owner == current()


async def middleware(request: Request, call_next):
    """
    Bind the caller's session for the length of one HTTP request.

    A malformed id is refused rather than dropped. Dropped, it would fall into
    the `None` session and see everything recorded before sessions existed.
    """
    raw = request.headers.get(HEADER)
    if raw is not None and not valid(raw.strip()):
        return JSONResponse(status_code=400,
                            content={"detail": f"malformed {HEADER} header"})
    token = _current.set(raw.strip() if raw else None)
    try:
        return await call_next(request)
    finally:
        _current.reset(token)
