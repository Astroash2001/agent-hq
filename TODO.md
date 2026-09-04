# TODO

Unfinished work, known issues and next steps. Everything here was verified against
the codebase, not assumed.

[[README]] · [[ARCHITECTURE]] · [[STACK]] · [[DECISIONS]]

---

## Immediate

### No `requirements.txt`

**Done.** Split into `requirements.txt` (fastapi, uvicorn, websockets, pydantic,
pydantic-settings, litellm) and `requirements-dev.txt` (the test suite plus
pillow/numpy/scipy for the asset pipeline). Both pinned to the versions the
32-test suite passes on under Python 3.10.9, and both verified to resolve.

`websockets` was missing from the old working set — uvicorn ships no WebSocket
implementation of its own, so `/ws` fails the handshake without it. `playwright`
was listed but is imported nowhere; it is not in either file.

Still outside pip: `node` on PATH for `preview_map.py` and `check_layout.py`, and
the art source packs if you want to rebuild the atlas.

### Dead sprites in the atlas

Measured: the atlas holds **233** non-character sprites; the map uses **96**.

- **15 authored sprites unused:** `chair_office`, `conf_table`, `conf_wall_run`,
  `conf_wall_strip`, `counter_sink_v`, `credenza_lamp`, `desk_l`, `desk_l_wide`,
  `desk_l_wing`, `desk_pedestal`, `plant_potted`, `printer_small`,
  `projector_screen`, `tv_boardroom`, `tv_widescreen`
- **8 orphaned PNGs** sit in `assets/generated/authored/` but are no longer
  produced by `gen_tiles.py` — `conf_wall_run`, `conf_wall_strip`, `credenza_lamp`,
  `plant_potted`, `tablet`, `tv_boardroom`, `tv_wall_east`, `whiteboard_arch`.
  `build_atlas.py` globs that directory, so they are baked in as dead weight.

Fix: delete the orphans, prune the unused `AUTHORED` entries and `PACK_PICKS`, then
rebuild. Should meaningfully shrink the atlas.

### `emergency_stop` doesn't reach a running LLM call

`cancel()` sets `_is_cancelled`, checked at the top of each ReAct step. A request
already in flight to `litellm.acompletion` runs to its 12s timeout first. Real
cancellation needs the LLM call wrapped in a task that can be cancelled.

---

## Backend

### Task routing is keyword matching

**Done.** `planner.py` asks the model for a validated DAG and falls back to the
old buckets when there is no key or the reply will not validate. The buckets
survive as `heuristic_plan`, now with real dependencies instead of a fixed chain.
Contract in [[PROTOCOL]] (`plan.created`).

Still open: the planner sees the roster and each agent's record, but not the
tools each agent actually holds, so it can still assign work to a specialist that
lacks the tool for it. `validate_plan` does not check that.

### Everything runs sequentially

**Done.** `_execute_plan` runs every step whose dependencies are satisfied in one
`asyncio.gather` wave, capped by `max_parallel_agents` (3). One agent still runs
one subtask at a time, held by a per-agent lock.

**Also done.** The cap is now `asyncio.Semaphore(max_parallel_agents)` held
around each `_run_step`, and the whole ready set is dispatched rather than a
slice of it, so the fourth step starts the instant a slot frees. The scheduler
re-evaluates on every completion (`asyncio.wait(..., FIRST_COMPLETED)`) instead
of draining a whole wave, so a step also starts the moment its dependencies land
rather than at the next wave boundary. `orchestrator._peak_active_steps` records
the high-water mark, which is the only way to assert the cap actually caps -- a
counter read at rest reads zero however broken the scheduler is.

Steps also gained a deadline (`step_timeout_sec`, 300s) and a retry budget
(`step_max_attempts`, 2) for transient failures only, classified by
`llm.is_transient` so there is one classifier rather than two that drift.

### A killed process loses the mission

**Done.** `runstore.py` writes every mission and step transition to a SQLite file
(stdlib, no service) *before* the matching event is emitted, so there is no
window in which the UI has been shown a step the store never heard of.
`POST /api/missions/{id}/resume` picks a killed mission back up: completed steps
are skipped, their artifacts are read back off disk into the workspace, and
everything else -- including anything left `running` when the process died --
runs again.

Verified by killing a real process mid-mission: the store held
`s1=completed, s2=running, s3=running`, and a fresh process resumed it, skipped
`s1`, ran `s2`/`s3`/`s4` and finished.

Only `completed` is resumable. A step recorded `running` was in flight when the
process died -- its outcome is unknown and its artifact may be half written, so
re-running it is the only safe reading of that state.

### Subtask results are not fed forward

**Done.** `workspace.py` gives each mission an artifact store. A step declares
what it `produces` and `consumes`; upstream artifacts are delivered to the desk
and pasted into the agent's prompt before it starts. `MAX_INJECTED_CHARS` (2400)
truncates long ones.

Still open: truncation is a hard character cut, not a summary, so a very long
upstream artifact loses its tail rather than being condensed.

### Spatial runtime edge cases

`office.py` binds tools to stations and makes agents walk to them. Open items:

- Walk time is straight-line distance; it ignores walls and the collision grid
  the renderer paths against, so a backend walk and a drawn walk can disagree on
  a route that has to go around the conference room
- Station capacity is modelled (`Station.capacity`) but the lock is always
  exclusive, so a capacity of 2 does nothing
- `station_wait_timeout` (45s) drops the agent through to calling the tool
  without the station rather than failing the step

### Memory is lexical, not semantic

`memory.py` scores similarity by IDF-weighted token overlap. It has no
dependency and no key, which is why it was chosen, but it cannot tell that
"speed up the cache" and "optimise memoisation" are the same task. Swapping in
embeddings means replacing `_similarity` and nothing else.

Also open: nothing ever marks an episode a failure except an emergency stop, so
`success_rate` is 1.0 for every agent and rerouting will not fire in practice
until agent runs can actually fail.

### Token telemetry is estimated on the simulator path

**Done.** `llm.acompletion` now returns an `LLMResult` carrying the provider's own
token counts, the model that actually answered, the attempt count and whether it
fell back; `pricing.py` costs it at that model's rate instead of two hardcoded
Gemini-flash constants. `_simulate_specialist_action` still estimates — it has to,
nothing was called — but every simulated turn is tagged `estimated: true` on
`token.update` and the mission is tagged `tokens_estimated` on `task.completed`, so
the gauge can say it is guessing rather than presenting a guess as a reading.
`max_tokens_budget_per_task` is enforced between waves in `_execute_plan`.

Every mission now also leaves a `trace.jsonl` beside its artifacts and a `per_step`
cost breakdown in `mission.json`. See `tracing.py` and [[PROTOCOL]].

### `_tokens_spent()` is a lifetime total, not a mission total

`BaseAgent.token_usage` accumulates across every mission the process has served and
is never reset, so `_tokens_spent()` — which sums it across the roster — climbs
monotonically. Two consequences, both real:

- `total_tokens` in `mission.json` is the process's running total, not the run's.
  `mission_tokens` (added with the per-step breakdown) is the correct per-mission
  figure; `total_tokens` is left alone because the budget gate reads it.
- The budget gate in `_execute_plan` therefore tightens as a process serves more
  missions, and a long-lived server will eventually refuse a mission on a budget
  that a fresh process would pass.

Fix is to scope the counter to the mission rather than the agent, which changes
what the budget means — deliberately out of scope for the telemetry work that
found it.

### Steps returned free text that nothing checked

**Done.** `contracts.py` measures every artifact against a spec inferred from its
promised filename and step description -- empty, the known stub, far under length,
missing structure or promised sections -- and `critic.py` asks a small model
whether the work answers its brief. A failure re-dispatches the same step to the
same agent with the complaint appended, at most `max_repair_attempts` (1) extra
times, shared across both checkers. A step that exhausts the budget completes
with `repairing: false` on its `step.critiqued` event rather than failing and
stranding the DAG behind it.

`_best_artifact` and the "Deliverable:" instruction both remain -- they make a
good artifact likelier, which is still worth having now that something checks.

### No eval suite

**Done.** `evals/` holds 14 golden tasks across four shapes, a cassette
record/replay transport that makes a scored run reproducible, an LLM judge that
scores each rubric line against the artifact files, and a committed
`scorecard.md` with a regression delta. `python -m evals.runner --suite all
--replay` runs offline with no key, and `--compare` proves two runs agree:
verified 14/14 identical.

Two things it measured immediately, both left visible rather than tuned away:

- `heuristic_plan` has no notion of a trivial goal. "What does SSD stand for?"
  fans out to its full 4-step bucket, so `adversarial_trivial` fails `max_steps`.
- `adversarial_missing_tool` targets the planner-tool-grants gap below. It is
  expected to be the weakest row; it is there to be measured, not to pass.

The quality column is empty until cassettes are recorded, which needs a real key
and real spend. The scorecard says so in prose rather than printing a number.

### Synthesis is string assembly

`_synthesize_results` concatenates Markdown. The "Final Orchestrator Verdict" is a
fixed sentence, not a conclusion drawn from the results.

### CORS is fully open

`allow_origins=["*"]` with `allow_credentials=True` — fine for local dev, must be
tightened before any deployment.

### `terminal` tool executes shell commands

`tools/terminal.py` is reachable by the `coder` and `data_analyst` agents. Fine
locally; needs sandboxing or an allowlist before this is exposed to anything
untrusted.

---

## Frontend

### `hud.js` handles events `app.js` never sends

`updateActiveDagNode` and `setQueue` exist but nothing calls them — the `dag.update`
event is emitted by the backend and broadcast, but `app.js`'s switch has no case for
it. The DAG is computed and sent but never visualised.

### Standalone simulation names are hardcoded

`simulateStandaloneTask` in `app.js` hardcodes Alice/Charlie/Bob/Eve and their log
lines. Adding or renaming an agent leaves the offline demo stale.

### Boss Dave never leaves his desk

`hangouts` are all in the bullpen, lounge and kitchen. The idle-wander code picks
from that list regardless of which room an agent is in, so the orchestrator would
have to path out of the boss office to reach any of them.

---

## Office rendering

Current state: 40 × 24 tiles, 213 props, `check_layout.py` clean.

### Known rough edges

- **Boss office is the thinnest room** — least prop density of the four
- **North wall could take another two or three pieces** to match the reference's
  end-to-end furniture run
- **Agents stand rather than sit.** Modern Interiors ships sit animations in its
  character generator; wiring them up means extending the character build in
  `build_atlas.py` and adding a seated clip to `drawAgent`
- **Nine desks in a 3×3 grid leaves more open floor** than the previous twelve.
  Either a fourth rank (there is vertical room now) or more aisle greenery

### Not started

- Boss office and kitchen have no automatic doors; the system is general
  (`map.doors` takes any number) so each is roughly a one-line addition
- The lounge has no games furniture — the reference has a foosball table, which
  neither pack contains

---

## Testing

11 tests pass in `backend/tests/` (`test_phase1..4.py`), covering health, agent
listing, WebSocket handshake and event flow.

**Not covered:**

- Any frontend code — no JS test runner is configured
- The asset pipeline — `check_layout.py` is a validator run by hand, not a test
- `_decompose_task` routing branches
- Tool execution paths beyond the registry surface

---

## Housekeeping

- `frontend/assets/modern_interiors/` still holds the **non-commercial** free pack.
  Confirm no free-pack sprite survives in the atlas before any commercial use —
  licence detail in [[STACK]]
- `furniture/new/` is a byte-identical duplicate of the free pack; safe to delete
- `tools/_slices/` holds 614 one-off debug crops from earlier sessions
- `backend/.pytest_cache/` is committed alongside the source
- `Preview.png` in the project root is unreferenced
