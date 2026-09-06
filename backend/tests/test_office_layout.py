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


def test_the_floor_is_still_seven_regions(layout):
    """
    Pinned as a fact, not endorsed as a goal. The renderer's A* can only route
    within one region, so this number is the ceiling on how much of the office
    the pathfinder is actually doing -- movement falls back to sliding for the
    rest. If a map change makes it worse this fails; if someone fixes the map,
    this fails too and should be updated downwards with the fix.
    """
    regions = _regions(_grid(layout))
    assert len(regions) == 7, (
        f"walkable regions changed from 7 to {len(regions)} ({regions}). "
        f"Fewer is better -- update this test with the map fix.")
    assert regions[0] == 324, f"main region changed size: {regions[0]}"
