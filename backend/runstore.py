"""
Where a mission's progress lives while it is still happening.

`orchestrator.active_tasks` and `self.workspaces` are in-memory dicts, and
`mission.json` is written once, at the very end. Kill the process halfway
through a five-step mission and the run is gone -- not merely unfinished, but
unrecorded: there is no answer to "which steps completed" beyond the artifact
files lying in the mission directory with nothing to say which step wrote them
or what was still owed.

So every status change is written here first and only then emitted as an event.
Write-through in that order on purpose: if the DB is written after the event,
a process killed in the gap has shown the user a completed step it has no record
of, and a resume would run it twice.

SQLite via the stdlib rather than a service. It adds no dependency, the whole
store is one file you can open with any tool, and WAL mode plus short
transactions mean a hard kill leaves a readable database rather than a lock file
and a corrupt page. That matters more here than throughput ever will: this
writes a handful of rows per mission.
"""
import json
import logging
import os
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional

from config import settings

logger = logging.getLogger("AgentHQ.RunStore")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS missions (
    id           TEXT PRIMARY KEY,
    goal         TEXT NOT NULL,
    status       TEXT NOT NULL,
    plan_source  TEXT,
    plan_json    TEXT,
    started_at   REAL,
    ended_at     REAL,
    cost_usd     REAL DEFAULT 0.0,
    mission_dir  TEXT
);

CREATE TABLE IF NOT EXISTS steps (
    mission_id    TEXT NOT NULL,
    step_id       TEXT NOT NULL,
    agent_id      TEXT,
    status        TEXT NOT NULL,
    attempt       INTEGER DEFAULT 0,
    produces      TEXT,
    consumes      TEXT,
    artifact_path TEXT,
    started_at    REAL,
    ended_at      REAL,
    tokens        INTEGER DEFAULT 0,
    cost_usd      REAL DEFAULT 0.0,
    error         TEXT,
    PRIMARY KEY (mission_id, step_id)
);

CREATE INDEX IF NOT EXISTS idx_steps_mission ON steps(mission_id);
"""


class RunStore:
    """
    One SQLite file recording missions and their steps as they happen.

    Never raises at the call site. A harness that dies because its bookkeeping
    could not be written has turned a durability feature into a new way to lose
    a run, so every failure here is a logged warning and the mission continues
    exactly as it did before this module existed.
    """

    def __init__(self, path: Optional[str] = None):
        self.path = path or settings.runstore_path
        # sqlite3 connections are not safe to share across threads by default,
        # and the event loop runs these from whichever thread it likes. One
        # connection plus one lock is far simpler than a pool for a workload
        # measured in rows per minute.
        self._lock = threading.Lock()
        self._conn: Optional[sqlite3.Connection] = None
        self._broken = False
        self._connect()

    # -------------------------------------------------------------- plumbing

    def _connect(self) -> None:
        try:
            directory = os.path.dirname(os.path.abspath(self.path))
            os.makedirs(directory, exist_ok=True)
            self._conn = sqlite3.connect(self.path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            # WAL so a reader (the API) never blocks the writer (a running
            # mission), and so a hard kill leaves a recoverable file.
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()
        except (sqlite3.Error, OSError, ValueError) as exc:
            # OSError and ValueError as well as sqlite3.Error: a path that
            # cannot exist fails in os.makedirs before sqlite is reached at all,
            # and a NUL byte in a setting raises the latter. Losing the store
            # must never be able to take the mission with it.
            logger.warning("run store unavailable at %s: %s", self.path, exc)
            self._conn = None
            self._broken = True

    def _write(self, sql: str, params: tuple = ()) -> None:
        """One statement in one short transaction."""
        if self._conn is None or not settings.runstore_enabled:
            return
        try:
            with self._lock:
                self._conn.execute(sql, params)
                self._conn.commit()
        except sqlite3.Error as exc:
            logger.warning("run store write failed: %s", exc)

    def _read(self, sql: str, params: tuple = ()) -> List[sqlite3.Row]:
        if self._conn is None:
            return []
        try:
            with self._lock:
                return list(self._conn.execute(sql, params).fetchall())
        except sqlite3.Error as exc:
            logger.warning("run store read failed: %s", exc)
            return []

    def close(self) -> None:
        if self._conn is not None:
            with self._lock:
                self._conn.close()
            self._conn = None

    # -------------------------------------------------------------- missions

    def start_mission(self, mission_id: str, goal: str, mission_dir: str = "") -> None:
        self._write(
            "INSERT OR REPLACE INTO missions "
            "(id, goal, status, started_at, mission_dir) VALUES (?,?,?,?,?)",
            (mission_id, goal, "running", time.time(), mission_dir or ""))

    def record_plan(self, mission_id: str, plan) -> None:
        """
        Store the plan as soon as it exists.

        `mission.json` carries the plan too, but it is written only when a
        mission finishes -- so a killed run leaves none, and there would be
        nothing to resume *from*. This row is what makes the DB self-sufficient.
        """
        try:
            payload = json.dumps({
                "goal": plan.goal,
                "source": plan.source,
                "steps": [s.model_dump() for s in plan.steps],
            })
        except (TypeError, ValueError) as exc:
            logger.warning("plan not serialisable for %s: %s", mission_id, exc)
            return
        self._write("UPDATE missions SET plan_source=?, plan_json=? WHERE id=?",
                    (plan.source, payload, mission_id))

    def end_mission(self, mission_id: str, status: str,
                    cost_usd: Optional[float] = 0.0) -> None:
        """
        Give a mission its terminal status.

        `cost_usd=None` leaves the recorded cost alone, which is not the same
        as passing 0.0. A mission that ends by raising knows its status but not
        its bill, and a resumed mission already carries what the first run
        spent -- writing a confident zero over that loses money the run really
        did cost.
        """
        if cost_usd is None:
            self._write("UPDATE missions SET status=?, ended_at=? WHERE id=?",
                        (status, time.time(), mission_id))
            return
        self._write(
            "UPDATE missions SET status=?, ended_at=?, cost_usd=? WHERE id=?",
            (status, time.time(), float(cost_usd or 0.0), mission_id))

    def get_mission(self, mission_id: str) -> Optional[Dict[str, Any]]:
        rows = self._read("SELECT * FROM missions WHERE id=?", (mission_id,))
        if not rows:
            return None
        mission = dict(rows[0])
        if mission.get("plan_json"):
            try:
                mission["plan"] = json.loads(mission["plan_json"])
            except ValueError:
                mission["plan"] = None
        mission.pop("plan_json", None)
        mission["steps"] = self.get_steps(mission_id)
        return mission

    def list_missions(self, limit: int = 50) -> List[Dict[str, Any]]:
        rows = self._read(
            "SELECT id, goal, status, plan_source, started_at, ended_at, "
            "cost_usd, mission_dir FROM missions "
            "ORDER BY started_at DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]

    # ----------------------------------------------------------------- steps

    def upsert_step(self, mission_id: str, step, status: str,
                    attempt: int = 0, **fields: Any) -> None:
        """
        Record a step's current state.

        Called on every transition -- pending, running, completed, failed -- and
        always *before* the matching event reaches the bus.
        """
        self._write(
            "INSERT INTO steps (mission_id, step_id, agent_id, status, attempt, "
            "produces, consumes, started_at) VALUES (?,?,?,?,?,?,?,?) "
            "ON CONFLICT(mission_id, step_id) DO UPDATE SET "
            "status=excluded.status, attempt=excluded.attempt, "
            "agent_id=excluded.agent_id",
            (mission_id, step.id, step.agent_id, status, attempt,
             step.produces or "", json.dumps(list(step.consumes or [])),
             time.time()))
        if fields:
            self.finish_step(mission_id, step.id, status, **fields)

    def finish_step(self, mission_id: str, step_id: str, status: str,
                    tokens: int = 0, cost_usd: float = 0.0,
                    error: str = "", artifact_path: str = "") -> None:
        self._write(
            "UPDATE steps SET status=?, ended_at=?, tokens=?, cost_usd=?, "
            "error=?, artifact_path=? WHERE mission_id=? AND step_id=?",
            (status, time.time(), int(tokens or 0), float(cost_usd or 0.0),
             (error or "")[:1000], artifact_path or "", mission_id, step_id))

    def get_steps(self, mission_id: str) -> List[Dict[str, Any]]:
        rows = self._read(
            "SELECT * FROM steps WHERE mission_id=? ORDER BY step_id", (mission_id,))
        out = []
        for row in rows:
            step = dict(row)
            try:
                step["consumes"] = json.loads(step.get("consumes") or "[]")
            except ValueError:
                step["consumes"] = []
            out.append(step)
        return out

    def completed_step_ids(self, mission_id: str) -> List[str]:
        """
        Steps a resume may treat as already satisfied.

        Only `completed`. A step recorded as `running` was in flight when the
        process died and its artifact may be half-written or absent, so it is
        re-run -- the whole point of recording the transition before the event
        is that "running" is known to mean "started, outcome unknown".
        """
        return [r["step_id"] for r in self._read(
            "SELECT step_id FROM steps WHERE mission_id=? AND status='completed'",
            (mission_id,))]


_store: Optional[RunStore] = None


def get_store() -> RunStore:
    """The process-wide store, opened on first use rather than at import so a
    test can point `settings.runstore_path` somewhere else first."""
    global _store
    if _store is None:
        _store = RunStore()
    return _store


def reset_store(path: Optional[str] = None) -> RunStore:
    """Reopen the store, for tests and for a settings change at runtime."""
    global _store
    if _store is not None:
        _store.close()
    _store = RunStore(path)
    return _store
