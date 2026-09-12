"""
Mission workspace -- the shared blackboard a single mission writes into.

Before this, every specialist saw only its own `task.description` and the boss
glued the strings together at the end, so the "handoffs" in the briefing were
presentational. Now each step writes a named artifact here, and the steps that
depend on it receive that artifact's text in their prompt. The handoff is real.

It is also the thing the office draws: `artifact.created` fires when a desk
produces something, `artifact.delivered` when it is carried to another desk.

Artifacts are mirrored to disk as they are filed. The store itself is in
memory and dies with the mission, and the result only ever reached the browser
over the websocket -- so closing the tab destroyed the work. Every mission now
leaves a directory behind under `data/missions/`.
"""
import json
import logging
import os
import re
from typing import Any, Dict, List, Optional

from config import settings
from event_bus import event_bus
from schemas import Artifact, Event, EventType

logger = logging.getLogger("AgentHQ.Workspace")

# How much of an upstream artifact is pasted into a downstream agent's prompt.
# Generous enough to carry substantial tables, JSON data and findings without truncation,
# while safely fitting within modern LLM context windows.
MAX_INJECTED_CHARS = 12000

# How the budget splits between the start and the end of a long artifact. Not
# 100/0, which is what a plain `body[:limit]` is: a report puts its findings and
# its recommendation at the *end*, so cutting the tail hands the next agent the
# introduction and throws away the conclusion it was supposed to build on.
HEAD_SHARE = 0.6

# Below this there is no room to keep both ends and say so, and a head-only cut
# is the honest fallback.
MIN_SPLIT_CHARS = 400

# Artifact names come from the model, so they reach the filesystem untrusted.
# Anything that could climb out of the mission directory is stripped rather
# than rejected -- a bad name should not lose the artifact.
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _break_after(text: str, at: int) -> int:
    """
    Nearest line break at or before `at`, so a cut lands between lines.

    Falls back to a space, then to the raw index. Cutting mid-word is not a
    correctness problem, but it makes the elision read as corruption rather than
    as an omission, and an agent that thinks its input is corrupt behaves worse
    than one that knows a middle is missing.
    """
    window = text[:at]
    for sep in ("\n\n", "\n", " "):
        found = window.rfind(sep)
        if found > at * 0.5:
            return found
    return at


def _break_before(text: str, at: int) -> int:
    """Nearest line break at or after `at`, for the start of the kept tail."""
    tail_zone = text[at:]
    for sep in ("\n\n", "\n", " "):
        found = tail_zone.find(sep)
        if found != -1 and found < len(tail_zone) * 0.5:
            return at + found + len(sep)
    return at


def condense(body: str, limit: int = MAX_INJECTED_CHARS) -> str:
    """
    Fit `body` into `limit` characters, keeping both ends.

    `body[:limit]` was what this used to be, and it loses the tail -- which for
    a report is the findings, the recommendation and the numbers everything
    downstream was waiting for. The next agent received an introduction and an
    abrupt stop, with nothing to tell it the substance had been cut.

    Deliberately not a model call. Summarising every handoff would put a
    provider round-trip and a bill on an operation that runs once per edge of
    the DAG, and would introduce a place where the artifact a downstream agent
    reads is not the artifact its author wrote. This keeps the author's own
    words at both ends and says exactly how much is missing in between.
    """
    body = body or ""
    if len(body) <= limit:
        return body

    if limit < MIN_SPLIT_CHARS:
        return body[:_break_after(body, limit)] + "\n... [truncated]"

    marker_budget = 80
    keep = limit - marker_budget
    head_len = int(keep * HEAD_SHARE)
    tail_len = keep - head_len

    head_end = _break_after(body, head_len)
    tail_start = _break_before(body, len(body) - tail_len)
    elided = tail_start - head_end
    if elided <= 0:
        return body

    return (f"{body[:head_end].rstrip()}\n\n"
            f"... [{elided:,} characters elided from the middle] ...\n\n"
            f"{body[tail_start:].lstrip()}")


def _safe_name(name: str) -> str:
    cleaned = _UNSAFE.sub("_", os.path.basename(name or "")).lstrip(".")
    return cleaned[:120] or "untitled.md"


class MissionWorkspace:
    """Artifact store for one mission, mirrored to a directory on disk."""

    def __init__(self, mission_id: str):
        self.mission_id = mission_id
        self.artifacts: Dict[str, Artifact] = {}
        self.dir: Optional[str] = None
        if settings.persist_missions:
            try:
                self.dir = os.path.join(settings.missions_path, _safe_name(mission_id))
                os.makedirs(self.dir, exist_ok=True)
            except (OSError, ValueError) as exc:
                # Losing the mirror must never lose the mission. The run keeps
                # going in memory exactly as it did before persistence existed.
                # ValueError as well as OSError: a path with a NUL byte in it
                # raises the former, and a bad setting must not kill the run.
                logger.warning("[%s] cannot create mission directory: %s", mission_id, exc)
                self.dir = None

    def _write(self, filename: str, body: str) -> Optional[str]:
        """Write one file into the mission directory. Never raises."""
        if not self.dir:
            return None
        path = os.path.join(self.dir, _safe_name(filename))
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(body)
            return path
        except (OSError, ValueError) as exc:
            logger.warning("[%s] could not write %s: %s", self.mission_id, filename, exc)
            return None

    def save_report(self, text: str) -> Optional[str]:
        """The final synthesis, as the mission's headline file."""
        return self._write("report.md", text or "")

    def save_manifest(self, payload: Dict[str, Any]) -> Optional[str]:
        """
        Goal, plan, per-step status and totals for one run.

        This is what makes two runs comparable -- the artifacts alone say what
        was produced but not what was asked, planned, or how it went.
        """
        if not self.dir:
            return None
        try:
            return self._write("mission.json", json.dumps(payload, indent=2, default=str))
        except (TypeError, ValueError) as exc:
            logger.warning("[%s] manifest not serialisable: %s", self.mission_id, exc)
            return None

    async def put(
        self,
        name: str,
        content: str,
        author_id: str,
        author_name: str,
        step_id: str = ""
    ) -> Artifact:
        """Store an artifact and announce it to the office."""
        artifact = Artifact(
            name=name,
            content=content,
            author_id=author_id,
            author_name=author_name,
            step_id=step_id,
            size_chars=len(content)
        )
        self.artifacts[name] = artifact
        self._write(name, content)

        await event_bus.emit(Event(
            type=EventType.ARTIFACT_CREATED,
            agent_id=author_id,
            task_id=self.mission_id,
            data={
                "name": name,
                "author_id": author_id,
                "author_name": author_name,
                "step_id": step_id,
                "size_chars": artifact.size_chars,
                "preview": content[:180]
            }
        ))
        logger.info(f"[{self.mission_id}] {author_name} produced artifact '{name}' "
                    f"({artifact.size_chars} chars)")
        return artifact

    async def deliver(self, names: List[str], to_agent_id: str, to_agent_name: str, step_id: str = "") -> List[Artifact]:
        """
        Hand a set of artifacts to an agent.

        Emits one event per artifact carrying both endpoints, which is what lets
        the renderer fly an object from the author's desk to the recipient's.
        """
        delivered: List[Artifact] = []
        for name in names:
            artifact = self.artifacts.get(name)
            if not artifact:
                logger.debug(f"[{self.mission_id}] artifact '{name}' requested but not present")
                continue
            delivered.append(artifact)
            await event_bus.emit(Event(
                type=EventType.ARTIFACT_DELIVERED,
                agent_id=to_agent_id,
                task_id=self.mission_id,
                data={
                    "name": name,
                    "from_agent_id": artifact.author_id,
                    "from_agent_name": artifact.author_name,
                    "to_agent_id": to_agent_id,
                    "to_agent_name": to_agent_name,
                    "step_id": step_id,
                    "size_chars": artifact.size_chars
                }
            ))
        return delivered

    def get(self, name: str) -> Optional[Artifact]:
        return self.artifacts.get(name)

    def names(self) -> List[str]:
        return list(self.artifacts.keys())

    def render_context(self, names: List[str]) -> str:
        """
        Format artifacts as the prompt block a downstream specialist receives.

        Returns "" when nothing is available, so callers can test truthiness
        rather than special-casing the first step of a plan.
        """
        blocks = []
        for name in names:
            artifact = self.artifacts.get(name)
            if not artifact:
                continue
            is_truncated = len(artifact.content) > MAX_INJECTED_CHARS
            body = condense(artifact.content, MAX_INJECTED_CHARS)
            prefix = ""
            if is_truncated:
                prefix = (
                    f"⚠️ [NOTE: Upstream artifact '{name}' has {len(artifact.content):,} characters and was partially "
                    f"condensed for this prompt snippet. The complete file exists in your workspace as '{name}'. "
                    f"Use your 'read_file' tool to inspect the complete file on disk before extracting or transforming data!]\n\n"
                )
            blocks.append(
                f"--- {name} (from {artifact.author_name}) ---\n{prefix}{body}"
            )
        if not blocks:
            return ""
        return (
            "Work already delivered to your desk by colleagues. Build on it; "
            "do not repeat it.\n\n" + "\n\n".join(blocks)
        )

    # A mission produces a handful of markdown files, so the bodies ride along
    # in the completion payload rather than needing a fetch per artifact. The
    # cap is a guard against one runaway artifact, not an expected path.
    MAX_BODY_CHARS = 20000

    def summary(self) -> List[Dict[str, Any]]:
        """
        Artifact index for the mission result payload, bodies included.

        The bodies are here because the UI has nowhere else to get them: the
        store is in-memory and dies with the mission, and `artifact.created`
        carries only a 180-char preview. Without them the output panel can
        list what was produced but never show it.
        """
        out = []
        for a in self.artifacts.values():
            body = a.content or ""
            clipped = len(body) > self.MAX_BODY_CHARS
            out.append({
                "name": a.name,
                "author_id": a.author_id,
                "author_name": a.author_name,
                "step_id": a.step_id,
                "size_chars": a.size_chars,
                "created_at": a.created_at,
                "content": body[:self.MAX_BODY_CHARS],
                "truncated": clipped,
            })
        return out
