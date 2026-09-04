"""
Mission history, resume, and traces.

A separate router rather than four more handlers in `main.py`, which already
carries every endpoint inline and is the file most likely to be edited by
something else at the same time. It also keeps the durable-execution surface in
one place: these three reads and one write are the whole external contract for
"what has this office run, and can it carry on".

`GET /api/missions/{id}/trace` returns NDJSON as text rather than parsed JSON on
purpose -- the file is line-delimited so a partially written one from a killed
process is still readable, and parsing it server-side would throw that property
away at exactly the moment it matters.
"""
import json
import logging
import os

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import PlainTextResponse

import runstore
import tracing
from config import settings

logger = logging.getLogger("AgentHQ.API.Missions")

router = APIRouter(prefix="/api/missions", tags=["missions"])


def _mission_dir(mission_id: str) -> str:
    return os.path.join(settings.missions_path, mission_id)


@router.get("")
async def list_missions(limit: int = Query(50, ge=1, le=500)):
    """Every mission the run store knows about, newest first."""
    missions = runstore.get_store().list_missions(limit)
    return {"missions": missions, "count": len(missions)}


@router.get("/{mission_id}")
async def get_mission(mission_id: str):
    """
    One mission: its plan, its steps and their status.

    Served from the run store rather than from `mission.json`, because the
    manifest is only written when a mission finishes -- and an unfinished
    mission is the one you actually want to look at.
    """
    mission = runstore.get_store().get_mission(mission_id)
    if not mission:
        raise HTTPException(status_code=404, detail=f"no mission '{mission_id}'")

    directory = mission.get("mission_dir") or _mission_dir(mission_id)
    mission["artifacts"] = (
        sorted(f for f in os.listdir(directory)
               if os.path.isfile(os.path.join(directory, f)))
        if os.path.isdir(directory) else [])
    mission["has_trace"] = os.path.isfile(
        os.path.join(directory, tracing.TRACE_FILENAME))
    return mission


@router.get("/{mission_id}/trace", response_class=PlainTextResponse)
async def get_trace(mission_id: str):
    """The mission's spans, as the newline-delimited JSON they were written as."""
    mission = runstore.get_store().get_mission(mission_id)
    directory = (mission or {}).get("mission_dir") or _mission_dir(mission_id)
    path = os.path.join(directory, tracing.TRACE_FILENAME)
    if not os.path.isfile(path):
        raise HTTPException(status_code=404,
                            detail=f"no trace for mission '{mission_id}'")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"could not read trace: {exc}")


@router.post("/{mission_id}/resume")
async def resume_mission(mission_id: str):
    """
    Carry on a mission a killed process left unfinished.

    Steps recorded `completed` are skipped and their artifacts are read back off
    disk; everything else -- pending, failed, and anything left `running` when
    the process died -- is run again. A step that was in flight has an unknown
    outcome and a possibly half-written artifact, so re-running it is the only
    safe reading of that state.
    """
    from orchestrator import orchestrator

    store = runstore.get_store()
    mission = store.get_mission(mission_id)
    if not mission:
        raise HTTPException(status_code=404, detail=f"no mission '{mission_id}'")
    if mission.get("status") == "completed":
        raise HTTPException(
            status_code=409,
            detail=f"mission '{mission_id}' already completed; "
                   f"submit it as a new task to run it again")
    if not mission.get("plan"):
        raise HTTPException(
            status_code=409,
            detail=f"mission '{mission_id}' died before planning finished, "
                   f"so there is nothing to resume")

    try:
        return await orchestrator.handle_user_input(
            mission["goal"], resume_from=mission_id)
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc))
