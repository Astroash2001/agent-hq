import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Set

from fastapi import (FastAPI, File, HTTPException, Request, UploadFile,
                     WebSocket, WebSocketDisconnect, status)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

import api_guard
import api_missions
import attachments
import pricing
from config import settings
from event_bus import event_bus
from memory import memory
from office import office
from orchestrator import orchestrator
from schemas import Event, EventType

# Configure Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("AgentHQ.Server")


class ConnectionManager:
    """Manages active WebSocket connections and broadcasts events."""

    def __init__(self):
        self.active_connections: Set[WebSocket] = set()

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.add(websocket)
        logger.info(f"WebSocket client connected. Total clients: {len(self.active_connections)}")

    def disconnect(self, websocket: WebSocket):
        self.active_connections.discard(websocket)
        logger.info(f"WebSocket client disconnected. Total clients: {len(self.active_connections)}")

    async def broadcast_event(self, event: Event):
        """Send an Event to all connected browser clients."""
        if not self.active_connections:
            return

        payload = event.model_dump()
        dead_connections = set()

        for connection in self.active_connections:
            try:
                await connection.send_json(payload)
            except Exception as e:
                logger.debug(f"Failed to send to client: {e}")
                dead_connections.add(connection)

        for dead in dead_connections:
            self.disconnect(dead)


manager = ConnectionManager()


# Register the WebSocket broadcaster to EventBus
async def on_bus_event(event: Event):
    await manager.broadcast_event(event)

event_bus.subscribe_all(on_bus_event)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifecycle manager for startup and shutdown."""
    logger.info("AgentHQ Backend Server starting up...")
    api_guard.check_startup()
    # Before anything runs, say whether the models this office is configured to
    # use can be priced at all. An unpriced model reports $0.0000 everywhere --
    # cost panel, mission.json, eval budgets -- and a harness that sells "you
    # can price a run" reporting every run as free is the worst kind of quiet.
    pricing.warn_if_configured_models_are_unpriced()
    yield
    logger.info("AgentHQ Backend Server shutting down...")


# Refuse an open production server before binding a port, not after the first
# request: lifespan runs the same check for servers started some other way.
api_guard.check_startup()

_production = settings.environment == "production"
app = FastAPI(
    title=settings.app_name,
    version="1.0.0",
    lifespan=lifespan,
    # The interactive docs describe every route to whoever finds the URL.
    docs_url=None if _production else "/docs",
    redoc_url=None if _production else "/redoc",
    openapi_url=None if _production else "/openapi.json",
)

# Token on every /api/ route but the health check. Added before CORS, so CORS
# wraps it and a 401 still carries the headers a browser needs to read it.
app.middleware("http")(api_guard.require_token_middleware)

# Enable CORS
# Browsers reject allow_credentials=True with wildcard allow_origins=["*"].
# We set allow_credentials only when origins are explicitly specified.
cors_origins = getattr(settings, "cors_origins", ["http://localhost:8080"])
allow_creds = cors_origins != ["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=allow_creds,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Mission history, resume and traces live in their own router rather than as
# four more handlers here. See api_missions.py.
app.include_router(api_missions.router)


@app.get("/api/health")
async def health_check():
    return {
        "status": "healthy",
        "app": settings.app_name,
        "agents_count": len(orchestrator.get_manifests())
    }


@app.get("/api/agents")
async def get_agents():
    """Return all configured agent manifests for canvas initialization."""
    return orchestrator.get_manifests()


@app.get("/api/history")
async def get_history():
    """Return recent event history."""
    return [e.model_dump() for e in event_bus.get_history(limit=50)]


@app.get("/api/office")
async def get_office():
    """
    Floor geometry and live occupancy.

    The frontend reads stations from here rather than hardcoding a second copy:
    a tool is only callable at its station, so where the stations are is backend
    state, not decoration.
    """
    return office.snapshot()


@app.get("/api/plan")
async def get_plan():
    """The execution graph for the mission currently in flight, if any."""
    if not orchestrator.current_plan:
        return {"plan": None}
    return orchestrator.current_plan.model_dump()


@app.get("/api/memory")
async def get_memory(limit: int = 25):
    """Recent episodes plus each agent's lifetime record."""
    # Both index fields are dropped: `tokens` is noise to a reader, and
    # `embedding` is 256 floats an episode that would dwarf the payload.
    episodes = [e.model_dump(exclude={"tokens", "embedding"})
                for e in memory.episodes[-limit:]]
    episodes.reverse()
    agent_ids = [m["id"] for m in orchestrator.get_manifests()]
    return {
        "total_episodes": len(memory.episodes),
        "episodes": episodes,
        "stats": {aid: memory.stats(aid) for aid in agent_ids}
    }


@app.post("/api/memory/reset")
async def reset_memory():
    """Wipe episodic memory, so routing goes back to the planner's own choices."""
    memory.clear()
    return {"status": "memory_cleared"}


@app.post("/api/attachments")
async def upload_attachment(file: UploadFile = File(...)):
    """
    Stage one file to be handed to the next mission.

    Staged rather than attached directly because that is the order the browser
    does things in: a file is chosen before the goal is submitted, and a mission
    id is not minted until dispatch.

    A rejection returns 400 with the reason as written in `attachments.py` --
    those messages are shown to whoever just dragged the file in, so they say
    why rather than "invalid file".
    """
    # Read one byte past the limit and stop. `file.read()` with no size pulled
    # the whole body into memory before `attachments.save` could refuse it.
    raw = await file.read(settings.max_attachment_bytes + 1)
    try:
        return attachments.save(file.filename or "", raw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/attachments")
async def list_attachments():
    """What is staged and waiting for the next task."""
    return {"attachments": attachments.listing()}


@app.delete("/api/attachments/{name}")
async def delete_attachment(name: str):
    """Drop a staged file the user changed their mind about."""
    if not attachments.discard(name):
        raise HTTPException(status_code=404, detail=f"no staged file '{name}'")
    return {"status": "discarded", "name": name}


@app.post("/api/emergency-stop")
async def emergency_stop(request: Request):
    """Emergency kill switch to halt all running agents."""
    # The token is checked by api_guard's middleware, like every /api/ route.
    orchestrator.cancel_all()
    await event_bus.emit(Event(
        type=EventType.SYSTEM_STATUS,
        data={"message": "EMERGENCY STOP TRIGGERED BY USER"}
    ))
    return {"status": "emergency_stop_executed"}


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """
    Bidirectional WebSocket connection for live 2D canvas, logs, DAG updates, and token telemetry.
    """
    if settings.api_token:
        # A browser cannot set headers on a WebSocket, so the socket alone also
        # accepts ?token=. REST routes do not: a query string lands in logs.
        token = (websocket.query_params.get("token", "").strip()
                 or api_guard.token_from_headers(websocket.headers))
        if not api_guard.token_matches(token):
            await websocket.close(code=1008, reason="Unauthorized: invalid or missing API token.")
            return

    await manager.connect(websocket)

    try:
        # 1. Send initial handshake with system state and manifests
        manifests = orchestrator.get_manifests()
        await websocket.send_json({
            "type": EventType.SYSTEM_STATUS.value,
            "data": {
                "message": "Connected to AgentHQ Harness",
                "agents": manifests,
                "token_budget": settings.max_tokens_budget_per_task
            }
        })

        # 2. Replay recent event history so browser displays current status immediately
        for past_event in event_bus.get_history(limit=30):
            await websocket.send_json(past_event.model_dump())

        # 3. Main client message receive loop
        last_submit_time = 0.0
        while True:
            raw_text = await websocket.receive_text()
            try:
                msg_data = json.loads(raw_text)
                action = msg_data.get("action")

                if action == "submit_task":
                    rate_limit = getattr(settings, "ws_submit_rate_limit", 1.0)
                    now = time.monotonic()
                    if rate_limit > 0 and (now - last_submit_time < rate_limit):
                        logger.warning("Rate limit exceeded for submit_task on websocket")
                        await websocket.send_json({
                            "type": EventType.SYSTEM_STATUS.value,
                            "data": {
                                "message": (
                                    f"Rate limit exceeded. Please wait {rate_limit:.1f}s "
                                    "between task submissions."
                                )
                            }
                        })
                        continue
                    last_submit_time = now

                    prompt = msg_data.get("message", "").strip()
                    # Names only. The bytes are already staged on disk, and
                    # sending a file over the event socket would block the loop
                    # that every agent's status update shares.
                    attached = [str(a) for a in (msg_data.get("attachments") or [])]
                    if prompt:
                        # Run as a task so the socket loop does not block, and
                        # inside a mission slot so a second connection cannot
                        # start a second bill.
                        started = api_guard.mission_slots.start(
                            orchestrator.handle_user_input(prompt, attachments=attached))
                        if started is None:
                            await websocket.send_json({
                                "type": EventType.SYSTEM_STATUS.value,
                                "data": {"message": (
                                    "A mission is already running. Wait for it to "
                                    "finish before submitting another.")}
                            })
                            continue
                        logger.info("Received user task via WebSocket: %s%s", prompt,
                                    f" (+{len(attached)} attachment(s))" if attached else "")

                elif action == "emergency_stop":
                    logger.warning("Emergency stop requested via WebSocket!")
                    orchestrator.cancel_all()

                elif action == "ping":
                    await websocket.send_json({"type": "pong"})

            except json.JSONDecodeError:
                logger.error(f"Invalid JSON received from WebSocket: {raw_text}")

    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
        manager.disconnect(websocket)


# Mount static files for the frontend UI (HTML, CSS, JS)
frontend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "frontend"))
if os.path.exists(frontend_dir):
    app.mount("/", StaticFiles(directory=frontend_dir, html=True), name="frontend")


if __name__ == "__main__":
    import uvicorn

    # Reload is a development convenience and is actively wrong in a container:
    # the source cannot change there, `watchfiles` is deliberately not a runtime
    # dependency so uvicorn falls back to stat-polling every file in the image,
    # and the extra reloader process makes a crashed app look like a restarting
    # one. docker-compose.yml sets ENVIRONMENT=production; a local `python
    # main.py` is unchanged, because `environment` defaults to development.
    reload = settings.environment != "production"
    uvicorn.run("main:app", host=settings.host, port=settings.port, reload=reload)
