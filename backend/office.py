"""
Spatial runtime -- the floor plan as an execution constraint, not a picture.

Tools are not free-floating functions any more. Each one lives at a station: the
terminal at the ops console, search at the research library, file writes at the
filing archive. To call a tool an agent has to walk to its station and hold it,
and a station holds one agent at a time. Two specialists that need the same
station serialise, and the queue that forms is real -- you can watch it.

That makes the office load-bearing. Moving a station in the map changes how long
missions take, which is not true of any layout that is only ever drawn.

Coordinates are TILE coordinates in the same 40x24 grid the renderer uses.
Seats mirror `OFFICE_MAP.seats` in `frontend/js/office_map.js`; stations are
defined here and served over `/api/office` so the frontend can read them rather
than hardcoding a second copy that drifts.
"""
import asyncio
import logging
import math
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional, Tuple

from config import settings
from event_bus import event_bus
from schemas import Event, EventType, Station

logger = logging.getLogger("AgentHQ.Office")


# Desk seats, in tile coords. Mirrors OFFICE_MAP.seats.
SEATS: Dict[str, Tuple[float, float]] = {
    "orchestrator": (32.5, 6.5),
    "researcher": (5.0, 7.9),
    "coder": (13.0, 7.9),
    "data_analyst": (21.0, 7.9),
    "creative": (5.0, 12.3),
}

# Where an agent with no desk of its own is parked.
DEFAULT_SEAT: Tuple[float, float] = (12.0, 15.0)


# The stations, placed on furniture that already exists in the map. `tools` is
# the binding that gates execution; a tool named by no station stays callable
# from the agent's desk.
STATIONS: List[Station] = [
    Station(
        id="ops_console",
        label="Ops Console",
        x=19.4, y=18.4,
        tools=["terminal", "code_analysis"],
        kind="tool",
        description="The console rig by the conference wall. Shell and AST work happens here."
    ),
    Station(
        id="research_library",
        label="Research Library",
        x=2.6, y=12.4,
        tools=["web_search", "read_file"],
        kind="tool",
        description="The west library wall. Search and reading happens at the shelves."
    ),
    Station(
        id="filing_archive",
        label="Filing Archive",
        x=2.6, y=9.4,
        tools=["write_file", "list_files"],
        kind="tool",
        description="The cabinets north of the library. Anything written to disk is filed here."
    ),
    Station(
        id="whiteboard",
        label="Whiteboard",
        x=5.2, y=5.2,
        tools=[],
        kind="planning",
        description="Where the boss decomposes a goal before dispatching it."
    ),
    Station(
        id="conference_table",
        label="Conference Room",
        x=11.0, y=19.6,
        tools=[],
        kind="meeting",
        description="Reserved for multi-agent sessions."
    ),
]


class OfficeFloor:
    """
    Tracks where every agent is, walks them around, and rations the stations.

    All the waiting is real `asyncio` waiting, so a blocked agent genuinely
    holds up its branch of the DAG while the other branches keep running.
    """

    def __init__(self):
        self._stations: Dict[str, Station] = {s.id: s for s in STATIONS}
        self._tool_index: Dict[str, str] = {}
        for station in STATIONS:
            for tool in station.tools:
                self._tool_index[tool] = station.id

        self._positions: Dict[str, Tuple[float, float]] = dict(SEATS)
        self._locks: Dict[str, asyncio.Lock] = {}
        self._waiting: Dict[str, int] = {sid: 0 for sid in self._stations}
        self._occupant: Dict[str, Optional[str]] = {sid: None for sid in self._stations}

    # ---------------------------------------------------------------- geometry

    def seat_of(self, agent_id: str) -> Tuple[float, float]:
        return SEATS.get(agent_id, DEFAULT_SEAT)

    def position_of(self, agent_id: str) -> Tuple[float, float]:
        return self._positions.get(agent_id, self.seat_of(agent_id))

    def station_for_tool(self, tool_name: str) -> Optional[Station]:
        station_id = self._tool_index.get(tool_name)
        return self._stations.get(station_id) if station_id else None

    def get_station(self, station_id: str) -> Optional[Station]:
        return self._stations.get(station_id)

    @staticmethod
    def _distance(a: Tuple[float, float], b: Tuple[float, float]) -> float:
        return math.hypot(b[0] - a[0], b[1] - a[1])

    def travel_seconds(self, frm: Tuple[float, float], to: Tuple[float, float]) -> float:
        """Walk time for a leg, clamped so a cross-office trip stays watchable."""
        raw = self._distance(frm, to) * settings.walk_seconds_per_tile
        return max(settings.min_walk_seconds, min(raw, settings.max_walk_seconds))

    # ---------------------------------------------------------------- movement

    async def walk_to(
        self,
        agent_id: str,
        target: Tuple[float, float],
        agent_name: str = "",
        label: str = "",
        station_id: str = "",
        task_id: Optional[str] = None
    ) -> float:
        """
        Move an agent and block for the travel time.

        The renderer gets the endpoints and the duration up front so it can tween
        the sprite over exactly the interval the backend is actually waiting.
        """
        origin = self.position_of(agent_id)
        if self._distance(origin, target) < 0.25:
            return 0.0

        duration = self.travel_seconds(origin, target)
        await event_bus.emit(Event(
            type=EventType.AGENT_MOVE,
            agent_id=agent_id,
            task_id=task_id,
            data={
                "agent_name": agent_name,
                "from": {"x": origin[0], "y": origin[1]},
                "to": {"x": target[0], "y": target[1]},
                "station_id": station_id,
                "label": label,
                "duration_ms": round(duration * 1000)
            }
        ))

        await asyncio.sleep(duration)
        self._positions[agent_id] = target

        await event_bus.emit(Event(
            type=EventType.AGENT_ARRIVED,
            agent_id=agent_id,
            task_id=task_id,
            data={
                "agent_name": agent_name,
                "at": {"x": target[0], "y": target[1]},
                "station_id": station_id,
                "label": label
            }
        ))
        return duration

    async def return_to_desk(self, agent_id: str, agent_name: str = "", task_id: Optional[str] = None) -> float:
        """Send an agent back to its own desk."""
        return await self.walk_to(
            agent_id, self.seat_of(agent_id),
            agent_name=agent_name, label="desk", station_id="", task_id=task_id
        )

    # ---------------------------------------------------------------- stations

    def _lock_for(self, station_id: str) -> asyncio.Lock:
        """Locks are created lazily so none of them binds to the import-time loop."""
        if station_id not in self._locks:
            self._locks[station_id] = asyncio.Lock()
        return self._locks[station_id]

    @asynccontextmanager
    async def use_tool_station(
        self,
        agent_id: str,
        tool_name: str,
        agent_name: str = "",
        task_id: Optional[str] = None
    ):
        """
        Acquire the station a tool lives at, walking there first and queueing if
        it is busy. Yields the `Station`, or None when the tool is unbound or the
        spatial runtime is switched off.

        The station is released on the way out even if the tool raised, so a
        crashing tool cannot deadlock the floor.
        """
        station = self.station_for_tool(tool_name) if settings.spatial_enabled else None
        if station is None:
            yield None
            return

        lock = self._lock_for(station.id)

        if lock.locked():
            self._waiting[station.id] += 1
            await event_bus.emit(Event(
                type=EventType.STATION_QUEUED,
                agent_id=agent_id,
                task_id=task_id,
                data={
                    "agent_name": agent_name,
                    "station_id": station.id,
                    "label": station.label,
                    "tool": tool_name,
                    "occupied_by": self._occupant.get(station.id),
                    "queue_length": self._waiting[station.id]
                }
            ))
            try:
                await asyncio.wait_for(lock.acquire(), timeout=settings.station_wait_timeout)
            except asyncio.TimeoutError:
                self._waiting[station.id] -= 1
                logger.warning(f"{agent_id} timed out waiting for station {station.id}")
                yield None
                return
            self._waiting[station.id] -= 1
        else:
            await lock.acquire()

        self._occupant[station.id] = agent_id
        try:
            await self.walk_to(
                agent_id, (station.x, station.y),
                agent_name=agent_name, label=station.label,
                station_id=station.id, task_id=task_id
            )
            await event_bus.emit(Event(
                type=EventType.STATION_ACQUIRED,
                agent_id=agent_id,
                task_id=task_id,
                data={
                    "agent_name": agent_name,
                    "station_id": station.id,
                    "label": station.label,
                    "tool": tool_name
                }
            ))
            yield station
        finally:
            self._occupant[station.id] = None
            lock.release()
            await event_bus.emit(Event(
                type=EventType.STATION_RELEASED,
                agent_id=agent_id,
                task_id=task_id,
                data={
                    "agent_name": agent_name,
                    "station_id": station.id,
                    "label": station.label,
                    "queue_length": self._waiting.get(station.id, 0)
                }
            ))

    # ---------------------------------------------------------------- reporting

    def reset(self):
        """Put everyone back at their desk. Called when a mission finishes."""
        self._positions = dict(SEATS)
        self._waiting = {sid: 0 for sid in self._stations}
        self._occupant = {sid: None for sid in self._stations}

    def snapshot(self) -> Dict[str, Any]:
        """Floor state for `/api/office` -- station geometry plus live occupancy."""
        return {
            "spatial_enabled": settings.spatial_enabled,
            "tile": 16,
            "walk_seconds_per_tile": settings.walk_seconds_per_tile,
            "seats": {aid: {"x": pos[0], "y": pos[1]} for aid, pos in SEATS.items()},
            "positions": {aid: {"x": pos[0], "y": pos[1]} for aid, pos in self._positions.items()},
            "stations": [
                {
                    **station.model_dump(),
                    "occupied_by": self._occupant.get(station.id),
                    "queue_length": self._waiting.get(station.id, 0)
                }
                for station in self._stations.values()
            ]
        }


# Global singleton floor.
office = OfficeFloor()
