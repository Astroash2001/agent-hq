from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class AgentStatus(str, Enum):
    IDLE = "idle"
    THINKING = "thinking"
    ACTIVE = "active"
    TOOL_CALLING = "tool_calling"
    DONE = "done"
    ERROR = "error"


class EventType(str, Enum):
    SYSTEM_STATUS = "system.status"
    TASK_CREATED = "task.created"
    TASK_ASSIGNED = "task.assigned"
    TASK_PROGRESS = "task.progress"
    TASK_COMPLETED = "task.completed"
    TASK_FAILED = "task.failed"
    AGENT_STATUS = "agent.status"
    AGENT_SPEAKING = "agent.speaking"
    TOOL_CALLED = "tool.called"
    TOOL_RESULT = "tool.result"
    TOKEN_UPDATE = "token.update"
    DAG_UPDATE = "dag.update"
    TERMINAL_LOG = "terminal.log"
    # Planning (P1)
    PLAN_CREATED = "plan.created"
    ROUTE_CHANGED = "route.changed"
    # Shared workspace (P2)
    ARTIFACT_CREATED = "artifact.created"
    ARTIFACT_DELIVERED = "artifact.delivered"
    # Spatial runtime (P3)
    AGENT_MOVE = "agent.move"
    AGENT_ARRIVED = "agent.arrived"
    STATION_QUEUED = "station.queued"
    STATION_ACQUIRED = "station.acquired"
    STATION_RELEASED = "station.released"
    # Memory (P6)
    MEMORY_RECALLED = "memory.recalled"
    # Artifact quality (WP2)
    STEP_CRITIQUED = "step.critiqued"
    # Meetings that argue (WP6)
    AGENT_ARGUED = "agent.argued"
    # An agent hired mid-mission (WP8)
    AGENT_SPAWNED = "agent.spawned"


class DeskPosition(BaseModel):
    x: int = Field(..., description="X coordinate on the 2D grid")
    y: int = Field(..., description="Y coordinate on the 2D grid")


class AgentManifest(BaseModel):
    """Configuration that defines an agent's identity, role, and capabilities."""
    id: str = Field(..., description="Unique slug identifier (e.g. 'coder', 'researcher')")
    name: str = Field(..., description="Display name shown on the in-world plate (e.g. 'Alice')")
    role: str = Field(..., description="Domain description (e.g. 'Bug Hunter & Code Engineer')")
    plate_verb: str = Field("Idle", description="Second line of the in-world name plate (e.g. 'Querying DB')")
    plate_icon: str = Field("", description="Emoji shown after the plate verb (e.g. '⚡')")
    avatar_emoji: str = Field("🤖", description="Emoji or sprite icon identifier")
    color: str = Field("#38bdf8", description="Neon theme accent color for UI lines/glows")
    system_prompt: str = Field(..., description="Specialized persona and system instruction")
    tools: List[str] = Field(default_factory=list, description="List of registered tool names")
    desk_position: DeskPosition = Field(default_factory=lambda: DeskPosition(x=100, y=100))

    # --- hired mid-mission ---------------------------------------------------
    # The five permanent staff are JSON files on disk and leave all of this
    # empty. A contractor the foundry hires for one mission carries its own
    # provenance, because a roster where you cannot tell who was hired and why
    # is a roster nobody can audit.
    spawned: bool = Field(False, description="Hired mid-mission rather than loaded from disk")
    spawned_for: str = Field("", description="The step id whose capability gap caused the hire")
    spawned_reason: str = Field("", description="Which tools nobody held")
    mission_id: str = Field("", description="The mission this agent belongs to, if spawned")
    # The permanent five are mapped to their sprite by office_map.js. A spawned
    # agent has no art of its own -- there are five characters in the atlas and
    # the source packs are gigabytes -- so it borrows one and is hue-shifted by
    # `sprite_hue` with a visitor badge drawn over it.
    sprite: str = Field("", description="Atlas character to draw, if not the static mapping")
    sprite_hue: int = Field(0, description="Degrees of hue rotation applied to the borrowed sprite")


class Event(BaseModel):
    """Strongly typed atomic event emitted by the backend to WebSockets & EventBus."""
    type: EventType
    agent_id: Optional[str] = None
    task_id: Optional[str] = None
    data: Dict[str, Any] = Field(default_factory=dict)
    timestamp: str = Field(default_factory=lambda: datetime.utcnow().isoformat())


class Task(BaseModel):
    """Unit of work managed by the Orchestrator."""
    id: str
    description: str
    parent_task_id: Optional[str] = None
    assigned_to: Optional[str] = None
    status: str = "pending"  # pending, in_progress, completed, failed
    result: Optional[str] = None
    error: Optional[str] = None
    created_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())


class DAGNode(BaseModel):
    """Node in the task dependency graph for UI visualization."""
    id: str
    label: str
    agent_id: Optional[str] = None
    status: str = "pending"  # pending, running, completed, failed
    dependencies: List[str] = Field(default_factory=list)


class TokenUsage(BaseModel):
    """Telemetry tracking token usage and cost."""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    estimated_cost_usd: float = 0.0


# ---------------------------------------------------------------------------
# Planning (P1)
# ---------------------------------------------------------------------------

class PlanStep(BaseModel):
    """One node of an execution DAG, as authored by the planner."""
    id: str = Field(..., description="Step id, s1/s2/s3... in plan order")
    agent_id: str = Field(..., description="Specialist that runs this step")
    description: str = Field(..., description="Instruction handed to that specialist")
    dependencies: List[str] = Field(default_factory=list, description="Step ids that must finish first")
    produces: str = Field("", description="Artifact filename this step writes to the workspace")
    consumes: List[str] = Field(default_factory=list, description="Artifact filenames handed to this step")
    rationale: str = Field("", description="Why this specialist was chosen")
    status: str = "pending"  # pending, running, completed, failed
    routed_from: Optional[str] = Field(None, description="Original agent id if memory rerouted this step")
    needs_tools: List[str] = Field(
        default_factory=list,
        description="Tools this step cannot be done without, as named by the planner"
    )
    regranted_from: Optional[str] = Field(
        None,
        description="Original agent id if validation moved this step to one holding needs_tools"
    )

    # A step used to mean one agent doing one thing, which is why a meeting
    # could not be expressed at all: there was nowhere to name the other people
    # in the room. `agent_id` stays the single owner -- for a meeting it is
    # whoever chairs and records the decision -- and `attendees` are the others
    # who walk to the table and are held there for the duration.
    kind: str = Field("work", description="work | meeting")
    attendees: List[str] = Field(
        default_factory=list,
        description="Additional agent ids that join a meeting step, besides agent_id"
    )


class Plan(BaseModel):
    """A validated execution graph for one user goal."""
    goal: str
    steps: List[PlanStep] = Field(default_factory=list)
    source: str = Field("heuristic", description="'llm' or 'heuristic'")


# ---------------------------------------------------------------------------
# Shared workspace (P2)
# ---------------------------------------------------------------------------

class Artifact(BaseModel):
    """A named piece of work one step produced and later steps can read."""
    name: str
    content: str
    author_id: str
    author_name: str
    step_id: str = ""
    size_chars: int = 0
    created_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())


# ---------------------------------------------------------------------------
# Spatial runtime (P3)
# ---------------------------------------------------------------------------

class Station(BaseModel):
    """A place on the floor an agent must occupy to use the tools bound to it."""
    id: str
    label: str
    x: float = Field(..., description="Tile X on the 40x24 office grid")
    y: float = Field(..., description="Tile Y on the 40x24 office grid")
    tools: List[str] = Field(default_factory=list, description="Tool names gated by this station")
    kind: str = Field("tool", description="tool | planning | meeting")
    description: str = ""
    capacity: int = Field(1, description="Agents that may hold it at once")


# ---------------------------------------------------------------------------
# Memory (P6)
# ---------------------------------------------------------------------------

class Episode(BaseModel):
    """One finished subtask, kept so the agent and the router can learn from it."""
    id: str
    agent_id: str
    agent_name: str = ""
    goal: str = ""
    subtask: str = ""
    outcome: str = "success"  # success | failure
    # How good the work was, 0..1, as judged by the critic -- distinct from
    # `outcome`, which only says whether the step ran. An agent that technically
    # succeeded badly used to score exactly like one that succeeded well.
    #
    # Optional, and None rather than a number, for two reasons: every episode
    # written before this field existed has no quality signal and must still
    # load, and a run with no critic (no key, or critic disabled) genuinely did
    # not measure this. A default of 1.0 would record "perfect" for both.
    quality: Optional[float] = None
    summary: str = ""
    duration_sec: float = 0.0
    total_tokens: int = 0
    tools_used: List[str] = Field(default_factory=list)
    artifact: str = ""
    tokens: List[str] = Field(default_factory=list, description="Cached lexical index for recall")
    # Semantic index for recall, filled in lazily by `memory._episode_vector`.
    # Deliberately not written to data/memory.json: the vector is reproducible
    # from `tokens` and already lives in the content-addressed embedding cache,
    # so persisting it here would store the same 256 floats a second time and
    # pin every episode to whichever provider happened to be configured when it
    # was recorded. Optional because an episode from an older store has none.
    embedding: Optional[List[float]] = None
    timestamp: str = Field(default_factory=lambda: datetime.utcnow().isoformat())
