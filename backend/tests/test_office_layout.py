"""
The exported office geometry, and what it says about the floor.

`office.py` walks agents to seats and stations by coordinate, and reads those
from `office_layout.json` rather than keeping its own copy -- a copy that went
stale the moment the map was rearranged, which is how all five seats once ended
up inside desks.

The export gained a `blocked` grid so the backend can eventually time a walk
over the route the renderer actually draws. These pin the shape of that grid,
and pin the fact that it is currently fragmented -- so a change that silently
makes it worse shows up here rather than as agents mysteriously teleporting.
"""
import json
import os
from collections import deque

import pytest

LAYOUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "office_layout.json")


@pytest.fixture(scope="module")
def layout():
    with open(LAYOUT, encoding="utf-8") as fh:
        return json.load(fh)


def _grid(layout):
    return [[c == "1" for c in row] for row in layout["blocked"]]


def _regions(grid):
    rows, cols = len(grid), len(grid[0])
    seen = [[False] * cols for _ in range(rows)]
    out = []
    for y in range(rows):
        for x in range(cols):
            if grid[y][x] or seen[y][x]:
                continue
            q, cells = deque([(x, y)]), 0
            seen[y][x] = True
            while q:
                cx, cy = q.popleft()
                cells += 1
                for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    nx, ny = cx + dx, cy + dy
                    if (0 <= nx < cols and 0 <= ny < rows
                            and not grid[ny][nx] and not seen[ny][nx]):
                        seen[ny][nx] = True
                        q.append((nx, ny))
            out.append(cells)
    return sorted(out, reverse=True)


def test_the_export_carries_a_grid_the_backend_can_path_on(layout):
    grid = _grid(layout)
    assert len(grid) == layout["rows"]
    assert all(len(row) == layout["cols"] for row in grid)
    assert any(not cell for row in grid for cell in row), "everything is blocked"


def test_the_grid_matches_the_renderers_dimensions(layout):
    """40x24 is the office. A mismatch means the export and the map disagree,
    which is the drift office_layout.json exists to prevent."""
    assert (layout["cols"], layout["rows"]) == (40, 24)
    assert layout["tile"] == 16


def test_every_seat_and_station_has_open_floor_beside_it(layout):
    """
    A seat sits under its own desk sprite and a station sits on the furniture it
    IS -- both expected, and the renderer exempts an agent at its own seat. What
    is not acceptable is a seat or station with no open tile adjacent at all,
    because then nothing can stand next to it however the pathing works.
    """
    grid = _grid(layout)
    rows, cols = len(grid), len(grid[0])
    stranded = []
    for kind in ("seats", "stations"):
        for name, pos in layout[kind].items():
            x, y = int(round(pos["x"])), int(round(pos["y"]))
            near = [(x + dx, y + dy) for dx, dy in
                    ((0, 0), (1, 0), (-1, 0), (0, 1), (0, -1))]
            if not any(0 <= nx < cols and 0 <= ny < rows and not grid[ny][nx]
                       for nx, ny in near):
                stranded.append(f"{kind}.{name} at ({x},{y})")
    assert not stranded, f"nothing can stand at: {stranded}"


def test_the_floor_is_effectively_one_region(layout):
    """
    476 of 478 walkable tiles are mutually reachable; the leftover is a
    two-tile decorative pocket. This is the property that makes routing over
    the grid worth doing at all -- A* can only ever path within one region, so
    a fragmented floor means the pathfinder is decorative.

    Pinned because a first version of the exporter marked every prop and made
    the office look like seven islands. That was the exporter being stricter
    than the renderer, not the map being broken, and it is exactly the kind of
    wrong number that gets written into a README.
    """
    regions = _regions(_grid(layout))
    assert regions[0] >= 470, f"main region shrank to {regions[0]} ({regions})"
    assert sum(regions) - regions[0] <= 4, (
        f"more than a pocket is stranded off the main region: {regions}")


def test_every_seat_and_station_is_reachable_from_every_other(layout):
    """
    The one that matters for routing: an agent has to be able to walk from any
    desk to any station. A station stranded off the main region cannot be
    reached however good the pathfinder is.
    """
    grid = _grid(layout)
    rows, cols = len(grid), len(grid[0])

    start = None
    for name, pos in layout["seats"].items():
        x, y = int(round(pos["x"])), int(round(pos["y"]))
        if not grid[y][x]:
            start = (x, y)
            break
    assert start, "no seat is on an open tile"

    seen = {start}
    q = deque([start])
    while q:
        x, y = q.popleft()
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            nx, ny = x + dx, y + dy
            if (0 <= nx < cols and 0 <= ny < rows
                    and not grid[ny][nx] and (nx, ny) not in seen):
                seen.add((nx, ny))
                q.append((nx, ny))

    unreachable = []
    for kind in ("seats", "stations"):
        for name, pos in layout[kind].items():
            x, y = int(round(pos["x"])), int(round(pos["y"]))
            near = [(x + dx, y + dy) for dx, dy in
                    ((0, 0), (1, 0), (-1, 0), (0, 1), (0, -1))]
            if not any(t in seen for t in near):
                unreachable.append(f"{kind}.{name} at ({x},{y})")
    assert not unreachable, f"cannot be walked to: {unreachable}"
