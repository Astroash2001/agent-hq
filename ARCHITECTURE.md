# Architecture

How AgentHQ is put together. Start at [[README]] for what it is and how to run it;
[[STACK]] lists the technologies; [[DECISIONS]] explains why things are the way
they are; [[TODO]] tracks what is unfinished.

---

## Shape of the system

```
Browser                          FastAPI (port 8080)
┌──────────────────────┐         ┌────────────────────────────────┐
│ index.html           │         │ main.py                        │
│  ├ canvas.js  ───────┼─ /ws ───┼─► ConnectionManager            │
│  ├ hud.js            │  (JSON) │      ▲                         │
│  ├ office_map.js     │         │      │ subscribe_all           │
│  └ app.js  (router)  │◄────────┼── event_bus  ◄─────────┐       │
└──────────────────────┘         │                        │ emit  │
                                 │ orchestrator ──► base_agent ───┤
                                 │      │                 │       │
                                 │      │            tool_registry┘
                                 │      └─ agents/*.json          │
                                 └────────────────────────────────┘
```

Everything the UI shows arrives as an `Event` over one WebSocket. Nothing polls.

---

## Backend

### `main.py` — transport

- `ConnectionManager` holds the set of live sockets and broadcasts `Event` objects,
  pruning dead connections as it goes.
- Registers itself on the bus with `event_bus.subscribe_all(on_bus_event)`, so
  **every** event emitted anywhere reaches every browser without explicit wiring.
- REST: `GET /api/health`, `GET /api/agents`, `GET /api/history`,
  `POST /api/emergency-stop`.
- `WS /ws` — on connect it sends a `system.status` handshake carrying all agent
  manifests plus the token budget, then **replays the last 30 events** so a
  reconnecting browser catches up immediately. Then it reads client messages:
  `submit_task`, `emergency_stop`, `ping`.
- A submitted task is launched with `asyncio.create_task(...)` so the socket's
  receive loop never blocks on a long agent run.
- Mounts `frontend/` at `/` as static files — same origin, no CORS in practice.

### `event_bus.py` — the fabric

A singleton in-memory async pub/sub. Per-type subscribers plus global subscribers,
and a rolling 200-event history that backs both `/api/history` and the reconnect
replay. Callbacks are dispatched with `asyncio.create_task` when they are
coroutines, so a slow subscriber cannot stall an emit.

This is the seam that keeps the orchestrator, agents and tools ignorant of
WebSockets entirely.

### `orchestrator.py` — planning

Singleton. On construction it globs `backend/agents/*.json`, validates each into an
`AgentManifest`, and wraps it in a `BaseAgent`. The manifest with `id ==
"orchestrator"` becomes `boss_agent`; the rest go in `self.agents`.

`handle_user_input(prompt)` is the whole flow:

1. Create a `Task` and a `MissionWorkspace`, emit `task.created`
2. Boss goes `thinking` and walks to the whiteboard
3. `planner.plan(prompt, roster)` produces a validated DAG; emit `plan.created`
4. `_apply_memory_routing` may re-point a step at a different specialist, emitting
   `route.changed` when it does
5. Build `DAGNode`s from the plan and emit `dag.update`
6. `_execute_plan` runs the graph in waves — every step whose dependencies are
   satisfied starts together under `asyncio.gather`, capped by
   `max_parallel_agents`
7. Boss synthesises, emit `task.completed` with Markdown and the artifact index
8. Reset every agent to `idle` and put the floor back (`office.reset()`)

`_run_step` is what happens to one node: emit `task.assigned` (the flying task
orb), take the per-agent lock, deliver the artifacts this step `consumes` to the
desk, pull memory hints, `await specialist.run(...)` with both as context, file
what it `produces` into the workspace, and record the episode.

`_synthesize_results` is string assembly into Markdown, not an LLM call.

### `planner.py` — the plan

`Planner.plan` asks the model for `{"steps": [...]}`, then `validate_plan`
repairs what is repairable (renumbers ids, remaps aliases, drops steps naming an
unknown agent, drops forward references — which is also what makes the graph
provably acyclic) and rejects what is not. Two attempts, the second carrying the
failure back to the model. Anything that still fails, plus the no-key case, falls
through to `heuristic_plan` — the original four intent buckets, now declaring
real dependencies so the middle of each plan fans out.

### `workspace.py` — the shared blackboard

`MissionWorkspace` holds the artifacts one mission produces. A step declares
`produces` and `consumes`; `deliver` emits the carry event for each artifact and
`render_context` formats them into the block the recipient's prompt receives,
truncated at `MAX_INJECTED_CHARS`.

### `office.py` — the spatial runtime

Tools are bound to stations. `use_tool_station` is an async context manager that
takes the station's lock (emitting `station.queued` if someone else holds it),
walks the agent there and blocks for the travel time, yields, then releases in a
`finally` so a raising tool cannot deadlock the floor. Coordinates are tile
coordinates in the same 40×24 grid the renderer uses; `/api/office` serves them
so the frontend does not keep a second copy.

### `memory.py` — episodic memory

Every finished subtask is appended to a JSON store. `recall` returns the most
similar past episodes for an agent, `render_hints` turns them into a prompt
block, and `suggest_agent` proposes a different specialist when one has a
materially better record on similar work — gated on a minimum episode count and
a margin, so a cold store never overrides the planner.

Similarity is a cosine over embeddings, with the original IDF-weighted token
overlap kept as `_lexical_similarity` and used whenever embeddings are
unavailable. Episode vectors are backfilled in one batched call before a scan,
never one call per episode.

`competence` weighs three things rather than one: whether the step ran
(`outcome`), how good the critic said it was (`quality`, blended in at
`memory_quality_weight` and only over episodes something actually scored), and
how recent the evidence is (`memory_half_life_days` halves an episode's weight
as it ages). The result is still pulled toward 0.5 when the evidence is thin.

### `embeddings.py` — vectors without a dependency

`embed(texts)` with two providers. **hash** (the default) is a deterministic
hashed bag of words and character trigrams — SHA-256 buckets, signed, L2
normalised — so it needs no key, no network and no package, and produces the
same vector in every process on every machine. **litellm** calls a real
embedding model over the credentials already configured, and is the only one of
the two that understands synonymy. A provider that fails is logged once, marked
unavailable for the process, and returns `[]`, which is the signal for
`memory._similarity` to fall back to the lexical scorer.

Vectors are cached by SHA-256 of `provider|model|dim|text`, in memory always and
on disk only for providers that cost a call.

### `evals/` — the scorecard

Not part of the running app, and the app does not know it exists. `runner.py`
drives real missions through `orchestrator.handle_user_input` with nothing mocked
below that line, and `replay.py` swaps only the `llm.acompletion` symbol so a
scored run replays from committed cassettes rather than from a provider. A
cassette miss is a hard error, never a live call.

`judge.py` scores each rubric line independently against the **artifact files** —
never `report.md`, because `_synthesize_results` is string assembly around a
template and would score identically on a mission where every specialist produced
nothing. `report.py` renders `scorecard.md` with a delta against the previous run.

See `evals/README.md`.

### `runstore.py` — what happened, while it is still happening

A SQLite file (stdlib `sqlite3`, WAL, one short transaction per write) holding
`missions` and `steps`. Every status change is written here **before** the
matching event reaches the bus -- in that order, because the reverse leaves a
window where the UI has been shown a completed step the store has no record of,
and a resume would then run it twice.

It stores the plan as soon as planning finishes, not at the end. `mission.json`
carries the plan too but is written only when a mission completes, so a killed
run leaves none and there would be nothing to resume *from*.

Never raises at the call site: bookkeeping that can kill a run has turned a
durability feature into a new way to lose one.

### `api_missions.py` — mission history and resume

`GET /api/missions`, `GET /api/missions/{id}`, `GET /api/missions/{id}/trace` and
`POST /api/missions/{id}/resume`, as an `APIRouter` that `main.py` includes in
two lines. The trace endpoint returns raw NDJSON rather than parsed JSON: the
file is line-delimited precisely so a partially written one is still readable,
and parsing it server-side would discard that property at the moment it matters.

### `debate.py` — the meeting argues back

Attendees used to sit at the conference table while only the chair thought.
Each one now takes exactly one turn before the chair sums up: one bounded call,
no tools, no ReAct loop, asked for the single strongest objection from its own
specialism. The turns are independent, so they run under one `asyncio.gather`,
and each is its own trace span charged to that agent's own token usage — the
price of the argument lands in the same gauge and the same mission budget as
the work. Every position is emitted as `agent.argued` and rendered as speech.

Bounded by construction: `debate_rounds` (1) × `meeting_capacity - 1` seats ×
`debate_max_tokens` (350). Without a credential the turns are deterministic
stand-ins tagged `simulated`, so the offline demo still shows the room arguing.

### `contracts.py` — is this artifact usable

Pure functions, no model and no I/O. `infer_spec` derives a bar from the promised
filename and the step description (a `*_report.md` demands more than a
`*_notes.md`); `check` measures the artifact against it and returns the specific
failures as text an agent can act on. The first `must_not_match` pattern is the
literal stub a real run once filed as its research artifact. Deliberately
permissive: a spec that rejects reasonable work costs a repair attempt and then
completes anyway, having paid for nothing.

### `critic.py` — is this artifact any good

The judgement contracts cannot make. One `llm.acompletion` call at temperature 0
returning a strict JSON verdict, reusing `planner._extract_json` rather than a
second scraper. Runs on `settings.critic_model`, which defaults to the small
`fallback_model` — criticism that costs more than the work is not worth having.
Never raises: a failed or unparseable judgement is a *skipped* one, and a skip
never reads as a rejection. Skipped entirely with no credential, because the
built-in simulator grading itself is not a measurement.

The repair loop that consumes both lives in `orchestrator._run_step_body`:
contract, then critic, then at most `settings.max_repair_attempts` extra
dispatches of the same step to the same agent with the complaint appended. The
cap is the feature. Each attempt is its own span under the step, so the cost of
quality shows up in the trace and in `mission.json`'s `per_step`.

### `pricing.py` — what a call cost

A table of per-1M-token input/output prices keyed by litellm model string, with
`price()` and `is_priced()`. Deliberately incomplete: a model with no row returns
`0.0` and is reported through `is_priced()` as unpriced, because a guessed rate is
silently wrong forever while an unpriced model is visible in `mission.json` and
fixable with one line. Prefix matching is boundary-checked, so `gpt-4o-2024-08-06`
resolves to `gpt-4o` and `gpt-5.5` does not resolve to `gpt-5`.

### `tracing.py` — the run, recorded

Nested spans (`mission` → `step` → `turn` → `tool`/`llm`) written to
`data/missions/<id>/trace.jsonl`. Parentage lives in a `ContextVar`, so asyncio's
per-task context copy makes a step span the parent of everything that step does,
and two steps in the same wave cannot become each other's children — no signature
anywhere has to carry a span. Appended as spans close so a killed process leaves a
parseable file, then rewritten in start order at the end. An exception inside a
span is recorded as `status="error"` and re-raised, never swallowed.

### `base_agent.py` — the ReAct loop

`run(task)` loops up to `settings.max_agent_steps` (8):

```
update_status(THINKING)
  → _call_llm(messages, tool_schemas)
  → accumulate + emit token.update
  → speak(thought)
  → no tool calls?  → final answer, break
  → else execute each tool, append assistant+tool messages, loop
```

Tool results are appended in OpenAI function-calling message shape, so the same
history works whether the LLM path or the simulator produced them.

`_call_llm` tries `litellm.acompletion` when any API key is present, with a 12s
timeout. **On any exception it falls through to `_simulate_specialist_action`** —
a deterministic simulator that picks a plausible tool call based on the agent's id
and the task text, then returns a formatted result on the second pass. This is why
the demo never dies.

Cancellation is a `_is_cancelled` flag checked at the top of each step;
`orchestrator.cancel_all()` sets it on every agent.

### `tool_registry.py` — tools

Singleton registry. Each tool has a name, description, JSON-Schema parameters and a
callable. `get_schemas_for_agent(names)` returns OpenAI-shaped function schemas for
just the tools an agent's manifest grants.

`execute()` emits `tool.called` before and `tool.result` after (with `duration_ms`),
which is what populates the HUD terminal. Registered tools:

| Tool | Module |
|---|---|
| `terminal` | `tools/terminal.py` |
| `web_search` | `tools/web_search.py` |
| `read_file`, `write_file`, `list_files` | `tools/file_ops.py` |
| `code_analysis` | `tools/code_analysis.py` |

### `schemas.py` — the contract

`AgentStatus` (idle/thinking/active/tool_calling/done/error) and `EventType` (13
values) are the vocabulary shared with the frontend. `AgentManifest` carries both
the LLM-facing fields (`system_prompt`, `tools`) and the *presentation* fields the
office needs (`name`, `plate_verb`, `plate_icon`, `color`) — one file defines an
agent's brain and its on-screen identity together.

---

## Frontend

No build step. Four plain scripts loaded in order by `index.html`:
`office_map.js` → `canvas.js` → `hud.js` → `app.js`.

### `app.js` — event router

Opens the WebSocket, reconnects with backoff, and switches on `event.type` to drive
`canvas` and `hud`. If the socket is not open on submit, it runs
`simulateStandaloneTask()` — a scripted sequence that animates the whole office
without any backend. That is what makes frontend-only development possible.

### `office_map.js` — the office as data

The entire floor plan is one exported object, `OFFICE_MAP`:

| Key | Meaning |
|---|---|
| `cols`, `rows`, `tile`, `scale` | 40 × 24 tiles of 16px → 640 × 384px, aspect 1.667 |
| `rooms` | Named regions (bullpen, boss, lounge) |
| `blocks` | The shell outline — now a single rounded rect |
| `walls`, `doorways` | Wall runs punched by doorways → collision grid |
| `lights`, `fixtures` | Baked lighting sources and ceiling downlights |
| `doors` | Automatic sliding doors (`x`, `y`, `w`, `trigger`) |
| `props` | 213 placements: `sprite`, `x`, `y`, optional `dy`, `z`, `layer` |
| `seats` | Where each agent sits, and which way they face |
| `hangouts` | Destinations agents wander to when idle |
| `agentNames/Verbs/Icons` | Plate text before the backend answers |

**Placement rules** (documented at the top of the file, and enforced by
`check_layout.py`):

- Sprites anchor **bottom-centre**; an agent's baseline is `y*16`, a prop's is
  `(y+1)*16`
- `dy` nudges a sprite off its baseline (putting items on a desk)
- `z` biases sort order without moving anything (desktop items over their desk)
- `layer: 'floor'` draws with the floor instead of being depth-sorted (rugs)

Composition helpers avoid hand-listing repetitive geometry:

- `workstation(tx, ty, opts)` — a four-tile desk run: two L-desks plus monitors,
  keyboard, mouse, plant, mug, clutter, chair
- `deskRank(ty, variant)` — three workstations across, rotating four dressing kits
  so no two are identical
- `wallRun(x0, x1, y, sprite)` / `wallCol(x, y0, y1, sprite)` — partition runs

### `canvas.js` — the renderer

`PixelOfficeCanvas` owns a rAF loop: `update(dt)` then `draw()`.

**Draw order matters and is deliberate:**

```
clear to VOID (black)
translate + scale by zoom
  drawShellRim      ← four-band bevel frame, drawn BEFORE the world
  clip(shell)
    drawFloor       ← plank variants + floor-layer props (rugs)
    drawWalls       ← wall course, thin partitions, lit edges, cast shadows
    drawDepthSorted ← props + agents + doors in ONE list, sorted by baseline
    drawOrbs        ← task orbs and particles
    applyLighting   ← multiply light map, then add glow map
  restore clip
restore transform
drawOverlay         ← plates and bubbles, in SCREEN space
```

Points worth knowing:

- **Depth sorting** puts props, agents and doors in one array sorted by baseline, so
  an agent can walk behind a desk and in front of a door.
- **Lighting** is baked once into two offscreen canvases: a *light map* (ambient
  colour plus additive radial pools) composited with `multiply`, and a *glow map*
  (fixture cones and lamp halos) composited with `lighter` afterwards, so emitted
  light sits on top of the shadow rather than being darkened by it. Smoothing is
  re-enabled for these two draws only — nearest-neighbour upscaling of a gradient
  bands badly.
- **Labels render in screen space**, after the world transform is popped, so type
  is crisp instead of a 7px font multiplied by the zoom.
- **Automatic doors** ease `open` 0→1 while any agent is within `trigger` tiles,
  opening faster than they close. The leaf frames are generated *from the wall
  sprite* at atlas build time, so a door can never mismatch its wall.
- **Cache busting**: the atlas PNG and its JSON manifest load with one shared
  `?v=` token, because the browser caching them independently pairs a fresh PNG
  with a stale manifest and renders scrambled furniture.
- **Pathing** is A* over the collision grid; idle agents wander to `hangouts`.

### `hud.js` — the sidebar

`RetroHUD` builds the agent dock from manifests, appends colour-coded terminal
lines, drives the 20-segment token gauge, and reveals the mission output
(Markdown via `marked`). It reads agent portraits out of the same atlas the
canvas uses. Four panels beyond the dock:

* **Execution plan** — `renderPlan` / `layoutDag` / `drawDag` turn a
  `plan.created` or `dag.update` payload into SVG: nodes ranked by *longest* path
  from a root (a shortest-path rank would point an edge upwards and read as a
  cycle), the owning agent's manifest colour as the fill, status as the ring and
  glyph, and what the step cost written beneath it. Per-step badges — a reroute
  and a review verdict — arrive on their own events and are kept in
  `hud.stepBadges` rather than painted onto the SVG, because every status change
  redraws the whole graph.
* **Mission cost** — `updateCost` folds each `dag.update` into a per-mission
  meter: cost, tokens, wall clock, a latency bar per step and tokens by agent.
  It reads the graph rather than keeping a second ledger that could disagree
  with it, and marks any figure the simulator produced.
* **Trace replay** — the controls for `replay.js`.
* **Mission output** — the goal, the briefing and the artifacts, with the card
  *moved* into the full-screen overlay rather than copied, so the open file, the
  selected tab and the scroll position survive the trip.

### `replay.js` — the time-travel debugger

`TraceReplay` loads a past mission's plan (`GET /api/missions/{id}`) and spans
(`GET /api/missions/{id}/trace`), compiles the spans back into the events the
WebSocket would have sent, and feeds them to the same `handleEvent` in `app.js`
that the live socket feeds — so the office replays through the live renderer
rather than a second one that would drift. Play, pause, scrub and 1×–8×.

A step span becomes the assignment that opened it and the completion that closed
it; a tool span becomes a walk (the tool's station is looked up in
`/api/office`, since the trace holds no coordinates) and the two tool events
around it; a turn becomes the tokens it burned; a debate span becomes the
objection it recorded. Nothing the trace does not hold is synthesised — a
replayed mission has no briefing to show, and says so.

Seeking backwards replays from zero, because the office is a state machine and
there is no inverse for "the agent walked to the library". Catch-up events are
flagged `silent`, which is what stops a scrub animating twenty seconds of walking.

---

## Asset pipeline

Source art lives in `furniture/`; nothing there is loaded at runtime. Everything is
baked into **one atlas** plus a JSON manifest that the browser loads.

```
furniture/final/moderninteriors/   (paid pack)
furniture/final/Modern_Office_.../ (paid pack)
        │
        ├─ tools/index_pack.py   → tools/_index/<sheet>.json + numbered contact sheet
        │                          (segments a packed sheet, or reads a Singles folder)
        ├─ tools/pack_source.py  → pick(sheet, i) resolves an index back to a crop
        │                          trim() strips the uniform padding the office
        │                          pack wraps every single in
        ├─ tools/gen_tiles.py    → synthesised floor + props no pack contains
        │                          (planter runs, boardroom table, conference rug)
        └─ tools/build_atlas.py  → frontend/assets/generated/office_atlas.{png,json}
                                   + floor_wood.png

        tools/check_layout.py    → geometric validator, exits non-zero on problems
        tools/preview_map.py     → headless PNG render (node reads the map, PIL draws)
```

**Rebuild order:** `gen_tiles.py` → `build_atlas.py` → `check_layout.py`.

`build_atlas.py` also derives sprites at build time rather than storing variants:
`of_glass_thin` (a slimmer glazing run cropped from the full panel),
`of_chair_side_l` (a mirror of the right-facing chair), and the eight door leaf
frames. Picks are `(sheet, index)` pairs into the index files, never raw pixel
rectangles — see [[DECISIONS]].

`check_layout.py` computes every prop's real pixel rectangle from the atlas and
flags: props past a wall or off the map, props standing on wall tiles, and
overlaps above 35%. It knows about deliberate exceptions — wall-mounted items,
partition runs, and chairs drawn up to a table.

---

## Adding things

**A new agent:** drop a JSON manifest in `backend/agents/` (validated against
`AgentManifest`), add a `seats` entry in `office_map.js`, and add its sprite key to
`agentSprites`. The orchestrator discovers it on next construction; the frontend
gets it in the handshake.

**A new tool:** implement in `backend/tools/`, `self.register(...)` it in
`ToolRegistry._register_default_tools`, and list its name in the manifests that
should have it.

**A new prop:** find it with `index_pack.py`, add a `(sheet, index)` pick to
`build_atlas.py`, rebuild, place it in `office_map.js`, run `check_layout.py`.
