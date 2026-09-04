# Protocol

The wire contract between the backend and the office renderer. This is the
boundary: the backend may **add** events and fields freely, but renaming or
removing one is a conversation, not a commit.

[[README]] · [[ARCHITECTURE]] · [[STACK]] · [[DECISIONS]] · [[TODO]]

Every message is an `Event` from `backend/schemas.py`, sent as JSON over `/ws`:

```json
{ "type": "agent.move", "agent_id": "coder", "task_id": "task_ab12", "data": { }, "timestamp": "..." }
```

`agent_id` and `task_id` may be `null`. Everything interesting is in `data`.

---

## What is new

Four subsystems landed together and each brought events with it. Nothing that
existed before was removed, so a frontend that ignores all of this still works
exactly as it did — it just stops seeing most of what is happening.

| Subsystem | Events |
|---|---|
| Planner | `plan.created`, `route.changed` |
| Workspace | `artifact.created`, `artifact.delivered` |
| Spatial runtime | `agent.move`, `agent.arrived`, `station.queued`, `station.acquired`, `station.released` |
| Memory | `memory.recalled` |

---

## Planning

### `plan.created`

Fires once per mission, immediately after the boss finishes at the whiteboard
and before any step is dispatched. This is the whole graph up front.

```json
{
  "goal": "Implement and test a fast Python fibonacci cache",
  "source": "llm",
  "parallel_enabled": true,
  "max_parallel": 3,
  "steps": [
    { "id": "s1", "agent_id": "researcher", "description": "...",
      "dependencies": [], "produces": "research_notes.md", "consumes": [],
      "rationale": "needs sources before anyone writes code",
      "status": "pending", "routed_from": null }
  ]
}
```

`source` is `"llm"` or `"heuristic"` — worth surfacing, because it tells the
viewer whether a model planned this mission or the fallback buckets did.

### `route.changed`

Memory overrode the planner's choice of specialist for one step. Fires before
execution, so the DAG can be drawn with the correction already applied.

```json
{ "step_id": "s2", "from_agent_id": "coder", "from_agent_name": "Charlie",
  "to_agent_id": "data_analyst", "to_agent_name": "Bob",
  "from_score": 0.31, "to_score": 0.78,
  "reason": "Bob has a stronger record on work like this" }
```

---

## Workspace

### `artifact.created`

A desk produced something. `preview` is the first 180 characters, enough for a
label or a tooltip without shipping the whole document.

```json
{ "name": "research_notes.md", "author_id": "researcher", "author_name": "Alice",
  "step_id": "s1", "size_chars": 812, "preview": "### Output from Alice ..." }
```

### `artifact.delivered`

The artifact is being carried to another desk. Both endpoints are in the payload
precisely so the renderer can fly an object from one to the other. One event per
artifact, so a fan-in step receives two.

```json
{ "name": "research_notes.md",
  "from_agent_id": "researcher", "from_agent_name": "Alice",
  "to_agent_id": "coder", "to_agent_name": "Charlie",
  "step_id": "s2", "size_chars": 812 }
```

Delivery happens *before* the recipient starts work, which is the cue to animate
the handoff and then let the recipient begin.

---

## Spatial runtime

Tools are bound to stations (`backend/office.py`). An agent must be standing at
a tool's station to call it, and a station holds one agent at a time. Positions
are **tile coordinates on the same 40×24 grid** `office_map.js` uses.

Fetch the station geometry from `GET /api/office` rather than hardcoding it —
where the stations are is backend state now, and moving one changes how long
missions take.

### `agent.move`

Sent at the moment the walk begins. `duration_ms` is exactly how long the backend
will block, so tween the sprite over that interval and it will arrive in sync.

```json
{ "agent_name": "Charlie",
  "from": { "x": 13.0, "y": 7.9 }, "to": { "x": 19.4, "y": 18.4 },
  "station_id": "ops_console", "label": "Ops Console", "duration_ms": 750 }
```

A walk back to the agent's own desk carries `"station_id": ""` and
`"label": "desk"`.

### `agent.arrived`

```json
{ "agent_name": "Charlie", "at": { "x": 19.4, "y": 18.4 },
  "station_id": "ops_console", "label": "Ops Console" }
```

### `station.queued`

The agent wants a station someone else is holding, and is now waiting. This is
the traffic jam — the one event that makes the floor plan visibly matter.

```json
{ "agent_name": "Bob", "station_id": "ops_console", "label": "Ops Console",
  "tool": "terminal", "occupied_by": "coder", "queue_length": 1 }
```

### `station.acquired` / `station.released`

Bracket the actual tool call. `acquired` fires after the agent has arrived;
`released` fires even if the tool raised.

```json
{ "agent_name": "Charlie", "station_id": "ops_console",
  "label": "Ops Console", "tool": "terminal" }
```

---

## Memory

### `memory.recalled`

The agent started this step holding relevant past episodes.

```json
{ "step_id": "s2", "agent_name": "Charlie", "episodes": 3,
  "preview": "Relevant things you have done before in this office: ..." }
```

---

## Existing events that gained fields

All additive — old readers keep working.

| Event | New in `data` |
|---|---|
| `agent.status` | `station_id`, `station_label` on `tool_calling` |
| `task.assigned` | `step_id`, `produces`, `consumes`, `rationale`, `routed_from` |
| `dag.update` | `source` (`"llm"` / `"heuristic"`); node ids are now plan step ids (`s1`, `s2`) instead of `dag_0` |
| `task.progress` | `wave` (which parallel wave just finished), `failed` |
| `task.completed` | `plan_source`, `artifacts` (index, no bodies), `cost_usd`, `tokens_estimated` |
| `token.update` | `estimated` (the counts are the simulator's, not a provider's), `model` (which model answered) |

**Reading `token.update`:** `estimated: true` means no model was called and the
counts are the built-in simulator's invention. A gauge that renders those the same
way it renders a real reading is presenting a guess as a measurement -- mark them.
`cost_usd` on `task.completed` is this mission's spend, summed per step; it is
`0.0` on the simulator path because nothing was called, not because it was free.

**Note on parallelism:** several agents are now active at once, so
`agent.status`, `agent.speaking` and `tool.called` interleave across agents.
Anything that assumed one active agent at a time needs a per-agent keyed state.

---

## REST

| Endpoint | Returns |
|---|---|
| `GET /api/agents` | manifests, each now with a `stats` block (`runs`, `success_rate`, `avg_duration_sec`, `avg_tokens`) |
| `GET /api/office` | stations, seats, live positions, occupancy, queue lengths |
| `GET /api/plan` | the graph for the mission in flight, or `{"plan": null}` |
| `GET /api/memory?limit=25` | recent episodes plus per-agent lifetime stats |
| `POST /api/memory/reset` | wipes episodic memory |

### On disk, per mission

`backend/data/missions/<task_id>/` holds the artifacts plus:

| File | What it is |
|---|---|
| `report.md` | the synthesised briefing |
| `mission.json` | goal, plan, per-step status, and the cost breakdown below |
| `trace.jsonl` | one span per line: mission, step, turn, tool, llm |

`mission.json` gained `cost_usd`, `mission_tokens` (this run alone -- `total_tokens`
is the roster's lifetime figure that the budget gate reads), `unpriced_models`,
`tokens_estimated`, and `per_step`, one row per plan step carrying `model`,
`prompt_tokens`, `completion_tokens`, `cost_usd`, `duration_sec`, `tool_calls`,
`retries`, `fell_back`, `estimated` and `status`.

A span in `trace.jsonl` is `{span_id, parent_id, kind, name, seq, t_start, t_end,
duration_sec, status, error, attrs}`. `parent_id` is null on the one mission span
that roots the tree. The file is written append-as-you-close, so a killed process
still leaves every finished line readable, and rewritten in `(t_start, seq)` order
when the mission ends -- `seq` because the clock is ~15ms granular on Windows and
a parallel wave routinely ties.

---

## Feature flags

All in `backend/config.py`, all default on. Turning one off returns the backend
to its previous behaviour, which is the fastest way to isolate whether a visual
bug is yours or mine.

| Flag | Off means |
|---|---|
| `planner_enabled` | heuristic buckets only, no planning call |
| `parallel_enabled` | one step at a time, as before |
| `spatial_enabled` | no walking, no stations; tools callable from the desk |
| `memory_enabled` | no recall, no rerouting, nothing recorded |
| `tracing_enabled` | no `trace.jsonl`; spans still yield, they are simply not recorded |
| `otel_enabled` | (default **off**) no OTLP export; needs a collector and the dev-only opentelemetry packages |

Pacing knobs worth having: `walk_seconds_per_tile` (0.06),
`min_walk_seconds` / `max_walk_seconds` (0.25 / 2.0), `max_parallel_agents` (3).
