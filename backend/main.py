import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Set

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

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
    yield
    logger.info("AgentHQ Backend Server shutting down...")


app = FastAPI(
    title=settings.app_name,
    version="1.0.0",
    lifespan=lifespan
)

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
    episodes = [e.model_dump(exclude={"tokens"}) for e in memory.episodes[-limit:]]
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


@app.post("/api/emergency-stop")
async def emergency_stop(request: Request):
    """Emergency kill switch to halt all running agents."""
    expected_token = getattr(settings, "api_token", "")
    if expected_token:
        auth_header = request.headers.get("authorization", "").strip()
        token = ""
        if auth_header.startswith("Bearer "):
            token = auth_header[7:].strip()
        elif auth_header:
            token = auth_header
        if not token:
            token = request.headers.get("x-api-token", "").strip()
        if not token:
            token = request.query_params.get("token", "").strip()

        if token != expected_token:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Unauthorized: invalid or missing API token."
            )

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
    expected_token = getattr(settings, "api_token", "")
    if expected_token:
        token = websocket.query_params.get("token", "").strip()
        if not token:
            auth_header = websocket.headers.get("authorization", "").strip()
            if auth_header.startswith("Bearer "):
                token = auth_header[7:].strip()
            elif auth_header:
                token = auth_header
        if not token:
            token = websocket.headers.get("x-api-token", "").strip()

        if token != expected_token:
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
                    if prompt:
                        logger.info(f"Received user task via WebSocket: {prompt}")
                        # Run orchestrator as an async task so WebSocket loop doesn't block!
                        asyncio.create_task(orchestrator.handle_user_input(prompt))

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
    uvicorn.run("main:app", host=settings.host, port=settings.port, reload=True)
