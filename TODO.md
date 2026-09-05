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

**Partly done, and the old numbers here were wrong in both directions.**
Re-measured: 395 atlas frames, of which 249 are props; the frontend references
**123**. Not 233 and 96.

The orphan count was wrong too — 16 authored PNGs sit in
`assets/generated/authored/` that `gen_tiles.py` no longer produces, not 8. But
**two of those 16 are still used by the map**: `tablet` and `whiteboard_arch`.
Deleting all the orphans, which is what the old entry told you to do, would have
taken two live sprites with them.

The other 14 are gone, and the atlas was rebuilt: **512×734 → 512×578, 204,700 →
140,478 bytes, a 31% reduction.** `check_layout.py` is clean at 194 props, and
every one of the 123 referenced frames was verified present in the new atlas
before this was committed.

Two things worth knowing before anyone does the rest of it:

- **`build_atlas.py` is byte-for-byte reproducible.** Running it on an unchanged
  tree produces an identical `office_atlas.png` and `.json` (same md5). That is
  what makes pruning safe to attempt at all: the diff is exactly the change.
- **A literal string scan of `frontend/js` is not a usage check.** Doors are
  drawn from a computed name — `` `${base}_${frame}` `` at `canvas.js:808` — so a
  naive scan reports all 25 `door_slide_*` / `door_ext_*` frames as unused and a
  prune based on it deletes the door animations. Any future pass has to special
  case the computed families.

Still open: **108 unused frames sourced from the packs** via `PACK_PICKS` in
`build_atlas.py`, which is where the remaining weight is. Left alone on purpose —
they are a palette for layout work, and unlike the authored orphans there is no
second signal (a missing generator) confirming they are dead rather than merely
unplaced. The method above is proven if you want them gone.

### `emergency_stop` doesn't reach a running LLM call

**Done, and this entry was stale.** `BaseAgent.cancel()` sets `_is_cancelled`
*and* cancels `self._inflight`, the task wrapping the request; the ReAct loop
catches the resulting `CancelledError` and falls through to the halt path at the
top of the loop rather than keeping a second copy of that bookkeeping
(`base_agent.py:63`, `:181`). A HALT pressed mid-call no longer waits out the
timeout.

**The mission now stops too.** Flagging the roster was only ever half of it:
`_execute_plan` kept scheduling, and `run()` clears `_is_cancelled` on entry
(`base_agent.py:102`), so the step in flight halted and the very next step the
DAG offered reset the flag and carried on spending. HALT stopped one turn, not
the run.

`cancel_all` now marks the mission in `_halted`, and the scheduler refuses to
dispatch, cancels what is in flight, fails the rest as `halted by emergency stop`
and emits one `task.failed`. Three things this needed that were not obvious
until the tests were written:

- **The scheduler had to be woken.** It parks in `asyncio.wait` on the steps in
  flight, so a flag is only read between waits — fine when the halt makes agents
  return, useless when one is stuck in a tool call or queueing for a station,
  which is when the switch actually gets pressed. An `asyncio.Event` per mission
  waits alongside the steps.
- **A returning agent looks exactly like a working one.** `BaseAgent.run`
  catches the cancelled request and *returns* "Task halted by emergency stop."
  — an ordinary string on an ordinary completed future. Taken at face value the
  scheduler files that sentence as the step's artifact and lets the DAG read it.
  Any step landing while the mission is halted is failed instead.
- **`.exception()` raises on a cancelled task** rather than returning the
  exception, so `.cancelled()` has to be asked first or the halt comes out of
  the scheduler unhandled.

`test_emergency_stop.py` pins all four behaviours; four of its five fail on the
old `cancel_all`.

Still open: a step that genuinely completed a moment before the HALT, and is
collected in the same batch, is discarded with the rest — the same conservative
reading resume already takes of a step left `running` by a killed process. And
the halt is not persisted, so a resume of a halted mission re-runs those steps
rather than being told the operator stopped them on purpose.

---

## Backend

### Task routing is keyword matching

**Done.** `planner.py` asks the model for a validated DAG and falls back to the
old buckets when there is no key or the reply will not validate. The buckets
survive as `heuristic_plan`, now with real dependencies instead of a fixed chain.
Contract in [[PROTOCOL]] (`plan.created`).

**Also done: the plan is now checked against tool grants.** The prompt had
listed each agent's tools for a while, but nothing checked the model read them,
and a mismatch does not fail cleanly — the agent runs, finds it has no
`terminal`, and writes a plausible report with an invented number in it, which
is the worst available outcome.

A step now declares `needs_tools`, and `validate_plan` takes the roster's actual
grants. A step naming a tool its agent does not hold is **moved** to one that
does, preferring an agent already in the plan so a three-step plan does not
scatter across the roster; `regranted_from` records the move, and it reaches
`mission.json`. Repaired rather than rejected, because the plan is right about
the work and wrong only about who does it.

Three cases the tests pin, because each could reasonably have gone the other
way: a hallucinated tool name is held by nobody and is ignored rather than
treated as a requirement; a real tool nobody in the office holds is dropped the
same way, with the *rest* of the requirement still honoured, because half a
capable agent beats none; and a pair of tools that exist but never together
leaves the step exactly where it was, because reassigning to someone who will
also fail hides where the plan went wrong.

`agent_tools` is an optional third argument, so `validate_plan(raw, ids)` still
behaves as it did. `evals/tasks/adversarial_missing_tool.yaml` is the task that
targets this; its rubric row stays unjudged until cassettes exist.

Still open: `heuristic_plan` does not populate `needs_tools`, so the fallback
planner is unaffected — with no key, this pass has nothing to check.

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

**Also done: truncation keeps both ends.** It was `body[:2400]` — which keeps
the introduction and throws away the conclusion. For the documents this system
actually produces, the findings, the recommendation and the numbers are at the
*end*, so the hard cut discarded precisely the part the next step was waiting
for, and left nothing to say it had: the downstream agent could not tell a
truncation from an author who gave up.

`workspace.condense` keeps a 60/40 head/tail split, breaks on line boundaries so
the elision does not read as corruption, and states the gap — `[N characters
elided from the middle]`. Below 400 characters there is no room to keep both
ends and explain the gap, so it falls back to the old head cut, still labelled.

Deliberately **not** a model call, which is what "condensed" would have implied.
Summarising every handoff puts a provider round-trip and a bill on an operation
that runs once per edge of the DAG, and introduces a place where the artifact a
downstream agent reads is not the artifact its author wrote. The author's own
words survive at both ends instead.

### Spatial runtime edge cases

`office.py` binds tools to stations and makes agents walk to them. Open items:

- Walk time is straight-line distance; it ignores walls and the collision grid
  the renderer paths against, so a backend walk and a drawn walk can disagree on
  a route that has to go around the conference room
- ~~Station capacity is modelled but the lock is always exclusive~~ — **stale.**
  `_lock_for` is an `asyncio.Semaphore(capacity)` (`office.py:284`), so the
  conference table's capacity of 4 admits four. Now covered by
  `test_station_capacity.py`, which is worth having because the difference is
  invisible without contention: one agent at a time passes either way, and a
  regression to `asyncio.Lock` would only show up as meetings queueing.
  The tests wait on the admitted count rather than sleeping, because
  `use_tool_station` walks the agent to the station first and that is simulated
  time that varies with distance.
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

**Done.** `BaseAgent.token_usage` still accumulates across every mission the
process has served — nothing resets it, and nothing should; it is the agent's
lifetime record. What changed is who reads it raw. `execute_task` now snapshots
the roster's total into `_token_baseline[task_id]` before planning, and
`_mission_tokens_spent()` returns the difference, so the budget gate in
`_execute_plan` measures this run and no other.

The snapshot is what keeps the fix honest. Summing `_step_telemetry` instead
would also have scoped the figure, but a row only appears there once a step has
*finished*, and the gate is checked while steps are in flight — it would have
stopped noticing a mission running away with itself until after it had. The
baseline keeps per-turn resolution and costs one integer per mission.

`mission.json` follows: `total_tokens` is now this run's, measured the way the
gate measures it, and sits alongside `mission_tokens`, which sums the per-step
rows. They differ by the turns no step can be billed for, and `total_tokens` is
the larger. [[PROTOCOL]] says so.

Measured: a roster carrying 10,000 prior tokens against a 1,000-token budget used
to fail all three steps of a mission that went on to burn 30 —
`test_token_budget.py` pins that case, and pins that the gate still fires on a
mission's own overspend so the scoping did not quietly disarm it.

Still open: a resumed mission re-baselines, so its budget governs the work left
rather than the work the dead process had already paid for. That is the reading
this chose deliberately, but it does mean the two halves of a resumed mission can
together exceed one budget.

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

Two things it measured immediately, both left visible rather than tuned away
at the time -- and both since fixed, which is the point of measuring:

- `heuristic_plan` had no notion of a trivial goal. "What does SSD stand for?"
  fanned out to its full 4-step bucket. **Fixed** -- see below.
- `adversarial_missing_tool` targets the planner-tool-grants gap. Its structural
  row passes (the code bucket already routes execution to `coder`); what it
  actually measures is the rubric, which is still unjudged.

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

**Also done: `grant_to` is now per tool as well as per server.** The list form
is all-or-nothing, and an agent granted a filesystem server got its
`delete_file` along with its `read_file` — the wrong default for the one server
type most likely to be pointed at a real disk.

`grant_to` now takes either shape: `["coder"]` for every tool, or
`{"coder": ["read_file"], "researcher": "*"}` per tool. Grants are written
against the server's own tool names, since that is what a server's
documentation lists, though the prefixed name is accepted too for anyone
copying out of a log. `_resolve_grants` is separate from `register_server` so
the rules can be read without a registry or an office in the way. An unknown
agent or tool name is a logged warning that costs that line, not the server —
a typo in `mcp_servers.json` should not lose a working integration.

Still open: nothing re-lists tools after startup — a server that gains a tool
while the office is running is not seen until a restart.

### Synthesis is string assembly

**Done for the part that mattered.** The assembly is still assembly — the
execution graph, the handoffs and the artifact list are bookkeeping and should
be — but the closing "Final Orchestrator Verdict" was a fixed sentence: *"All N
steps completed and every artifact filed. System state is healthy and ready for
the next command."* That is a claim about the scheduler dressed as a claim about
the work, and it printed identically for a mission that answered the goal and
one where four specialists each filed a stub. It is why `evals/judge.py` grades
the artifact files and pointedly never the briefing.

`synthesis.py` asks a small model the one question the assembly cannot: given
the goal and what the specialists actually produced, what is the answer? It
reads the artifact *bodies*, not the steps' closing chat messages, and it is
told which steps failed so it can name the hole rather than conclude confidently
around it. The result leads the briefing, above the execution graph, because a
reader wants the finding before the audit trail.

Bounded like the critic: one call, no tools, `synthesis_model` defaulting to the
small one, `synthesis_max_tokens` (600), each artifact trimmed to 3000 chars
before a 12000-char total so one 40KB report cannot crowd out the four short
ones that disagree with it. `synthesis_enabled=False` restores the old closing
line exactly.

Skipped with no credential — there is no offline conclusion, because the
simulator would be summarising its own invented text. The skip is *stated* in
the briefing rather than hidden: a mission with no conclusion and a mission
whose conclusion was that everything is fine must not look alike, which was the
original bug in a different costume.

Still open: nothing grades the conclusion. The judge still reads artifacts only,
which is right — but it means the one part of the briefing that now makes a
claim is the one part the eval suite does not check.

### CORS is fully open

**Done in WP7; this entry was stale.** `main.py:104` reads `settings.cors_origins`
(default `["http://localhost:8080"]`) and sets `allow_credentials` only when the
origins are explicit, so the wildcard-plus-credentials combination browsers reject
can no longer be configured by accident. See the WP7 section below.

### `terminal` tool executes shell commands

`tools/terminal.py` is reachable by the `coder` and `data_analyst` agents. Fine
locally; needs sandboxing or an allowlist before this is exposed to anything
untrusted.

---

## Frontend

### `hud.js` handles events `app.js` never sends

**Done.** `app.js` has cases for `plan.created` and `dag.update`, and `hud.js`
draws the graph as SVG: ranked by dependency depth, real edges, the owning
agent's manifest colour as the node fill, status as its ring and glyph, a pulse
on the running one, and what each finished step cost written under it. A `⇄`
badge marks a step memory took off the planner's pick and a scored badge marks a
reviewer's verdict -- red when it was rejected with no repair budget left, which
otherwise reads exactly like a pass. Both badges live outside the SVG in
`hud.stepBadges`, because `dag.update` redraws the whole graph on every status
change.

`updateActiveDagNode` survives as a one-line shim that lights the dock card;
`setQueue` is gone.

### There was no cost panel and no way to replay a run

**Done.** `dag.update` now carries each finished step's cost, latency and tokens
plus the mission totals (see [[PROTOCOL]]), so the MISSION COST card shows live
spend, wall clock, a per-step latency strip and tokens by agent -- every figure
per-mission, and marked `~est` when it came from the simulator.

`frontend/js/replay.js` drives the office from a past mission's `trace.jsonl`
instead of the live socket: play, pause, scrub, 1x-8x. Recorded spans are turned
back into the same events the WebSocket sends and handed to the same
`handleEvent`, so the renderer is not forked and nothing in `canvas.js` was
touched. Walks are reconstructed by looking each `tool` span's tool up in
`/api/office`.

Still open: the trace holds no artifact bodies, so a replayed mission shows the
work happening but cannot show the briefing -- it says so rather than inventing
one. And a replay is all-or-nothing: seeking backwards replays from zero rather
than stepping back, because the office is a state machine with no inverse for
"the agent walked to the library".

### No hero image

`Preview.png` has been deleted — it was a third-party asset pack's contact
sheet, not a screenshot of this project, and nothing referenced it. The README
opens with a Mermaid diagram and the scorecard instead.

Still open, and now the only thing on this list that cannot be done from a
terminal: a recording of a mission — planning, fanning out, the DAG lighting up,
the cost ticking, the memo landing. It needs a browser driving the running app,
and it is the one claim in the README the README cannot itself show.

### Standalone simulation names are hardcoded

`simulateStandaloneTask` in `app.js` hardcodes Alice/Charlie/Bob/Eve and their log
lines. Adding or renaming an agent leaves the offline demo stale.

### Boss Dave never leaves his desk

**Stale, and the premise does not hold.** The observation is right that
`hangouts` are all outside the boss office and that the idle-wander picks from
that list regardless of room. The conclusion — that Dave therefore cannot get to
one — is wrong: the two doorways at `x=27` (`y=9` and `y=17`, both 2 tiles high)
connect the boss office to the rest of the floor, and a breadth-first search
over the masonry grid from his seat at `(32, 8)` reaches **all seven** hangouts,
the library and conference room included.

So he does wander, whenever the office is not `liveDriven` and he is not
`active`/`thinking` — the same conditions as everyone else.

What is *not* verified here: the search was over walls and doorways only. The
walkable grid the renderer actually paths against also marks furniture, computed
from the atlas rectangles, and reproducing that outside the browser needs
`check_layout.py`'s prop-rect machinery. A prop could in principle bottleneck a
doorway that the masonry leaves open. If Dave is ever seen stuck, that is where
to look — not at the hangout list.

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

**354 tests pass** in `backend/tests/` (`python -m pytest -q`, Python 3.10.9),
across 29 files: the original phase tests plus telemetry, contracts, the critic,
the run store, resume, the sandbox, semantic memory, debate, MCP, the eval
harness, the `dag.update` payload, the token budget, the kill switch, plan tool
grants and the conclusion. CI runs the same suite on
3.10 and 3.12, with `ruff check` and the eval smoke suite.

The CI eval step is described in the workflow as replaying cassettes, which it
does not: there are none, so `--replay` finds no recording, `credentials_for`
clears credentials in simulate mode and the suite runs the simulator. The run is
deterministic and offline either way, which is what the step is there for — but
the comment claims a stronger guarantee than the repo currently earns, and will
only become true once cassettes exist.

**Not covered:**

- Any frontend code — no JS test runner is configured, and adding one would be
  the project's first node build step. The DAG panel, the cost panel and the
  replay driver are verified by hand and by `node --check`, and
  `tools/check_replay.js` compiles a real `trace.jsonl` through the actual
  `replay.js` and checks the result against the mission manifest beside it —
  a hand-run validator in the same spirit as `check_layout.py`.
- The asset pipeline — `check_layout.py` is a validator run by hand, not a test
- Tool execution paths beyond the registry surface and the sandbox refusals

---

### The fallback planner answered every goal the same way

**Done.** `heuristic_plan` was four keyword buckets and nothing else, so every
goal got the same shape of plan. The eval suite measured five separate
consequences of that, and all five are now green:

- **A trivial goal is one step.** "What does SSD stand for?" hired four
  specialists; it now gets one, producing `answer.md`.
- **A goal with no subject gets two.** "Make it better." fanned out to four
  specialists inventing four different subjects; it now establishes one reading,
  names the assumption, and answers it.
- **A goal that asks for a room gets a meeting.** "Hold a standup", "discuss as
  a team" produced four solo desks in a harness that supports meetings. It now
  plans research in parallel, a chaired meeting that depends on it, and a
  write-up that follows the recorded decision.
- **The deliverable is named after what was asked for.** Every mission ended in
  `briefing.md`; a goal asking for a report now produces `report.md`, a guide
  `guide.md`, a summary `summary.md`. `consumes` follows the rename, which
  `test_heuristic_plan.py` checks -- a step consuming a file nothing produces
  starts at an empty desk.

Scorecard: 95% aggregate with five failing rows, to **100% across all 14**, and
`evals/scorecard.md` carries the fail → pass line for each.

**The caveat, stated rather than buried:** these are keyword heuristics written
against tasks the suite already contained. Each is defensible on its own terms,
but a suite you fix code against is one you can over-fit to, and 14 tasks is
small. They also only affect the *fallback* planner -- with a key, the model
plans and these branches never run.

Still open: `_is_trivial` and `_MEETING_RE` are English-only and match on
surface words. A goal that asks for a meeting without using any of those words
still gets solo desks.

### The eval harness could never record a cassette

**Done -- four separate faults, each fatal on its own.** The quality column was
empty because nothing had ever recorded; nothing had ever recorded because:

1. `run_task` installed the replay dummy key in `record` mode as well, so every
   recording authenticated as `replay-not-a-real-key` and died on a 400.
2. `Settings.model_config` used `env_file=".env"`, resolved against the process
   working directory. `python -m evals.runner` is documented to run from the
   repo root, where there is no `.env` -- so the harness saw no credential at
   all, every task fell to the simulator and the judge reported "no model
   configured". Both env files are now absolute and anchored to `config.py`,
   `backend/.env` last so a run started from `backend/` is unchanged. The same
   bug silently affected `python -m mcp_server` from anywhere but `backend/`.
3. The judgement was taken *outside* the cassette transport, though
   `judge.py`'s own docstring says it goes through it. A replayed run therefore
   re-graded live -- money on every replay, and no grade at all in CI.
4. `memory.render_hints` writes ", took 41.2s" into the hints block, and that
   number is wall clock, so a step recorded at 41.2s and replayed at 0.9s
   missed its own cassette. Normalised out of the cassette key, exactly as the
   date already was; `test_evals.py` pins it.

Fixing (2) had a consequence worth naming: with credentials now visible from
the repo root, `--replay` began making real calls for tasks that have no
cassette. The offline promise had been resting on the working directory rather
than on the code, so `credentials_for` now clears credentials outright in
simulate mode. Verified: `--suite all --replay` returns all 14 verdicts
unchanged, offline, in three minutes.

Still open: **the cassettes themselves.** Recording is now possible and was
proven on `code_cache_bug` (structural 100%, quality 100%, $0.0258, 14
cassettes), but the account's Gemini quota ran out partway through --
`gemini-2.5-flash` now silently answers via the `3.5-flash-lite` fallback, and
a later recording of the same task produced a run where both steps failed. The
suite is one `--record` sweep away from a filled quality column once quota
allows; nothing in the code blocks it now.

### The container ran uvicorn's reloader

**Done.** `backend/main.py` passed `reload=True` unconditionally, so the image
started a reloader over source that cannot change -- and because `watchfiles` is
deliberately a dev dependency, uvicorn fell back to `StatReload`, stat-polling
every file in the image. The extra process also makes a crashed app look like a
restarting one. Reload is now on unless `ENVIRONMENT=production`, which
`docker-compose.yml` sets; a local `python main.py` is unchanged.

Verified by booting both ways on a runtime-only venv: development logs
`Started reloader process ... using StatReload`, production logs only
`Started server process`.

## Housekeeping

- **Done.** `Preview.png` deleted — a third-party asset pack's contact sheet
  that nothing referenced. It is in git history if it is ever wanted back.
- **This entry was stale:** `backend/.pytest_cache/` is *not* committed.
  `.gitignore:59` covers it and `git ls-files` returns nothing for it; the
  directory exists locally because pytest writes it, which is not the same
  thing.

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

