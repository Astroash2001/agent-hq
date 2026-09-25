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
import io
import json
import logging
import os
import zipfile
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import PlainTextResponse, Response

import runstore
import sessions
import tracing
from api_guard import mission_slots, safe_mission_id
from config import settings

logger = logging.getLogger("AgentHQ.API.Missions")

router = APIRouter(prefix="/api/missions", tags=["missions"])


def _mission_dir(mission_id: str) -> str:
    """
    The directory for one mission, never anything above it.

    The id comes from the URL. `%2e%2e` decodes to `..`, and joined here that
    named `backend/data` itself -- so the artifact route served `memory.json`
    to anyone who asked. Validated by shape, then by resolved path.
    """
    safe_mission_id(mission_id)
    root = os.path.realpath(settings.missions_path)
    directory = os.path.realpath(os.path.join(root, mission_id))
    if os.path.dirname(directory) != root:
        raise HTTPException(status_code=400, detail="invalid mission id")
    return directory


def _manifest_owner(directory: str) -> Optional[str]:
    """The session recorded in a mission's mission.json, or None."""
    try:
        with open(os.path.join(directory, "mission.json"), "r", encoding="utf-8") as fh:
            return json.load(fh).get("session_id")
    except (OSError, ValueError, AttributeError):
        return None


def _require_owner(mission_id: str) -> str:
    """
    The mission's directory, if the caller's session started this mission.

    404 rather than 403 for someone else's mission: a different answer would
    tell a caller that an id they guessed exists.
    """
    directory = _mission_dir(mission_id)
    row = runstore.get_store().get_mission(mission_id)
    owner = row.get("session_id") if row else _manifest_owner(directory)
    if not sessions.owns(owner):
        raise HTTPException(status_code=404, detail=f"no mission '{mission_id}'")
    return directory


@router.get("")
async def list_missions(limit: int = Query(50, ge=1, le=500)):
    """The caller's missions, newest first, merged with on-disk records."""
    store_missions = runstore.get_store().list_missions(
        limit, session_id=sessions.current())
    seen_ids = set()
    valid_missions = []

    for m in store_missions:
        m_id = m.get("id")
        if not m_id:
            continue
        mdir = m.get("mission_dir")
        if not mdir or not os.path.isdir(mdir):
            disk_dir = _mission_dir(m_id)
            if os.path.isdir(disk_dir):
                m["mission_dir"] = disk_dir
            else:
                continue
        seen_ids.add(m_id)
        valid_missions.append(m)

    if os.path.isdir(settings.missions_path):
        for entry in os.listdir(settings.missions_path):
            if entry in seen_ids or entry.startswith((".", "README", "INDEX")):
                continue
            entry_dir = os.path.join(settings.missions_path, entry)
            if not os.path.isdir(entry_dir):
                continue
            # The store lists only the caller's rows, so an id not seen there
            # may be another session's mission. Skip it unless it is on disk
            # only and its manifest names this session.
            if runstore.get_store().get_mission(entry) is not None:
                continue
            if not sessions.owns(_manifest_owner(entry_dir)):
                continue
            manifest_path = os.path.join(entry_dir, "mission.json")
            trace_path = os.path.join(entry_dir, tracing.TRACE_FILENAME)
            report_path = os.path.join(entry_dir, "report.md")
            if os.path.isfile(manifest_path) or os.path.isfile(trace_path) or os.path.isfile(report_path):
                goal = entry
                started_at = os.path.getmtime(entry_dir)
                status = "completed"
                cost_usd = 0.0
                if os.path.isfile(manifest_path):
                    try:
                        with open(manifest_path, "r", encoding="utf-8") as fh:
                            data = json.load(fh)
                        goal = data.get("goal") or goal
                        status = data.get("outcome") or status
                        cost_usd = float(data.get("cost_usd") or 0.0)
                    except Exception:
                        pass
                seen_ids.add(entry)
                valid_missions.append({
                    "id": entry,
                    "goal": goal,
                    "status": status,
                    "plan_source": "disk",
                    "started_at": started_at,
                    "ended_at": started_at,
                    "cost_usd": cost_usd,
                    "mission_dir": entry_dir,
                })

    valid_missions.sort(key=lambda x: x.get("started_at") or 0, reverse=True)
    return {"missions": valid_missions[:limit], "count": len(valid_missions[:limit])}


@router.get("/{mission_id}")
async def get_mission(mission_id: str):
    """
    One mission: its plan, its steps, its report, and artifact details.
    """
    safe_dir = _require_owner(mission_id)
    mission = runstore.get_store().get_mission(mission_id)
    directory = (mission.get("mission_dir") if mission else None) or safe_dir
    if not os.path.isdir(directory):
        directory = safe_dir

    if not mission:
        if os.path.isdir(directory):
            manifest_path = os.path.join(directory, "mission.json")
            if os.path.isfile(manifest_path):
                try:
                    with open(manifest_path, "r", encoding="utf-8") as fh:
                        manifest = json.load(fh)
                    mission = {
                        "id": mission_id,
                        "goal": manifest.get("goal", mission_id),
                        "status": manifest.get("outcome", "completed"),
                        "plan": {"steps": manifest.get("steps", [])},
                        "steps": manifest.get("steps", []),
                        "cost_usd": float(manifest.get("cost_usd") or 0.0),
                        "started_at": os.path.getmtime(manifest_path),
                        "mission_dir": directory,
                    }
                except Exception:
                    pass
        if not mission:
            raise HTTPException(status_code=404, detail=f"no mission '{mission_id}'")

    mission["mission_dir"] = directory
    artifacts = (
        sorted(f for f in os.listdir(directory)
               if os.path.isfile(os.path.join(directory, f)))
        if os.path.isdir(directory) else []
    )
    mission["artifacts"] = artifacts
    mission["has_trace"] = os.path.isfile(
        os.path.join(directory, tracing.TRACE_FILENAME))

    # Read report
    report_text = ""
    for cand in ("report.md", "final_report.md"):
        cand_path = os.path.join(directory, cand)
        if os.path.isfile(cand_path):
            try:
                with open(cand_path, "r", encoding="utf-8") as fh:
                    report_text = fh.read()
                break
            except Exception:
                pass

    if not report_text:
        for f in artifacts:
            if f.endswith(".md") and f not in ("INDEX.md", "README.md"):
                cand_path = os.path.join(directory, f)
                try:
                    with open(cand_path, "r", encoding="utf-8") as fh:
                        report_text = fh.read()
                    break
                except Exception:
                    pass

    mission["report"] = report_text

    # Read artifact details for FILES tab
    artifact_details = []
    for f in artifacts:
        if f in ("trace.jsonl", "INDEX.md", "README.md"):
            continue
        f_path = os.path.join(directory, f)
        try:
            sz = os.path.getsize(f_path)
            if sz < 500_000:
                with open(f_path, "r", encoding="utf-8", errors="replace") as fh:
                    content = fh.read()
                artifact_details.append({
                    "name": f,
                    "size_chars": len(content),
                    "content": content[:20000],
                    "truncated": len(content) > 20000,
                    "author_name": "workspace",
                })
        except Exception:
            pass

    mission["artifact_details"] = artifact_details
    return mission


@router.get("/{mission_id}/artifacts/{name}", response_class=PlainTextResponse)
async def get_artifact(mission_id: str, name: str):
    """
    One artifact's text, off disk.

    `get_mission` lists the filenames and stops there, so a replayed mission
    could show the work happening and not what the work said -- the trace holds
    spans, not bodies, and putting bodies in it would bloat the one file that
    has to stay readable when a process is killed mid-write.

    The name is resolved and then checked to be inside the mission directory
    rather than pattern-matched. `..%2f` and an absolute path both survive URL
    decoding, and a blocklist of shapes is a losing game against a filename that
    reaches the filesystem; comparing the resolved path is not.
    """
    directory = _require_owner(mission_id)
    target = os.path.realpath(os.path.join(directory, name))
    if os.path.commonpath([directory, target]) != directory or target == directory:
        raise HTTPException(status_code=400, detail="artifact name escapes the mission")
    if not os.path.isfile(target):
        raise HTTPException(
            status_code=404, detail=f"no artifact '{name}' in mission '{mission_id}'")

    try:
        with open(target, encoding="utf-8") as fh:
            return fh.read()
    except UnicodeDecodeError:
        raise HTTPException(status_code=415, detail=f"'{name}' is not a text artifact")
    except OSError as exc:
        raise HTTPException(status_code=500,
                            detail=f"could not read artifact: {exc}") from exc


@router.get("/{mission_id}/bundle")
async def get_bundle(mission_id: str):
    """
    Every file of one mission, as a single zip.

    The files tab lists them one at a time, so keeping a finished mission meant
    saving each artifact by hand -- and on a deployment with no disk, a mission
    is gone at the next restart. One download takes the report, the artifacts,
    the manifest and the trace together.

    Built in memory: a mission directory is a handful of markdown files plus a
    trace, and streaming a temporary file would leave one behind on a killed
    process.
    """
    directory = _require_owner(mission_id)
    if not os.path.isdir(directory):
        raise HTTPException(status_code=404, detail=f"no mission '{mission_id}'")

    buffer = io.BytesIO()
    written = 0
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in sorted(os.listdir(directory)):
            path = os.path.join(directory, name)
            if not os.path.isfile(path):
                continue
            try:
                archive.write(path, arcname=f"{mission_id}/{name}")
                written += 1
            except OSError as exc:
                logger.warning("[%s] could not add %s to the bundle: %s",
                               mission_id, name, exc)
    if not written:
        raise HTTPException(status_code=404,
                            detail=f"mission '{mission_id}' has no files to download")

    return Response(
        content=buffer.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{mission_id}.zip"'},
    )


@router.get("/{mission_id}/trace", response_class=PlainTextResponse)
async def get_trace(mission_id: str):
    """The mission's spans, as the newline-delimited JSON they were written as."""
    safe_dir = _require_owner(mission_id)
    mission = runstore.get_store().get_mission(mission_id)
    directory = (mission or {}).get("mission_dir") or safe_dir
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

    safe_mission_id(mission_id)
    store = runstore.get_store()
    mission = store.get_mission(mission_id)
    if not mission or not sessions.owns(mission.get("session_id")):
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

    # The same cap a new mission obeys: a resume spends exactly as much.
    if not mission_slots.try_acquire():
        raise HTTPException(
            status_code=429,
            detail="a mission is already running; resume it when that one finishes")
    try:
        return await mission_slots.run(orchestrator.handle_user_input(
            mission["goal"], resume_from=mission_id))
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc))
