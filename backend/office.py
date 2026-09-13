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
Seats and station coordinates come from `office_layout.json`, exported from
`frontend/js/office_map.js` by `tools/export_layout.py`. They used to be
literals here -- a second copy of the map's geometry -- and they drifted badly
enough that every seat ended up inside a desk. The map owns the geometry; this
module owns the behaviour, and serves both over `/api/office`.
"""
import asyncio
import json
import logging
import math
import os
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional, Tuple

from config import settings
from event_bus import event_bus
from schemas import Event, EventType, Station

logger = logging.getLogger("AgentHQ.Office")


# Office geometry, loaded from the map rather than duplicated here.
#
# These used to be literals -- a second copy of OFFICE_MAP.seats. Nothing kept
# the two in step, and they drifted: after the bullpen was rearranged all five
# backend seats sat inside a desk sprite, and two stations sat inside the
# boardroom table. The map is the source of truth now, exported to
# office_layout.json by tools/export_layout.py and checked by check_layout.py.
_LAYOUT_PATH = os.path.join(os.path.dirname(__file__), "office_layout.json")

try:
    with open(_LAYOUT_PATH, encoding="utf-8") as _f:
        _LAYOUT: Dict[str, Any] = json.load(_f)
except (OSError, ValueError) as _exc:      # pragma: no cover - startup guard
    raise RuntimeError(
        f"office_layout.json is missing or unreadable ({_exc}). "
        "Run: python tools/export_layout.py"
    ) from _exc

SEATS: Dict[str, Tuple[float, float]] = {
    aid: (seat["x"], seat["y"]) for aid, seat in _LAYOUT["seats"].items()
}

_STATION_POS: Dict[str, Tuple[float, float]] = {
    sid: (st["x"], st["y"]) for sid, st in _LAYOUT["stations"].items()
}

# The walkable grid, as rows of "0110". Same grid `canvas.js buildCollision(true)`
# builds and the renderer's A* paths on, exported alongside the coordinates so a
# walk can be timed over the route that is actually drawn rather than through the
# conference room wall. Absent from an older export, in which case walk timing
# falls back to straight-line distance and behaves exactly as it did.
_BLOCKED: List[List[bool]] = [[c == "1" for c in row]
                              for row in _LAYOUT.get("blocked", [])]


def _station_xy(station_id: str, fallback: Tuple[float, float]) -> Tuple[float, float]:
    """Coordinates for a station, from the map when it names one."""
    return _STATION_POS.get(station_id, fallback)

# Where an agent with no desk of its own is parked.
DEFAULT_SEAT: Tuple[float, float] = (12.0, 15.0)

# The spare desks, for agents hired mid-mission. Two of them, because the
# bullpen's two ranks of three workstations are four-fifths occupied by the
# permanent staff -- so the cap on contractors is furniture rather than policy.
SPAWN_SEATS: List[Tuple[float, float]] = [
    (seat["x"], seat["y"]) for seat in _LAYOUT.get("spawn_seats", [])
]

# The street door, just inside. Where a contractor walks in from.
_ENTRANCE_RAW = _LAYOUT.get("entrance") or {}
ENTRANCE: Tuple[float, float] = (
    float(_ENTRANCE_RAW.get("x", 24.5)), float(_ENTRANCE_RAW.get("y", 22.4)),
)


# The stations, placed on furniture that already exists in the map. `tools` is
# the binding that gates execution; a tool named by no station stays callable
# from the agent's desk.
STATIONS: List[Station] = [
    Station(
        id="ops_console",
        label="Ops Console",
        x=_station_xy("ops_console", (19.4, 18.4))[0], y=_station_xy("ops_console", (19.4, 18.4))[1],
        tools=["terminal", "code_analysis", "query_data"],
        kind="tool",
        description=(
            "The console rig by the conference wall. Shell, AST work and data "
            "queries happen here."
        )
    ),
    Station(
        id="research_library",
        label="Research Library",
        # The fallback follows the map: the shelf run on the bullpen's north
        # wall. It used to read (2.6, 12.4), which was the west wall -- nine
        # tiles from the nearest bookshelf, beside a potted plant and the end
        # of somebody's desk.
        x=_station_xy("research_library", (8.91, 4.97))[0], y=_station_xy("research_library", (8.91, 4.97))[1],
        tools=["web_search", "read_file", "fetch_url", "describe_image"],
        kind="tool",
        description=(
            "The library shelves on the north wall. Search, reading and fetching "
            "a page happens here."
        )
    ),
    Station(
        id="filing_archive",
        label="Filing Archive",
        x=_station_xy("filing_archive", (2.6, 9.4))[0], y=_station_xy("filing_archive", (2.6, 9.4))[1],
        tools=["write_file", "list_files", "search_workspace"],
        kind="tool",
        description=(
            "The cabinets on the west wall. Anything written to disk is filed "
            "here, and searching the drawers happens here too."
        )
    ),
    Station(
        id="whiteboard",
        label="Whiteboard",
        x=_station_xy("whiteboard", (5.2, 5.2))[0], y=_station_xy("whiteboard", (5.2, 5.2))[1],
        tools=[],
        kind="planning",
        description="Where the boss decomposes a goal before dispatching it."
    ),
    Station(
        id="conference_table",
        label="Conference Room",
        x=_station_xy("conference_table", (11.0, 19.6))[0], y=_station_xy("conference_table", (11.0, 19.6))[1],
        tools=["review_artifacts", "record_decision"],
        kind="meeting",
        description=(
            "The boardroom. Attendees read each other's work here and write down "
            "what they agreed."
        ),
        # The one station meant to be shared. Every other station is a bench one
        # agent stands at; this is a table people sit round, so its capacity is
        # the number of specialists that can be in a meeting at once.
        capacity=4
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
        # Desks handed to agents hired mid-mission. Kept apart from SEATS,
        # which is the map's own record of where the permanent staff sit and
        # has to stay that -- a contractor must not leave a desk behind in it.
        self._assigned: Dict[str, Tuple[float, float]] = {}
        self._locks: Dict[str, asyncio.Semaphore] = {}
        self._waiting: Dict[str, int] = {sid: 0 for sid in self._stations}
        # Several agents can hold a station now, so occupancy is a set.
        self._occupant: Dict[str, set] = {sid: set() for sid in self._stations}

    # ---------------------------------------------------------------- geometry

    def seat_of(self, agent_id: str) -> Tuple[float, float]:
        if agent_id in self._assigned:
            return self._assigned[agent_id]
        return SEATS.get(agent_id, DEFAULT_SEAT)

    # ------------------------------------------------------ hired mid-mission

    def free_seats(self) -> int:
        """Spare desks not currently handed out."""
        return max(0, len(SPAWN_SEATS) - len(self._assigned))

    def assign_seat(self, agent_id: str) -> Optional[Tuple[float, float]]:
        """
        Give `agent_id` a spare desk, and start it at the door.

        None when the bullpen is full. The caller must treat that as a refusal
        to hire rather than seating someone on DEFAULT_SEAT: two contractors
        parked on the same tile look like one agent, and an office that lies
        about who is in it is worse than one that says it is full.
        """
        if agent_id in self._assigned:
            return self._assigned[agent_id]
        taken = set(self._assigned.values())
        for seat in SPAWN_SEATS:
            if seat in taken:
                continue
            self._assigned[agent_id] = seat
            # They are not at the desk yet. They are outside the front door,
            # and `walk_in` is what gets them across the floor.
            self._positions[agent_id] = ENTRANCE
            return seat
        return None

    def release_seat(self, agent_id: str) -> None:
        """Give the desk back. The contract is over."""
        self._assigned.pop(agent_id, None)
        self._positions.pop(agent_id, None)

    async def walk_in(self, agent_id: str, agent_name: str = "",
                      task_id: Optional[str] = None) -> float:
        """
        Walk a newly hired agent from the street door to their desk.

        The same `walk_to` every other move uses, so it is timed over the real
        route and emits the same AGENT_MOVE the renderer already animates -- a
        contractor crossing the floor is not a special case, it just starts
        where nobody else starts.
        """
        seat = self._assigned.get(agent_id)
        if seat is None or not settings.spatial_enabled:
            return 0.0
        self._positions[agent_id] = ENTRANCE
        return await self.walk_to(agent_id, seat, agent_name=agent_name,
                                  label="desk", task_id=task_id)

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

    @staticmethod
    def _open(tile: Tuple[int, int]) -> bool:
        x, y = tile
        return (0 <= y < len(_BLOCKED) and 0 <= x < len(_BLOCKED[0])
                and not _BLOCKED[y][x])

    @classmethod
    def _nearest_open(cls, point: Tuple[float, float]) -> Optional[Tuple[int, int]]:
        """
        The closest tile you can stand on, searched outward from `point`.

        Needed at both ends of a route: a seat sits under its own desk and a
        station sits on the furniture it is, so the endpoints of a real walk are
        routinely on blocked tiles. Searching outward rather than rejecting is
        what makes "walk to the conference table" mean "walk to the floor beside
        the conference table", which is what actually happens.
        """
        start = (int(round(point[0])), int(round(point[1])))
        if cls._open(start):
            return start
        for radius in range(1, 6):
            best = None
            for dy in range(-radius, radius + 1):
                for dx in range(-radius, radius + 1):
                    if max(abs(dx), abs(dy)) != radius:
                        continue
                    tile = (start[0] + dx, start[1] + dy)
                    if not cls._open(tile):
                        continue
                    d = (tile[0] - point[0]) ** 2 + (tile[1] - point[1]) ** 2
                    if best is None or d < best[0]:
                        best = (d, tile)
            if best:
                return best[1]
        return None

    @classmethod
    def path_tiles(cls, frm: Tuple[float, float],
                   to: Tuple[float, float]) -> Optional[int]:
        """
        Length in tiles of the shortest walkable route, or None if there is none.

        Breadth-first over the exported grid: it is 40x24, so the whole floor is
        under a thousand cells and an exact search costs less than the heuristic
        would. Four-connected, matching the renderer -- allowing diagonals here
        would make the backend consistently optimistic about a route the office
        draws as a staircase.
        """
        if not _BLOCKED:
            return None
        start, goal = cls._nearest_open(frm), cls._nearest_open(to)
        if start is None or goal is None:
            return None
        if start == goal:
            return 0

        rows, cols = len(_BLOCKED), len(_BLOCKED[0])
        seen = {start}
        frontier = [start]
        steps = 0
        while frontier:
            steps += 1
            nxt = []
            for x, y in frontier:
                for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    tile = (x + dx, y + dy)
                    if tile in seen:
                        continue
                    if not (0 <= tile[0] < cols and 0 <= tile[1] < rows):
                        continue
                    if _BLOCKED[tile[1]][tile[0]]:
                        continue
                    if tile == goal:
                        return steps
                    seen.add(tile)
                    nxt.append(tile)
            frontier = nxt
        return None

    def travel_seconds(self, frm: Tuple[float, float], to: Tuple[float, float]) -> float:
        """
        Walk time for a leg, clamped so a cross-office trip stays watchable.

        Measured along the walkable route rather than through the walls. This
        used to be straight-line distance, so the backend and the renderer
        disagreed on every trip that has to go round something -- the office
        drew an agent still walking after the backend had decided it had
        arrived, and the queue at a station started before anyone reached it.

        Falls back to straight-line when the grid is missing or the two points
        are not connected, because a walk that cannot be routed still has to
        take some time; the renderer's own movement falls back the same way.
        """
        tiles = self.path_tiles(frm, to)
        distance = self._distance(frm, to) if tiles is None else float(tiles)
        raw = distance * settings.walk_seconds_per_tile
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

    def _lock_for(self, station_id: str) -> asyncio.Semaphore:
        """
        A station admits `capacity` agents at once.

        This used to be an asyncio.Lock, i.e. always exactly one holder, which
        made Station.capacity dead metadata -- declared in the schema and read
        nowhere. A tool bench genuinely is one-at-a-time, but the conference
        table is not: a meeting is several agents in the same place, so it needs
        a counter rather than a flag.

        Created lazily so none of them binds to the import-time event loop.
        """
        if station_id not in self._locks:
            station = self._stations.get(station_id)
            capacity = max(1, getattr(station, "capacity", 1) or 1)
            self._locks[station_id] = asyncio.Semaphore(capacity)
        return self._locks[station_id]

    def _seats_free(self, station_id: str) -> bool:
        """True when the station has room right now."""
        sem = self._lock_for(station_id)
        return sem._value > 0        # asyncio.Semaphore exposes no public count

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

        if not self._seats_free(station.id):
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
                    "occupied_by": ", ".join(sorted(self._occupant.get(station.id) or [])) or None,
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

        self._occupant.setdefault(station.id, set()).add(agent_id)
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
            self._occupant.get(station.id, set()).discard(agent_id)
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

    @asynccontextmanager
    async def hold_meeting(
        self,
        station_id: str,
        attendees: List[Tuple[str, str]],
        task_id: Optional[str] = None
    ):
        """
        Gather people at a station and keep them there for the block.

        `attendees` is (agent_id, agent_name) for everyone EXCEPT the chair.
        That exclusion is deliberate and load-bearing: the chair reaches the
        table the ordinary way, by calling a tool bound to it, which takes a
        seat through use_tool_station. Reserving a seat for the chair here as
        well would mean it needs two at once, and a full room would deadlock the
        chair out of its own meeting. The planner caps the guest list at
        capacity - 1, so the seat the chair needs is always the one left over.

        Seats are acquired before anyone walks, so a meeting either forms or
        waits -- it never half-forms with two people standing at a full table.
        Everyone is walked back to their desk on the way out, including when the
        meeting raised.
        """
        station = self._stations.get(station_id)
        if station is None or not settings.spatial_enabled:
            yield None
            return

        sem = self._lock_for(station_id)
        held: List[Tuple[str, str]] = []
        try:
            for agent_id, agent_name in attendees:
                try:
                    await asyncio.wait_for(sem.acquire(), timeout=settings.station_wait_timeout)
                except asyncio.TimeoutError:
                    logger.warning(f"{agent_id} timed out waiting for a seat at {station_id}")
                    continue
                held.append((agent_id, agent_name))
                self._occupant.setdefault(station_id, set()).add(agent_id)

            for agent_id, agent_name in held:
                await self.walk_to(
                    agent_id, (station.x, station.y),
                    agent_name=agent_name, label=station.label,
                    station_id=station_id, task_id=task_id
                )
                await event_bus.emit(Event(
                    type=EventType.STATION_ACQUIRED,
                    agent_id=agent_id,
                    task_id=task_id,
                    data={
                        "agent_name": agent_name,
                        "station_id": station_id,
                        "label": station.label,
                        "tool": "meeting"
                    }
                ))
            yield station
        finally:
            for agent_id, agent_name in held:
                self._occupant.get(station_id, set()).discard(agent_id)
                sem.release()
                await event_bus.emit(Event(
                    type=EventType.STATION_RELEASED,
                    agent_id=agent_id,
                    task_id=task_id,
                    data={
                        "agent_name": agent_name,
                        "station_id": station_id,
                        "label": station.label,
                        "queue_length": self._waiting.get(station_id, 0)
                    }
                ))
                await self.return_to_desk(agent_id, agent_name=agent_name, task_id=task_id)

    # ---------------------------------------------------------------- reporting

    def reset(self):
        """
        Put everyone back at their desk. Called when a mission finishes.

        Contractors' desks are released with it: they were hired for the
        mission that just ended, and a seat still held afterwards is one the
        next mission cannot hire into.
        """
        self._assigned.clear()
        self._positions = dict(SEATS)
        self._waiting = {sid: 0 for sid in self._stations}
        self._occupant = {sid: set() for sid in self._stations}

    def snapshot(self) -> Dict[str, Any]:
        """Floor state for `/api/office` -- station geometry plus live occupancy."""
        return {
            "spatial_enabled": settings.spatial_enabled,
            "tile": 16,
            "walk_seconds_per_tile": settings.walk_seconds_per_tile,
            "seats": {aid: {"x": pos[0], "y": pos[1]}
                      for aid, pos in {**SEATS, **self._assigned}.items()},
            "spawn_seats": [{"x": x, "y": y} for x, y in SPAWN_SEATS],
            "entrance": {"x": ENTRANCE[0], "y": ENTRANCE[1]},
            "positions": {aid: {"x": pos[0], "y": pos[1]} for aid, pos in self._positions.items()},
            "stations": [
                {
                    **station.model_dump(),
                    "occupied_by": ", ".join(sorted(self._occupant.get(station.id) or [])) or None,
                    "queue_length": self._waiting.get(station.id, 0)
                }
                for station in self._stations.values()
            ]
        }


# Global singleton floor.
office = OfficeFloor()
