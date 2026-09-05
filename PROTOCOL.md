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
| Meetings | `agent.argued` |

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

## Artifact quality

### `step.critiqued`

An artifact was checked before anything downstream was allowed to read it.
Fires once per attempt, from either checker.

```json
{ "step_id": "s2", "agent_name": "Alice", "verdict": "revise", "score": 0.4,
  "reasons": ["No figures are given for either chemistry"], "attempt": 1,
  "source": "critic", "repairing": true, "artifact": "research_notes.md" }
```

| Field | Means |
|---|---|
| `source` | `"contract"` (regex layer, free and certain) or `"critic"` (a model call, a judgement) |
| `verdict` | `"accept"` or `"revise"` |
| `score` | 0..1 from the critic; **null** from a contract check, which does not score |
| `attempt` | 1 on the original dispatch, higher on a repair |
| `repairing` | whether a re-dispatch follows |

**`repairing: false` on a `revise` is the case to render loudest.** It means the
repair budget was spent and the step completes carrying work its reviewer
rejected. Drawn the same as an accept, a capped repair looks like a pass.

A step with no `step.critiqued` event was not judged — the critic is skipped
when no credential is configured, because the built-in simulator grading itself
would look like a measurement while being nothing of the kind.

---

## Meetings

### `agent.argued`

One attendee's turn at the conference table, before the chair sums up. Fires
once per attendee per debate round — so a three-person meeting with the default
`debate_rounds: 1` sends three, in whatever order the concurrent turns finish.

```json
{ "agent_name": "Charlie", "role": "Data & Metrics Analyst",
  "position": "Table 2 gives a 40% figure with no source; I would cut it or cite it.",
  "round": 1, "step_id": "s3", "simulated": false,
  "model": "gemini/gemini-2.5-flash-lite", "total_tokens": 240, "cost_usd": 0.0002 }
```

| Field | Means |
|---|---|
| `position` | the objection, three sentences at most |
| `round` | 1-based; only above 1 when `debate_rounds` is raised |
| `simulated` | **true** when no credential is configured and the turn is the deterministic offline stand-in, not a model's opinion |
| `total_tokens` / `cost_usd` | what this turn cost, already added to that agent's `token.update` |

Render it as speech: the argument going round the table is the most legible
multi-agent moment in the product, and it is the one thing a viewer can watch
that a single-agent system could not produce. `app.js` reuses the existing
speech bubble rather than introducing a second way of showing an agent talking.

A meeting with no `agent.argued` events had `debate_enabled` off, or no
attendees.

---

## Existing events that gained fields

All additive — old readers keep working.

| Event | New in `data` |
|---|---|
| `agent.status` | `station_id`, `station_label` on `tool_calling` |
| `task.assigned` | `step_id`, `produces`, `consumes`, `rationale`, `routed_from`, `attempt` (1 on the original dispatch, higher on a repair) |
| `dag.update` | `source` (`"llm"` / `"heuristic"`); node ids are now plan step ids (`s1`, `s2`) instead of `dag_0`. Each node that has **run** also carries `cost_usd`, `duration_sec`, `total_tokens`, `model`, `attempts`, `estimated`; the event carries mission totals `cost_usd`, `mission_tokens`, `tokens_estimated` |
| `task.progress` | `wave` (which parallel wave just finished), `failed` |
| `task.completed` | `plan_source`, `artifacts` (index, no bodies), `cost_usd`, `tokens_estimated` |
| `token.update` | `estimated` (the counts are the simulator's, not a provider's), `model` (which model answered) |

**Reading `dag.update`:** a node with no `cost_usd` has not run yet. That is
deliberately different from `cost_usd: 0.0`, which means it ran and cost nothing
measurable -- the simulator path, or a model with no row in the pricing table. A
panel that draws the first as $0.0000 is telling the viewer a step was free when
it has not started. The mission totals are **this mission's**: the agents' own
counters are lifetime totals that climb across every mission the process has
served.

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
| `GET /api/missions?limit=50` | every mission the run store knows, newest first |
| `GET /api/missions/{id}` | one mission: plan, steps, status, artifacts, `has_trace` |
| `GET /api/missions/{id}/trace` | the mission's spans as raw NDJSON (`text/plain`) |
| `POST /api/missions/{id}/resume` | carry on a mission a killed process left unfinished |

`GET /api/missions/{id}` is served from the run store, not from `mission.json` --
the manifest is written only when a mission finishes, and an unfinished mission is
the one you actually want to look at. Resume returns the same payload a normal run
does, plus `resumed: true` and `skipped_steps`. It answers **409** for a mission
that already completed, and for one that died before planning finished and so has
no graph to carry on.

### On disk, per mission

`backend/data/missions/<task_id>/` holds the artifacts plus:

| File | What it is |
|---|---|
| `report.md` | the synthesised briefing |
| `mission.json` | goal, plan, per-step status, and the cost breakdown below |
| `trace.jsonl` | one span per line: mission, step, turn, tool, llm |

`mission.json` gained `cost_usd`, `mission_tokens`, `unpriced_models`,
`tokens_estimated`, and `per_step`, one row per plan step carrying `model`,
`prompt_tokens`, `completion_tokens`, `cost_usd`, `duration_sec`, `tool_calls`,
`retries`, `fell_back`, `estimated` and `status`.

Both token figures are this mission's. `total_tokens` is what the budget gate
measured -- every token the roster burned between the mission starting and
finishing, including turns that belong to no single step. `mission_tokens` sums
the `per_step` rows, so it is the part the breakdown can account for, and is the
smaller of the two whenever they disagree. Neither is a process-lifetime figure:
the gate takes a baseline of the roster's counters at mission start and reads the
difference, so a server that has already run twenty missions offers the
twenty-first the same allowance as the first.

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
| `contracts_enabled` | artifacts are filed unchecked, as before |
| `critic_enabled` | no model judges any artifact; contracts still run |
| `debate_enabled` | attendees sit silent through a meeting and only the chair thinks, as before |
| `mcp_enabled` | (default **off**) no MCP servers are connected and no `mcp__*` tools exist |

`max_repair_attempts` (1) is the extra dispatches a rejected step may have.
Setting it to 0 leaves the checks running and the verdicts emitting, and
simply never re-dispatches.

Pacing knobs worth having: `walk_seconds_per_tile` (0.06),
`min_walk_seconds` / `max_walk_seconds` (0.25 / 2.0), `max_parallel_agents` (3).


## Cassettes, and what a replay actually replays

`evals/replay.py` swaps two symbols for the length of one task: `llm.acompletion`
and `tool_registry.execute`. Both are restored on the way out, including when the
task raises.

Model exchanges are keyed on the model, the whole message history, the tool
schemas offered and the temperature, with two things normalised out because they
change for reasons that have nothing to do with the code: today's date, which
`config.date_context()` puts in every system prompt, and the `, took 41.2s` wall
clock that `memory.render_hints` writes into recalled episodes.

Tool calls are keyed on the tool name and its arguments, and **only tools whose
answer comes from outside the office are served from disk** — `web_search` and
any `mcp__*` server. Everything else really runs, because a local tool's side
effect *is* the mission's output: serving `write_file` a recorded "ok" leaves the
workspace empty and the run scores as having produced nothing.

Recording only the model calls is not enough, and the failure is quiet. An
agent's first turn searches; the results land verbatim in the second turn's
prompt; when the web moves, that prompt changes and the second turn misses its
own cassette — a suite that replayed cleanly an hour ago going red with nothing
in the repo having changed.

A cassette also records the model that answered it, and a replay resolving to a
different model is refused rather than served. The key does not include the
resolved default — `base_agent` calls `acompletion` with no model at all, so
every agent turn hashes the empty string — which meant a suite recorded on one
model replayed clean under another and the scorecard printed the wrong name in
its model column.
