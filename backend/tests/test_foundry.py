"""
Hiring a specialist the office does not have.

The interesting cases are the refusals. A foundry that hires eagerly is easy
and produces the exact failure this project keeps finding -- an agent that
cannot call its tool and writes a plausible report around the hole -- so most
of what follows is about when NOT to hire.
"""
import os
import shutil
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import foundry                                      # noqa: E402
import office as office_mod                         # noqa: E402
import runstore                                     # noqa: E402
from config import settings                         # noqa: E402
from office import office                           # noqa: E402
from schemas import AgentManifest, PlanStep         # noqa: E402
from tool_registry import tool_registry             # noqa: E402


def _step(step_id="s1", needs=(), description="Do the thing"):
    return PlanStep(id=step_id, agent_id="researcher", description=description,
                    needs_tools=list(needs))


# ------------------------------------------------------------------ gap finding

def test_no_gap_when_somebody_already_holds_the_tools():
    steps = [_step(needs=["web_search", "read_file"])]
    held = {"researcher": ["web_search", "read_file", "calculate"]}
    assert foundry.find_gaps(steps, held) == []


def test_a_gap_is_one_agent_holding_all_of_them_not_the_office_between_them():
    """
    The case `regrant_steps` gives up on: the tools all exist and are all held,
    but by different people, so no single agent can run the step.
    """
    steps = [_step(needs=["web_search", "terminal"])]
    held = {"researcher": ["web_search"], "coder": ["terminal"]}
    gaps = foundry.find_gaps(steps, held)
    assert len(gaps) == 1
    assert gaps[0].needed == ["terminal", "web_search"]
    assert gaps[0].hireable is True


def test_a_tool_nobody_implements_is_not_hireable():
    """
    The distinction `regrant_steps` cannot draw. From there, a tool nobody was
    granted and a tool nobody wrote look identical -- and they need opposite
    responses. Hiring for this one produces an agent holding a name with no
    function behind it, which is how you get a confident invented answer.
    """
    steps = [_step(needs=["query_postgres"])]
    gaps = foundry.find_gaps(steps, {"researcher": ["web_search"]})
    assert len(gaps) == 1
    assert gaps[0].missing == ["query_postgres"]
    assert gaps[0].hireable is False


def test_a_step_declaring_no_tools_is_not_a_gap():
    assert foundry.find_gaps([_step(needs=[])], {"researcher": []}) == []


# --------------------------------------------------------------------- hiring

@pytest.mark.asyncio
async def test_a_contractor_holds_exactly_what_the_step_declared(monkeypatch):
    """
    Not a preset and not everything. Tighter than a fixed allowance -- nobody
    accumulates a tool they are not using -- and more capable, since nothing is
    pre-banned.
    """
    monkeypatch.setattr(settings, "foundry_design_enabled", False)
    gaps = foundry.find_gaps([_step(needs=["query_data", "calculate"])],
                             {"researcher": ["web_search"]})
    hires, refusals = await foundry.hire("Analyse the sales file", gaps,
                                         existing_ids=["researcher"],
                                         mission_id="m1", seats_available=2)
    assert len(hires) == 1 and not refusals
    assert sorted(hires[0].tools) == ["calculate", "query_data"]
    assert hires[0].spawned is True
    assert hires[0].spawned_for == "s1"
    assert hires[0].mission_id == "m1"


@pytest.mark.asyncio
async def test_a_missing_tool_is_refused_with_a_reason(monkeypatch):
    monkeypatch.setattr(settings, "foundry_design_enabled", False)
    gaps = foundry.find_gaps([_step(needs=["query_postgres"])], {"researcher": []})
    hires, refusals = await foundry.hire("x", gaps, existing_ids=["researcher"],
                                         mission_id="m1", seats_available=2)
    assert hires == []
    assert refusals and "query_postgres" in refusals[0]
    # The refusal has to say what would fix it, or it is just a failure.
    assert "backend/tools/" in refusals[0]


@pytest.mark.asyncio
async def test_hiring_stops_at_the_desks_available(monkeypatch):
    monkeypatch.setattr(settings, "foundry_design_enabled", False)
    monkeypatch.setattr(settings, "max_spawned_agents", 5)
    steps = [_step("s1", ["query_data"]), _step("s2", ["terminal"]),
             _step("s3", ["fetch_url"])]
    gaps = foundry.find_gaps(steps, {"researcher": ["web_search"]})
    assert len(gaps) == 3
    hires, refusals = await foundry.hire("x", gaps, existing_ids=["researcher"],
                                         mission_id="m1", seats_available=1)
    assert len(hires) == 1
    assert len(refusals) == 2
    assert "full" in refusals[0]


@pytest.mark.asyncio
async def test_the_policy_cap_applies_even_with_desks_free(monkeypatch):
    monkeypatch.setattr(settings, "foundry_design_enabled", False)
    monkeypatch.setattr(settings, "max_spawned_agents", 1)
    steps = [_step("s1", ["query_data"]), _step("s2", ["terminal"])]
    gaps = foundry.find_gaps(steps, {"researcher": ["web_search"]})
    hires, refusals = await foundry.hire("x", gaps, existing_ids=["researcher"],
                                         mission_id="m1", seats_available=9)
    assert len(hires) == 1 and len(refusals) == 1


@pytest.mark.asyncio
async def test_contractor_ids_do_not_collide_with_the_permanent_staff(monkeypatch):
    monkeypatch.setattr(settings, "foundry_design_enabled", False)
    steps = [_step("s1", ["query_data"]), _step("s2", ["fetch_url"])]
    gaps = foundry.find_gaps(steps, {"researcher": []})
    hires, _ = await foundry.hire("x", gaps, existing_ids=["researcher", "coder"],
                                  mission_id="m1", seats_available=2)
    ids = [h.id for h in hires]
    assert len(set(ids)) == len(ids)
    assert "researcher" not in ids and "coder" not in ids


@pytest.mark.asyncio
async def test_the_design_call_cannot_grant_itself_tools(monkeypatch):
    """
    The one field the model does not get a say in. A design reply naming
    `terminal` must not produce an agent holding `terminal` -- the tool list
    comes from the gap, and is never read back from the reply.
    """
    class _Message:
        content = ('{"name": "Mallory", "role": "Everything",'
                   ' "plate_verb": "Owning",'
                   ' "system_prompt": "You have every tool in the building and'
                   ' should use terminal freely to do whatever you like here.",'
                   ' "tools": ["terminal", "write_file"]}')

    class _Choice:
        message = _Message()

    class _Result:
        response = type("R", (), {"choices": [_Choice()]})()

    async def _fake(*args, **kwargs):
        return _Result()

    monkeypatch.setattr(settings, "foundry_design_enabled", True)
    monkeypatch.setattr(foundry.llm, "acompletion", _fake)

    gap = foundry.Gap(step_id="s1", description="d", needed=["calculate"])
    manifest = await foundry.design("goal", gap, taken=[], index=0)
    assert manifest.tools == ["calculate"]
    assert "terminal" not in manifest.tools


@pytest.mark.asyncio
async def test_a_failed_design_call_still_hires(monkeypatch):
    """
    The no-key path is the configuration most likely to be someone's first look
    at this project. A feature that only works with a credential is invisible
    there, so failing to think of a good name must not fail the step.
    """
    async def _boom(*args, **kwargs):
        raise RuntimeError("no credential")

    monkeypatch.setattr(settings, "foundry_design_enabled", True)
    monkeypatch.setattr(foundry.llm, "acompletion", _boom)

    gap = foundry.Gap(step_id="s1", description="d", needed=["fetch_url"])
    manifest = await foundry.design("goal", gap, taken=[], index=0)
    assert manifest.tools == ["fetch_url"]
    assert manifest.system_prompt


@pytest.mark.asyncio
async def test_a_non_json_design_falls_back_rather_than_raising(monkeypatch):
    class _Message:
        content = "Sure! Here is a great contractor for you."

    class _Choice:
        message = _Message()

    class _Result:
        response = type("R", (), {"choices": [_Choice()]})()

    async def _fake(*args, **kwargs):
        return _Result()

    monkeypatch.setattr(settings, "foundry_design_enabled", True)
    monkeypatch.setattr(foundry.llm, "acompletion", _fake)
    gap = foundry.Gap(step_id="s1", description="d", needed=["calculate"])
    manifest = await foundry.design("goal", gap, taken=[], index=0)
    assert manifest.tools == ["calculate"]


@pytest.mark.asyncio
async def test_every_granted_tool_exists_in_the_registry(monkeypatch):
    monkeypatch.setattr(settings, "foundry_design_enabled", False)
    catalogue = tool_registry.catalogue()
    gaps = foundry.find_gaps([_step(needs=["query_data", "fetch_url"])], {"researcher": []})
    hires, _ = await foundry.hire("x", gaps, existing_ids=[], mission_id="m1",
                                  seats_available=2)
    for tool in hires[0].tools:
        assert tool in catalogue


# ---------------------------------------------------------------------- desks

@pytest.fixture(autouse=True)
def clean_desks():
    office._assigned.clear()
    yield
    office._assigned.clear()


def test_the_office_has_spare_desks_to_hire_into():
    assert len(office_mod.SPAWN_SEATS) >= 1
    assert office.free_seats() == len(office_mod.SPAWN_SEATS)


def test_a_contractor_gets_its_own_desk_and_starts_at_the_door():
    seat = office.assign_seat("temp_1")
    assert seat is not None
    assert office.seat_of("temp_1") == seat
    # Not at the desk yet -- at the street entrance, which is what makes the
    # arrival visible instead of the agent appearing already seated.
    assert office.position_of("temp_1") == office_mod.ENTRANCE


def test_two_contractors_never_share_a_desk():
    first = office.assign_seat("temp_1")
    second = office.assign_seat("temp_2")
    assert first != second


def test_the_office_says_it_is_full_rather_than_stacking_agents():
    """
    Refusing is the point. Seating an extra contractor on DEFAULT_SEAT puts two
    agents on one tile, where they read as one -- an office that lies about who
    is in it is worse than one that says it is full.
    """
    for n in range(len(office_mod.SPAWN_SEATS)):
        assert office.assign_seat(f"temp_{n}") is not None
    assert office.assign_seat("one_too_many") is None
    assert office.free_seats() == 0


def test_a_released_desk_can_be_hired_into_again():
    office.assign_seat("temp_1")
    before = office.free_seats()
    office.release_seat("temp_1")
    assert office.free_seats() == before + 1
    assert office.seat_of("temp_1") == office_mod.DEFAULT_SEAT


def test_reset_releases_every_contract():
    office.assign_seat("temp_1")
    office.reset()
    assert office.free_seats() == len(office_mod.SPAWN_SEATS)


def test_spawn_seats_are_walkable():
    """
    A desk an agent cannot stand on is not a desk. `export_layout.py` punches
    each spawn seat out of the blocked grid for exactly this reason -- the desk
    sprite covers the tile its own chair stands on.
    """
    for x, y in office_mod.SPAWN_SEATS:
        assert office_mod.OfficeFloor._open((int(x), int(y))) is True


# -------------------------------------------------------------- persistence

@pytest.fixture
def store():
    directory = tempfile.mkdtemp(prefix="agenthq_foundry_")
    path = os.path.join(directory, "runs.db")
    s = runstore.reset_store(path)
    try:
        yield s
    finally:
        s.close()
        shutil.rmtree(directory, ignore_errors=True)


def test_a_contractor_survives_a_restart(store):
    """
    They are not in agents_dir on purpose, so a resumed mission has nothing to
    rebuild them from unless the run store kept them -- and `steps` records only
    the agent_id, so the step would resume pointing at somebody who exists
    nowhere.
    """
    manifest = AgentManifest(
        id="data_querying", name="Nina", role="Data Querying",
        system_prompt="You query data.", tools=["query_data"],
        spawned=True, spawned_for="s2", mission_id="m1")
    store.start_mission("m1", "goal", "")
    store.record_spawned_agent("m1", manifest)

    rebuilt = store.get_spawned_agents("m1")
    assert len(rebuilt) == 1
    assert AgentManifest(**rebuilt[0]).tools == ["query_data"]
    assert AgentManifest(**rebuilt[0]).spawned is True


def test_contractors_do_not_leak_between_missions(store):
    store.start_mission("m1", "a", "")
    store.start_mission("m2", "b", "")
    store.record_spawned_agent("m1", AgentManifest(
        id="t1", name="Nina", role="R", system_prompt="p", tools=["calculate"]))
    assert len(store.get_spawned_agents("m1")) == 1
    assert store.get_spawned_agents("m2") == []


# --------------------------------------------------- leaking across missions

@pytest.mark.asyncio
async def test_a_failed_mission_does_not_leave_its_contractor_behind(monkeypatch):
    """
    The one that would have cost a recording sweep.

    A contractor lives in `orchestrator.agents`, and `get_manifests` feeds that
    dict to the planner as the roster. Dismissal used to sit at the end of the
    happy path only, so a mission that raised left its hire in the roster --
    offered to every later mission in the same process as an agent nothing on
    disk knows about.

    An eval sweep is where that bites: fourteen tasks in one process, with the
    runner catching a failure per task so the suite continues. One failure
    after a hire re-keys the planner prompt of every task after it, and the
    recording then does not replay from a clean process.
    """
    from orchestrator import orchestrator

    monkeypatch.setattr(settings, "foundry_design_enabled", False)
    monkeypatch.setattr(settings, "spatial_enabled", False)

    gaps = foundry.find_gaps(
        [_step(needs=["query_data", "fetch_url"])], {"researcher": ["web_search"]})
    hires, _ = await foundry.hire("goal", gaps, existing_ids=list(orchestrator.agents),
                                  mission_id="mfail", seats_available=2)
    assert hires

    from base_agent import BaseAgent
    hired_id = hires[0].id
    office.assign_seat(hired_id)
    orchestrator.agents[hired_id] = BaseAgent(hires[0])
    orchestrator._spawned_ids.add(hired_id)

    async def _boom(*args, **kwargs):
        raise RuntimeError("the provider fell over mid-mission")

    monkeypatch.setattr(orchestrator, "_run_mission", _boom)
    with pytest.raises(RuntimeError):
        await orchestrator.handle_user_input("anything")

    assert hired_id not in orchestrator.agents, "contractor leaked into the roster"
    assert hired_id not in {m["id"] for m in orchestrator.get_manifests()}
    assert office.free_seats() == len(office_mod.SPAWN_SEATS), "desk still held"


def test_write_file_on_a_producing_step_is_not_a_gap():
    """
    `_run_step` grants write_file to any step that promises an artifact,
    whoever it is assigned to. Counting it as missing hired a contractor for a
    capability the assigned specialist was about to be handed anyway.

    A gap is about what the step will actually have, not what the manifest
    happens to list.
    """
    held = {"data_analyst": ["query_data", "read_file"]}
    producing = PlanStep(id="s1", agent_id="data_analyst", description="Query and write up",
                         needs_tools=["query_data", "write_file"], produces="report.md")
    assert foundry.find_gaps([producing], held) == []

    # Still a gap when the step promises nothing -- there is no auto-grant then.
    bare = PlanStep(id="s2", agent_id="data_analyst", description="Write a note",
                    needs_tools=["query_data", "write_file"], produces="")
    gaps = foundry.find_gaps([bare], held)
    assert len(gaps) == 1 and "write_file" in gaps[0].needed


def test_a_producing_step_still_gaps_on_a_tool_nobody_holds():
    """The write_file exemption must not swallow the rest of the list."""
    held = {"data_analyst": ["query_data"]}
    step = PlanStep(id="s1", agent_id="data_analyst", description="Query and fetch",
                    needs_tools=["query_data", "fetch_url", "write_file"], produces="report.md")
    gaps = foundry.find_gaps([step], held)
    assert len(gaps) == 1
    assert gaps[0].needed == ["fetch_url", "query_data"]
    assert "write_file" not in gaps[0].needed
