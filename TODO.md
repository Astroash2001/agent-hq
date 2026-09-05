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

**Done.** `embeddings.py` provides `embed()` with two providers -- a
deterministic hashed bag of words and character trigrams (the default: no key,
no network, no package, identical across processes) and `litellm` for a real
embedding model. `memory._similarity` is now a cosine over those vectors, with
the old IDF-weighted overlap kept as `_lexical_similarity` for when embeddings
are unavailable. The prediction in the old version of this entry held: nothing
outside `_similarity` had to change.

Measured on the repo's own episodes: "Optimise the caching layer" against
"Cache optimisation for the checkout service" scores **0.000** lexically -- no
shared token -- and **0.176** semantically, which is enough to recall the
episode and fire a `route.changed`. `test_semantic_memory.py` asserts exactly
that pair, both ways.

Routing is also quality-weighted and time-decayed: `competence` blends the
success rate with mean critic `quality` (`memory_quality_weight`, 0.4) over the
episodes something actually scored, and weights each episode by
`0.5 ** (age_days / memory_half_life_days)`.

**Also done, in WP4:** failures reach memory. `_handle_step_failure` records an
episode with `outcome="failure"`, so `success_rate` is no longer 1.0 for
everyone and rerouting has something to fire on.

Still open: hashed n-grams measure *surface* similarity. "Speed up the cache"
and "optimise memoisation" share no letters and still score 0.0 -- true
synonymy needs `embedding_provider="litellm"`, which costs a call per uncached
text. And `competence` scores the step description alone against episodes
indexed on goal *and* subtask, so the two sides are diluted unevenly; the floor
was measured with that asymmetry in place rather than corrected for.

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

### Meetings were a monologue

**Done.** `debate.py` gives every attendee exactly one turn before the chair
sums up -- one bounded call, no tools, no ReAct loop, run concurrently under a
single `asyncio.gather` because the turns are independent. Positions are
emitted as `agent.argued` (see [[PROTOCOL]]), rendered as speech in the office,
and each turn is its own trace span billed to that attendee's own token usage,
so the price of the argument sits in the same gauge as the work.

Bounded by `debate_rounds` (1) x `meeting_capacity - 1` seats x
`debate_max_tokens` (350). `debate_enabled=False` restores the silent-attendee
meeting exactly, which `test_debate.py` asserts against the chair's context
string.

Still open: a round is stateless -- attendees see each other's positions only
from the round before, so with the default of one round nobody replies to
anybody. Raising `debate_rounds` gets a reply round at a linear cost, and the
chair still owns the decision either way.

### No MCP

**Done, both directions.** `mcp_client.py` reads `backend/mcp_servers.json`
(stdio or HTTP), lists each server's tools at startup, and registers them into
the existing `tool_registry` as `mcp__<server>__<tool>`, bound to a new
`integrations` station -- so they emit the same events, are timed the same way,
and the agent walks to the bench to use them. `mcp_server.py` serves this
office's own `run_mission` / `get_mission` / `list_agents` over stdio, so Claude
Code or any MCP client can drive the harness. `mcp_servers.example.json` and a
README section show the config.

Off by default (`mcp_enabled`), `mcp` pinned in `requirements-dev.txt` only and
imported lazily, and an unreachable server is a logged warning that does not
block startup.

Still open: `grant_to` is per server, not per tool, so an agent granted a
server gets everything that server offers. And nothing re-lists tools after
startup -- a server that gains a tool while the office is running is not seen
until a restart.

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

- `backend/.pytest_cache/` is committed alongside the source
- `Preview.png` in the project root is unreferenced

---

## Hardening and Sandboxing (WP7)

### Terminal tool allowlist and sandboxing

**Done.** `backend/tools/terminal.py` enforces `settings.terminal_allowed_commands`,
rejects shell metacharacters before execution, runs with `asyncio.create_subprocess_exec`
(`shell=False`), jails `cwd` to `settings.jail_root`, scrubs secrets from child
environments (preserving only `PATH` and `SYSTEMROOT`), caps output at
`settings.terminal_max_output_chars`, and hard-kills and reaps timed-out children.
Refusals return structured dictionary payloads with `refused: true`.

### File ops path containment

**Done.** `backend/tools/file_ops.py` (`read_file`, `write_file`, `list_files`)
enforces containment within `settings.jail_root` (the repository root) or the active
mission workspace directory. Traversal attacks ('..') and absolute escapes are rejected.
Unbound writes are jailed to designated data/missions paths.

### API authentication and network hardening

**Done.** `backend/main.py` enforces explicit CORS origin configuration with
`allow_credentials` disabled when origins is wildcard `["*"]`. Optional shared
secret token `settings.api_token` gates `POST /api/emergency-stop` and `WS /ws`.
Per-connection rate limiting throttles rapid `submit_task` WebSocket requests.

### Project shipping artifacts

**Done.** Added `pyproject.toml` (packaging metadata, pytest asyncio mode, ruff),
`Dockerfile` (non-root `agenthq` user, healthcheck on `/api/health`, minimal runtime),
`docker-compose.yml`, `.dockerignore`, `LICENSE` (MIT), and GitHub Actions CI workflow
matrixing Python 3.10 and 3.12 with linting and tests.

