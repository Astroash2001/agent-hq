"""
A span recorder for one mission.

A finished mission left `mission.json`, `report.md` and its artifacts -- what
was produced, and nothing about how. Which turn was slow, which tool call
blocked on a station, how many attempts the model needed, where the money
went: all of it existed only as log lines in a terminal nobody kept.

This records the shape of a run as nested spans and writes them to
`trace.jsonl` in the mission directory. NDJSON rather than one JSON document
because a process killed mid-mission still leaves every completed line
readable -- which is the point, since the resume path and the office's replay
mode are both meant to read this file rather than the orchestrator's memory.

Spans are appended as they close, so the file is durable at every instant;
`Trace.finalise()` then rewrites it ordered by start time, because close order
is not start order and a reader wants the run in the order it happened.

Parentage travels in a ContextVar rather than through every signature. asyncio
copies the context when a task is created, so a step span opened in `_run_step`
is automatically the parent of that agent's turn and tool spans, and two steps
running under `asyncio.gather` cannot see each other's.
"""
import contextvars
import itertools
import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from config import settings

logger = logging.getLogger("AgentHQ.Tracing")

TRACE_FILENAME = "trace.jsonl"

# Opening order, as a tie-break on t_start.
#
# time.time() is about 15ms granular on Windows, so two steps dispatched into
# the same wave are routinely stamped with the identical start time. Sorting on
# t_start alone then falls back to the order the spans *closed* in, which is the
# one thing finalise() exists to correct -- observed: a fan-out of two steps
# written to the file shortest-first. The counter is monotonic within a process
# and rides along in the file, so a replay reader can order it the same way.
_sequence = itertools.count()


# ------------------------------------------------------------------- span

@dataclass
class Span:
    """One timed unit of work: a mission, a step, a turn, a tool, a call."""
    span_id: str
    parent_id: Optional[str]
    kind: str                       # mission | step | turn | tool | llm
    name: str
    t_start: float
    seq: int = 0                    # opening order, tie-breaks a coarse clock
    t_end: Optional[float] = None
    attrs: Dict[str, Any] = field(default_factory=dict)
    status: str = "ok"              # ok | error
    error: Optional[str] = None

    @property
    def duration_sec(self) -> float:
        return round((self.t_end or time.time()) - self.t_start, 4)

    def set(self, **attrs: Any) -> None:
        """Attach attributes after the span opened -- token counts and costs
        are only known once the call inside it has answered."""
        self.attrs.update(_clip(attrs))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "span_id": self.span_id,
            "parent_id": self.parent_id,
            "kind": self.kind,
            "name": self.name,
            "seq": self.seq,
            "t_start": round(self.t_start, 4),
            "t_end": round(self.t_end, 4) if self.t_end is not None else None,
            "duration_sec": self.duration_sec,
            "status": self.status,
            "error": self.error,
            "attrs": self.attrs,
        }


def _clip(attrs: Dict[str, Any]) -> Dict[str, Any]:
    """
    Keep a stray artifact body out of the trace.

    Attributes are written on every span, and an agent's reply or a search
    result pasted in whole would make the file larger than the work it
    describes.
    """
    limit = settings.trace_max_attr_chars
    out: Dict[str, Any] = {}
    for key, value in attrs.items():
        if isinstance(value, str) and len(value) > limit:
            value = value[:limit] + "..."
        out[key] = value
    return out


# ------------------------------------------------------------------ trace

class Trace:
    """Every span of one mission, mirrored to disk as it happens."""

    def __init__(self, mission_id: str, directory: Optional[str] = None):
        self.mission_id = mission_id
        self.spans: List[Span] = []
        self.path: Optional[str] = (
            os.path.join(directory, TRACE_FILENAME) if directory else None
        )
        if self.path:
            # Truncate a trace left by an earlier run of the same mission id,
            # so a re-run does not read as two runs interleaved into one.
            self._write("w", "")

    def _write(self, mode: str, body: str) -> None:
        """
        Best-effort, in the same spirit as `workspace._write`.

        Losing the trace must never lose the mission: an unwritable directory
        or a bad path is a warning, and the run carries on with its spans in
        memory.
        """
        if not self.path:
            return
        try:
            with open(self.path, mode, encoding="utf-8") as fh:
                fh.write(body)
        except (OSError, ValueError) as exc:
            logger.warning("[%s] could not write trace: %s", self.mission_id, exc)
            self.path = None        # stop trying; one warning is enough

    def append(self, node: "Span") -> None:
        self.spans.append(node)
        try:
            line = json.dumps(node.to_dict(), default=str)
        except (TypeError, ValueError) as exc:
            logger.warning("[%s] span %s not serialisable: %s",
                           self.mission_id, node.name, exc)
            return
        self._write("a", line + "\n")
        _export_otel(node)

    def to_ndjson(self) -> str:
        ordered = sorted(self.spans, key=lambda s: (s.t_start, s.seq))
        return "".join(json.dumps(s.to_dict(), default=str) + "\n" for s in ordered)

    def finalise(self) -> Optional[str]:
        """Rewrite the file in start order. A wave of parallel steps finishes
        shuffled, so append order is not the order the run happened in."""
        if not self.path:
            return None
        path = self.path
        self._write("w", self.to_ndjson())
        return path

    def root(self) -> Optional["Span"]:
        return next((s for s in self.spans if s.parent_id is None), None)

    def totals(self) -> Dict[str, Any]:
        """Cost and tokens summed off the llm spans, for the caller that has
        to report a mission figure without keeping a second ledger."""
        cost = sum(float(s.attrs.get("cost_usd", 0.0) or 0.0)
                   for s in self.spans if s.kind == "llm")
        tokens = sum(int(s.attrs.get("total_tokens", 0) or 0)
                     for s in self.spans if s.kind == "llm")
        return {"cost_usd": round(cost, 6), "total_tokens": tokens}


# ---------------------------------------------------------------- context

_current_trace: contextvars.ContextVar[Optional[Trace]] = contextvars.ContextVar(
    "agenthq_trace", default=None)
_current_span: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "agenthq_span", default=None)


def current_trace() -> Optional[Trace]:
    return _current_trace.get()


def start_mission(mission_id: str, directory: Optional[str] = None) -> Optional[Trace]:
    """Open a trace for this mission and make it the one spans attach to."""
    if not settings.tracing_enabled:
        return None
    trace = Trace(mission_id, directory)
    _current_trace.set(trace)
    _current_span.set(None)
    return trace


def end_mission() -> Optional[str]:
    """Flush the trace in start order and detach it. Safe to call twice."""
    trace = _current_trace.get()
    if trace is None:
        return None
    path = trace.finalise()
    _current_trace.set(None)
    _current_span.set(None)
    return path


@asynccontextmanager
async def span(kind: str, name: str, **attrs: Any):
    """
    Time a block of work as a span, and record what it did.

    Always yields a `Span`, even when tracing is off or no mission is open, so
    a call site never has to guard the `async with` or check for None before
    setting an attribute on it.

    An exception is recorded as `status="error"` with its type and message,
    then re-raised. Swallowing it here would turn a failed step into a silently
    successful one, which is the opposite of what a trace is for.
    """
    trace = _current_trace.get()
    recording = settings.tracing_enabled and trace is not None

    node = Span(
        span_id=uuid.uuid4().hex[:12],
        parent_id=_current_span.get(),
        kind=kind,
        name=name,
        t_start=time.time(),
        seq=next(_sequence),
        attrs=_clip(attrs),
    )

    token = _current_span.set(node.span_id) if recording else None
    try:
        yield node
    except BaseException as exc:
        # BaseException, not Exception: an emergency stop arrives as
        # CancelledError, which is not an Exception subclass, and a step that
        # was halted should be visible in the trace as halted rather than
        # missing from it.
        node.status = "error"
        node.error = f"{type(exc).__name__}: {exc}"[:settings.trace_max_attr_chars]
        raise
    finally:
        node.t_end = time.time()
        if token is not None:
            _current_span.reset(token)
        if recording:
            trace.append(node)


# ------------------------------------------------------------------- otel

_otel_exporter: Any = None
_otel_broken = False


def _export_otel(node: Span) -> None:
    """
    Mirror a finished span to an OTLP collector, if one is configured.

    Imported inside the function on purpose: opentelemetry is a dev-optional
    package, and a missing import must not be able to stop the app starting.
    A failure disables further attempts rather than warning once per span for
    the rest of the run.
    """
    global _otel_exporter, _otel_broken
    if not settings.otel_enabled or not settings.otel_endpoint or _otel_broken:
        return
    try:
        if _otel_exporter is None:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                OTLPSpanExporter)
            _otel_exporter = OTLPSpanExporter(endpoint=settings.otel_endpoint)
        _otel_exporter.export([_as_otel(node)])
    except ImportError as exc:
        logger.warning("OTel export disabled: %s "
                       "(pip install -r requirements-dev.txt)", exc)
        _otel_broken = True
    except Exception as exc:
        logger.warning("OTel export disabled after a failure: %s", exc)
        _otel_broken = True


def _as_otel(node: Span):
    """A ReadableSpan-shaped view of one of our spans, built lazily."""
    from opentelemetry.sdk.trace import ReadableSpan
    from opentelemetry.trace import SpanContext, TraceFlags
    return ReadableSpan(
        name=f"{node.kind}:{node.name}",
        context=SpanContext(
            trace_id=int(uuid.uuid4().hex, 16),
            span_id=int(node.span_id, 16),
            is_remote=False,
            trace_flags=TraceFlags(TraceFlags.SAMPLED),
        ),
        start_time=int(node.t_start * 1e9),
        end_time=int((node.t_end or node.t_start) * 1e9),
        attributes={"kind": node.kind, "status": node.status,
                    **{k: str(v) for k, v in node.attrs.items()}},
    )
