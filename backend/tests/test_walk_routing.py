"""
Walk time, measured along the floor rather than through the walls.

`travel_seconds` was straight-line distance, so the backend and the renderer
disagreed on every trip that has to go round something: the office drew an agent
still walking after the backend had decided it had arrived, and a station's
queue started before anyone had reached it.

The grid comes from `office_layout.json`, exported from the same map the
renderer draws, and the search is the same one `canvas.js` runs -- breadth-first,
four-connected, over `blocked`. Matching the algorithm matters as much as
matching the grid: allowing diagonals here would make the backend consistently
optimistic about a route the office draws as a staircase.
"""
import pytest

import office as office_mod
from config import settings
from office import office


def test_a_route_round_the_furniture_is_longer_than_the_straight_line():
    """
    The whole point. Boss Dave's desk and the ops console are on opposite sides
    of the floor with rooms between them, so the walk is far longer than the
    line between the two points.
    """
    boss = office_mod.SEATS["orchestrator"]
    ops = office_mod._STATION_POS["ops_console"]

    tiles = office.path_tiles(boss, ops)
    straight = office._distance(boss, ops)

    assert tiles is not None, "the two are not connected on the grid"
    assert tiles > straight * 1.5, (
        f"routed {tiles} tiles vs {straight:.1f} straight -- the walls are "
        f"not being taken into account")


def test_every_station_is_reachable_from_every_seat():
    """A station the backend cannot route to would block whichever step needs
    the tool bound to it."""
    unreachable = []
    for aid, seat in office_mod.SEATS.items():
        for sid, station in office_mod._STATION_POS.items():
            if office.path_tiles(seat, station) is None:
                unreachable.append(f"{aid} -> {sid}")
    assert not unreachable, f"no route for: {unreachable}"


def test_the_search_is_four_connected_like_the_renderers():
    """
    `canvas.js` walks [[1,0],[-1,0],[0,1],[0,-1]] and nothing else, so a route
    can never be shorter than the Manhattan distance between its endpoints.
    Allowing a diagonal step here would break that on the first open floor it
    crossed, and make the backend optimistic about a trip the office draws as a
    staircase.

    Asserted as the invariant over every real pair rather than one hand-picked
    offset -- a fixed pair only tests whichever tiles it happened to name, and
    the first version of this test named one that was blocked.
    """
    checked = 0
    for seat in office_mod.SEATS.values():
        for station in office_mod._STATION_POS.values():
            tiles = office.path_tiles(seat, station)
            if tiles is None:
                continue
            a = office._nearest_open(seat)
            b = office._nearest_open(station)
            manhattan = abs(a[0] - b[0]) + abs(a[1] - b[1])
            assert tiles >= manhattan, (
                f"{a} -> {b} routed in {tiles} tiles, under the Manhattan "
                f"minimum of {manhattan}: diagonals are being allowed")
            checked += 1
    assert checked, "no routes were checked"


def test_endpoints_on_furniture_snap_to_the_floor_beside_them():
    """
    A seat sits under its own desk and a station sits on the furniture it IS,
    so both ends of a real walk are routinely on blocked tiles. Rejecting them
    would mean no route existed for any actual trip.
    """
    library = office_mod._STATION_POS["research_library"]
    x, y = int(round(library[0])), int(round(library[1]))

    assert office_mod._BLOCKED[y][x], (
        "fixture assumes research_library sits on the shelving it names; if the "
        "map moved it onto open floor, pick another blocked station")
    snapped = office._nearest_open(library)
    assert snapped is not None and not office_mod._BLOCKED[snapped[1]][snapped[0]]
    assert office.path_tiles(office_mod.SEATS["researcher"], library) is not None


def test_travel_time_respects_the_clamp():
    """
    Routing makes long trips longer, so the ceiling binds more often than it
    did. It still has to bind: a cross-office walk that takes ten seconds is
    not watchable, whatever the geometry says.
    """
    boss = office_mod.SEATS["orchestrator"]
    ops = office_mod._STATION_POS["ops_console"]

    seconds = office.travel_seconds(boss, ops)
    assert seconds <= settings.max_walk_seconds
    assert seconds >= settings.min_walk_seconds
    # Same desk to itself is still a real minimum, not zero.
    assert office.travel_seconds(boss, boss) == settings.min_walk_seconds


def test_it_falls_back_to_straight_line_without_a_grid(monkeypatch):
    """
    An older `office_layout.json` has no `blocked` key. The harness must behave
    exactly as it did rather than refusing to walk anybody anywhere.
    """
    monkeypatch.setattr(office_mod, "_BLOCKED", [])

    a, b = (2.0, 3.0), (12.0, 3.0)
    assert office.path_tiles(a, b) is None

    expected = office._distance(a, b) * settings.walk_seconds_per_tile
    assert office.travel_seconds(a, b) == pytest.approx(
        max(settings.min_walk_seconds, min(expected, settings.max_walk_seconds)))


def test_an_unroutable_pair_still_takes_time(monkeypatch):
    """
    A walk that cannot be routed still has to take some time -- the renderer's
    own movement falls back to sliding rather than refusing to move.
    """
    wall = [[True] * 40 for _ in range(24)]
    wall[3][2] = False
    wall[3][30] = False
    monkeypatch.setattr(office_mod, "_BLOCKED", wall)

    assert office.path_tiles((2, 3), (30, 3)) is None
    assert office.travel_seconds((2, 3), (30, 3)) >= settings.min_walk_seconds
