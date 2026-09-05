"""
Station capacity, which used to be metadata nobody read.

`_lock_for` was an `asyncio.Lock` -- always exactly one holder -- so
`Station.capacity` was declared in the schema and honoured nowhere. A tool bench
genuinely is one-at-a-time, but the conference table is not: a meeting is
several agents standing in the same place, and a capacity of 4 that admits one
turns every meeting into a queue.

It is a semaphore now. These pin that, because the difference is invisible
except under contention -- one agent at a time passes either way.
"""
import asyncio

import pytest

from office import office
from schemas import Station


async def _wait_until(predicate, timeout=5.0):
    """
    Poll until `predicate()` holds.

    A fixed sleep is not enough here: `use_tool_station` walks the agent to the
    station before it takes a seat, and that walk is simulated time that varies
    with distance. The interesting assertion in every test below is about what
    happens *after* everyone who can be admitted has been, so the tests wait for
    that state rather than guessing how long it takes to reach.
    """
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return False


def _install(monkeypatch, station_id, tool, capacity):
    """
    Put one station on the floor for the length of a test.

    `_waiting` has to be seeded alongside `_stations`: it is built once from the
    station ids at construction, so a station added later has no queue counter
    and `use_tool_station` raises KeyError on the way in.
    """
    station = Station(id=station_id, label=station_id, x=1, y=1,
                      tools=[tool], capacity=capacity)
    monkeypatch.setitem(office._stations, station.id, station)
    monkeypatch.setitem(office._tool_index, tool, station.id)
    monkeypatch.setitem(office._waiting, station.id, 0)
    # The lock cache is keyed by station id and would outlive the test.
    office._locks.pop(station.id, None)
    return station


@pytest.fixture
def two_seat(monkeypatch):
    """A station with room for exactly two, registered for one test."""
    station = _install(monkeypatch, "tst_bench", "tst_tool", capacity=2)
    yield station
    office._locks.pop(station.id, None)


@pytest.mark.asyncio
async def test_a_station_admits_up_to_its_capacity_at_once(two_seat):
    """Two seats, three arrivals: the third waits, the first two do not."""
    peak = 0
    active = 0
    released = asyncio.Event()

    async def occupy(agent_id):
        nonlocal peak, active
        async with office.use_tool_station(agent_id, "tst_tool", agent_name=agent_id):
            active += 1
            peak = max(peak, active)
            await released.wait()
            active -= 1

    holders = [asyncio.ensure_future(occupy(f"a{i}")) for i in range(3)]
    assert await _wait_until(lambda: active >= 2), "nobody reached the station"
    # Give a third agent every chance to slip in past the cap.
    await asyncio.sleep(0.3)

    assert peak == 2, f"capacity is 2 but {peak} agents were admitted"
    released.set()
    await asyncio.wait_for(asyncio.gather(*holders), timeout=5)
    assert active == 0, "every seat was given back"


@pytest.mark.asyncio
async def test_the_third_agent_gets_in_once_a_seat_frees(two_seat):
    """The queue drains rather than deadlocking on a full station."""
    order = []
    gate = asyncio.Event()

    async def occupy(agent_id, hold):
        async with office.use_tool_station(agent_id, "tst_tool", agent_name=agent_id):
            order.append(agent_id)
            if hold:
                await gate.wait()

    a = asyncio.ensure_future(occupy("a1", True))
    b = asyncio.ensure_future(occupy("a2", True))
    assert await _wait_until(lambda: len(order) == 2), f"only {order} were admitted"
    c = asyncio.ensure_future(occupy("a3", False))
    await asyncio.sleep(0.3)

    assert order == ["a1", "a2"], "the third agent must not be admitted yet"
    gate.set()
    await asyncio.wait_for(asyncio.gather(a, b, c), timeout=5)
    assert order == ["a1", "a2", "a3"]


@pytest.mark.asyncio
async def test_seats_free_reports_the_real_count(two_seat):
    assert office._seats_free("tst_bench") is True

    gate = asyncio.Event()
    seen = {}

    async def occupy(agent_id):
        async with office.use_tool_station(agent_id, "tst_tool", agent_name=agent_id):
            seen[agent_id] = office._seats_free("tst_bench")
            await gate.wait()

    holders = [asyncio.ensure_future(occupy(f"a{i}")) for i in range(2)]
    assert await _wait_until(lambda: len(seen) == 2), "both agents must sit down"

    assert office._seats_free("tst_bench") is False, "both seats are taken"
    gate.set()
    await asyncio.wait_for(asyncio.gather(*holders), timeout=5)
    assert office._seats_free("tst_bench") is True


@pytest.mark.asyncio
async def test_a_single_seat_station_is_still_exclusive(monkeypatch):
    """The default. A tool bench is one agent at a time and must stay that way."""
    station = _install(monkeypatch, "tst_solo", "tst_solo_tool", capacity=1)

    peak = 0
    active = 0
    gate = asyncio.Event()

    async def occupy(agent_id):
        nonlocal peak, active
        async with office.use_tool_station(agent_id, "tst_solo_tool", agent_name=agent_id):
            active += 1
            peak = max(peak, active)
            await gate.wait()
            active -= 1

    holders = [asyncio.ensure_future(occupy(f"a{i}")) for i in range(3)]
    assert await _wait_until(lambda: active >= 1), "nobody reached the station"
    await asyncio.sleep(0.3)
    assert peak == 1

    gate.set()
    await asyncio.wait_for(asyncio.gather(*holders), timeout=5)
    office._locks.pop(station.id, None)
