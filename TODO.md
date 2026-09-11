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
reading resume already takes of a step left `running` by a killed process. 

**A halt is now its own mission status.** It used to be recorded as `failed`,
so in the mission list a run somebody stopped on purpose and a run that broke
looked identical — which is the first thing you want to know about a red row.
`_mission_outcome` returns `halted` in preference to `failed`, because the
steps that failed did so *because* of the halt. `api_missions` gates resume on
`completed` alone, so a halted mission stays resumable, and resuming it re-runs
the halted steps — which is right: you resumed it to finish it.

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
targets this, and it is now recorded and judged: **structural 100%, quality
100%** — including the rubric line "the step that needed execution was given to
an agent holding the terminal tool", which was the whole point of the task.

**Also done: the fallback planner declares its tools too.** `heuristic_plan`
left every step with an empty `needs_tools`, so the pass was a no-op on the one
path that runs with no key — which is the configuration most likely to be
somebody's first look at this. The buckets now name `web_search` for research
steps, `terminal` for the ones that run code, `list_files` for the data pass,
and `regrant_steps` (lifted out of `validate_plan` so both planners share it)
runs over the result. A test asserts the default roster needs no regranting at
all: the pass is a guard, not a crutch, and if that starts failing it means a
bucket names the wrong agent.

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

- **Walk time is straight-line distance, and the reason that has not been fixed
  turns out to be worse than the bug.** `export_layout.py` now emits the
  `blocked` grid alongside the seats and stations — the same grid
  `canvas.js buildCollision(true)` builds and the same one the renderer's A*
  paths on — so the backend *could* time a walk over the real route. It does not
  yet, because the grid is not usable for that:

  **The walkable grid is 7 disconnected regions.** The largest holds 324 of 386
  walkable tiles; the conference-room strip and a boss-office pocket are islands.
  A* can never route between them. `stations.ops_console` sits on one of those
  islands and `stations.conference_table` sits inside the boardroom table with no
  open tile beside it, so neither is reachable on the grid at all.

  The office looks fine anyway because `stepAgent` falls back to sliding when
  A* finds no path, and sliding tests a tile by its **top-left corner**
  (`blockedByProp`) rather than its centre, and exempts an agent's own seat. Two
  different collision rules, one of which is doing nearly all the work — so the
  pathfinder has been mostly decorative and nothing surfaced it, because the
  backend was timing straight lines regardless.

  **Both halves have since landed, and this entry is stale.**
  `office.travel_seconds` paths over the exported grid and falls back to
  straight-line only when two points are not connected. The map was opened up
  too: `check_layout.py` reports **2** walkable regions with 476 of 478 tiles in
  the largest — not the 7 regions and 324 of 386 described above. Two tiles
  remain stranded and no route needs them.
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
  actually measures is the rubric, which now scores 100%.

The quality column is filled: all fourteen tasks are recorded against
`openai/gpt-5-mini` and committed, so the scorecard prints measured numbers
rather than prose explaining their absence. Thirteen carry a judged score; see
the Testing section for the one that does not, and for the three faults that
stood between "the cassettes exist" and "anyone else can replay them".

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

**Also done: the meeting is an argument now.** At `debate_rounds: 1` the
`earlier` transcript is always empty, so nobody replied to anybody and the
meeting was a row of statements read out side by side.

Raising the default to 2 was necessary and not sufficient. Handing a second
round the same "State your objection." prompt with the transcript merely
appended gets attendees restating themselves next to each other -- the
transcript is background, not something to engage with. A later round is now
told explicitly that it is a REPLY round: name who you are answering, say where
you now agree, say what you still disagree with, and say plainly if someone has
changed your mind.

Cost is linear and small -- rounds x seats x `debate_max_tokens`, and a turn is
three sentences. `debate_rounds: 1` restores the silent version exactly, which
`test_the_reply_round_can_be_turned_off` pins.

One test had to be pinned rather than updated: it read the default round count
while testing the simulated fallback, so it broke the day the default changed
and told nobody anything useful.

### No MCP

**Built in both directions, then removed on purpose.** `mcp_client.py`
registered other people's tools into `tool_registry` as
`mcp__<server>__<tool>`; `mcp_server.py` served this office over stdio. Both
worked, both were off by default, and both are gone.

The reason is a trade, and it is written up in [[DECISIONS]]. Switching MCP on
means `npx`, third-party servers and network at runtime, against a harness
whose whole pitch is: clone it, run it with no key, replay it deterministically.
That property is worth more here than an open-ended catalogue, because nobody
is pointing this at a production data source.

What survived is the part that was never MCP-specific:
`tool_registry.catalogue()` as the single authority on what exists, and
`register(..., risk=)`. `OfficeFloor.bind_tool` did not -- it gated a
runtime-registered tool onto a station, which only MCP ever did, so with MCP
gone it had no caller. Every tool in the registry now exists at import and is
named in `STATIONS`.

The question MCP answered -- "the mission needs a capability nothing provides"
-- now has a plainer answer: `foundry.py` refuses to hire and names the missing
tool. Writing one in `backend/tools/` is around eighty lines plus a guardrail,
and the four added with the foundry are the worked examples.

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

**Partly closed: the conclusion is contract-checked.** It goes through
`contracts.check` before it is printed, so a conclusion that is a stub, or one
sentence long, is rejected and the briefing says a conclusion was not drawn
rather than leading with a placeholder. Free and deterministic, and the cost of
the call is still reported when the answer is thrown away — a price that
vanishes because the output was unusable understates the run.

**Also done: the conclusion is grounded against the artifacts.** Every figure
it asserts is checked against the text it was drawn from, and any that appears
in none of them is named under the conclusion and recorded in `mission.json` as
`ungrounded_figures`. A model that misreads $61/kWh as $45/kWh writes the second
as confidently as the first.

Matched on the digits alone, in a corpus with separators stripped, so `$61/kWh`
grounds against "61 USD per kWh" and nothing is flagged over how its units were
written. Years and integers under thirteen are skipped: a year is context, and a
small number is far more often a list position or a count of sources than a
measurement, so checking them would bury a real finding under noise.

**Reported, never rejected**, and that is the important half — $98 minus $61 is
$37, and arithmetic on grounded numbers is what a good summary does. Rejecting
on this would throw away good work to catch a bad number; naming the figure lets
a reader check it in seconds. `ungrounded_figures` is written even when empty,
because "checked and clean" and "never checked" must not read alike.

Still open: it checks figures, not claims. "Sodium-ion is cheaper" grounds
against nothing and is not caught -- that needs an entailment judgement, which
is a model call and a different kind of check.

### CORS is fully open

**Done in WP7; this entry was stale.** `main.py:104` reads `settings.cors_origins`
(default `["http://localhost:8080"]`) and sets `allow_credentials` only when the
origins are explicit, so the wildcard-plus-credentials combination browsers reject
can no longer be configured by accident. See the WP7 section below.

### `terminal` tool executes shell commands

**Stale** -- hardened in WP7 (allowlist, no metacharacters, jailed cwd, scrubbed
env). See the WP7 section.

### Agents are a fixed roster of five

**Done.** `foundry.py` hires a specialist when a step declares tools no single
agent holds. They enter by the street door at (24, 23), take one of the two
spare bullpen desks, and are dismissed when the mission ends.

The trigger already existed: `regrant_steps` computes exactly this condition
and gave up on it, which was right while the roster was fixed.

What the foundry adds on top of that is one distinction `regrant_steps` cannot
draw. From there, a tool nobody was granted and a tool nobody implements look
identical -- both get ignored as invented -- and they need opposite responses.
Against `tool_registry.catalogue()` they come apart: the first is hired for,
the second fails loudly, naming what is missing -- which is a request to go
and write that tool. Hiring for the second would produce an agent holding a name with no
function behind it, which is this project's oldest failure in a new costume.

The model writes the contractor's identity and never its tools -- the list is
`gap.needed` and is not read back from the reply, so a design naming `terminal`
does not get `terminal`. Tested. With no credential the design is deterministic
instead, so the feature is not invisible on the no-key path.

Two caps, smaller wins: `max_spawned_agents` (2) and the two spare desks. A
third hire is refused rather than seated on `DEFAULT_SEAT`, where two
contractors would stack and read as one agent.

Persisted in a `spawned_agents` table rather than `agents_dir`, and rehired on
resume -- without which a resumed mission raises "unknown agent" on exactly the
step the hire was for, and only after a process restart, which is the hardest
kind of bug to catch.

**Contracts end on every exit path, not just the happy one.** Dismissal sat at
the end of `_run_mission`, but the mission has a try/except/finally around it,
so a mission that raised left its hire in `orchestrator.agents` -- and
`get_manifests` feeds that dict to the planner as the roster. Every later
mission in the same process would have been offered an agent nothing on disk
knows about.

Worst exactly where it is hardest to see: an eval sweep runs fourteen tasks in
one process and the runner catches a failure per task so the suite continues.
One failure after a hire re-keys the planner prompt of every task after it, and
the recording then does not replay from a clean process. Two earlier sweeps
were thrown away for faults of that shape. Found before the next sweep was
paid for, and the regression test fails without the fix.

Still open:

- **A gap rarely fires offline now**, and that is the design working rather than
  a fault: the four specialists cover most combinations since the catalogue was
  widened, so the fallback planner's buckets do not produce one. It fires when
  a planner declares a combination that crosses agents. To watch it happen,
  narrow an agent's tools in `backend/agents/*.json` and re-run.
- **No eval task covers it.** The suite cannot score the foundry until the
  cassettes are re-recorded, so this is verified by unit tests and by hand.
- **A contractor borrows a sprite.** Hue-rotated with a visitor badge, because
  the atlas holds five characters and the packs that made them are gigabytes
  and not in the repo. A real contractor character needs the paid Character
  Generator and a `build_atlas.py` run.

### The tool catalogue, and the grant that was not enforced

**Done.** Four tools added, taking the built-ins from six to ten, because the
roster could not do ordinary work: `data_analyst` held `read_file`,
`list_files` and `terminal` -- no data tool at all -- and nothing anywhere
could open a URL it had been handed.

- `fetch_url` -- one named page or API as text
- `query_data` -- read-only SQL over a CSV or JSON file, via stdlib sqlite3
- `calculate` -- exact arithmetic, so a figure in a report is computed, not recalled
- `search_workspace` -- matching lines across the mission's files

No new dependency: `httpx` was already one, the rest is standard library.

**The grant check came first, and mattered more than the tools.**
`tool_registry.execute` never verified the caller held the tool it named. It
held up only because an agent is *shown* schemas for its own tools -- a habit
of the model, not a property of the system. That is fine while every manifest
is hand-written and reviewed, and stops being fine the moment a tool list is
chosen at runtime. Enforced now, and the refusal names what the agent *does*
have, so the model corrects instead of retrying synonyms until the step budget
is gone.

**Two findings while building it, both silent in the way this project cares about:**

- `csv.reader` returns strings, so a numeric column sorted **lexically**:
  `'80'` above `'120'`. Aggregates were right, because SQLite coerces text for
  `SUM`, so nothing errored -- `ORDER BY revenue DESC` just put the wrong region
  at the top of a ranked table. Cells are typed on load now, with leading zeros
  left as text because `01234` is a postcode more often than it is 1234.
- The first containment test for `query_data` passed against no containment at
  all. `file_ops._get_read_allowed_roots` allows `missions_path` *and its
  parent*, so pointing `missions_path` at a `mkdtemp` directory silently allows
  the entire system temp directory -- which is where the test had put its
  "outside" file. The fixture nests the mission dir one level down now.

**Still open: the eval cassettes are stale.** Agent tool lists are in the
planner prompt, so widening them changed the first planner call's cassette key
and all 14 tasks now miss. Nothing is broken -- `--replay` fails loudly, which
is what it is for -- but the suite cannot score until one `--record` sweep is
paid for. The committed scorecard is the last valid run and is now protected:
a sweep where every task errors refuses to overwrite it, having done exactly
that once.

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

**Done: a replay shows the briefing now.** The trace still holds spans and not
bodies -- putting artifact text in it would bloat the one file that has to stay
readable when a process is killed mid-write -- so the briefing is fetched off
disk instead, from a new `GET /api/missions/{id}/artifacts/{name}`. The name is
resolved and compared against the mission directory rather than pattern-matched:
`..%2f` survives URL decoding, and a blocklist of bad shapes is a losing game
against a string that reaches the filesystem. A mission that left no `report.md`
gets a note saying so, because an empty memo reads as "this mission concluded
nothing", which is a different claim from "this was never written".

Still open: a replay is all-or-nothing -- seeking backwards replays from zero rather
than stepping back, because the office is a state machine with no inverse for
"the agent walked to the library".

### No hero image

**Done.** `docs/hero.gif` -- twenty-five seconds of a real mission, at the top
of the README.

`Preview.png` was deleted earlier: it was a third-party asset pack's contact
sheet, not a screenshot of this project, and nothing referenced it.

The window is 32-57s of a 2:11 capture, chosen by sampling twelve stills across
the run rather than guessing: it is the stretch where the boss calls a standup,
three specialists walk to the conference room, and the DAG, token gauge and cost
panel all move. The opening twenty seconds are an idle office and the closing
minute is one agent writing, so neither earns a place in a loop.

Made with the ffmpeg that `imageio_ffmpeg` bundles -- there is none on PATH:

```
crop=1920:1052:0:148   drop the browser chrome; the app is the subject
fps=8, scale=860       6.24MB at 10fps/900px, 3.62MB here
palettegen stats_mode=diff, max_colors=128
paletteuse dither=bayer:bayer_scale=3
```

`stats_mode=diff` because the office is mostly static, so the palette is better
spent on what moves. Bayer dithering because the default banded the flat colour
that most of this frame is. Frames were dropped before pixels: losing resolution
costs more in a pixel-art scene than losing smoothness.

### Standalone simulation names are hardcoded

**Stale -- already fixed.** `simulateStandaloneTask` builds its roster from
`hud.agents`, which is filled from `/api/agents`, and filters the boss out
because he plans rather than executes. Adding or renaming an agent is picked up
with no edit, and a contractor hired mid-mission appears in it too.

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

- ~~Boss office and kitchen have no automatic doors~~ — **done.** Both openings
  in the col-27 divider now have sliding leaf pairs. The one thing that is easy
  to get wrong: a vertical door hangs from `(y+1)*16` and covers rows `y-1` and
  `y`, so its `y` is the *lower* of the two rows its `doorways` entry carves —
  the boss gap is rows 9-10 and takes `y: 10`, the kitchen gap is rows 17-18 and
  takes `y: 18`. Verified by rendering: `python tools/preview_map.py` draws doors
  and shows both pairs flush in their openings, and `check_layout.py` is clean.
- The lounge has no games furniture — the reference has a foosball table, which
  neither pack contains

---

## Testing

**491 tests pass** in `backend/tests/` (`python -m pytest -q`, Python 3.10.9),
across 34 files: the original phase tests plus telemetry, contracts, the critic,
the run store, resume, the sandbox, semantic memory, debate, the eval
harness, the `dag.update` payload, the token budget, the kill switch, plan tool
grants, the conclusion and its grounding, station capacity, artifact
condensing, the exported office grid, routed walk timing, the artifact
endpoint, attachments, the tool-message wire shape and the recall horizon. CI
runs the same suite on 3.10 and 3.12, with `ruff check` and the eval smoke
suite.

**Done: the CI eval step really does replay now.** It used to describe itself as
replaying cassettes when there were none, so `--replay` found no recording and
ran the simulator. All fourteen tasks are recorded against `openai/gpt-5-mini`,
both smoke tasks among them, and CI replays them with no credential.

Three things had to be fixed before the committed recordings were usable by
anyone but the machine that made them, and the first was the worst:

- **`--replay` failed on all 14 tasks.** The model guard compared each cassette
  against `settings.default_model` — which, in a mode that reaches no provider,
  is this machine's env talking about a lane the run never calls. The env had
  moved to `minimax-m3-free`; the cassettes had not moved at all. An unnamed
  call is no longer checked against it, because `cassette_key` hashes the model
  exactly as the caller passed it: a key built from `None` can only match a
  recording made by a call that also passed `None`, so the hit is already proof
  the two are the same call. The labelling hole the guard exists to close is
  closed from the other end instead — the row prints the model the cassettes
  say answered, gathered while serving them.
- **Named calls still keyed on the environment.** The critic, the synthesis pass
  and the judge all name `settings.fallback_model`, and that name is in the key,
  so a machine with a different fallback missed exactly those cassettes and
  turned a scored suite into an unjudged one with no error. Each task now
  carries `_recording.json` beside its cassettes, naming the default and
  fallback it was recorded under, and replay pins both from it.
- **The latency budget made replay non-deterministic.** `max_duration_sec` was
  scored against wall-clock time on a run served from disk, so it measured the
  machine: `battery_comparison` passed it on one box and failed it on another
  from identical cassettes, and `--compare` correctly called two identical runs
  `NOT DETERMINISTIC`. It is `skipped` on replay now — out of the denominator,
  not passed — and still enforced on `--live` and `--record`. This is the only
  reason `adversarial_ambiguous` reads 83% structural rather than 86%: same
  failure, one fewer check in the denominator.

Two consecutive full replays now compare `DETERMINISTIC` across all 14 tasks
under a deliberately wrong `DEFAULT_MODEL`.

**Also fixed while in here:** `--suite smoke` used to rewrite the committed
`scorecard.md`, replacing its fourteen rows with two and taking `previous.json`
— the baseline the next run's delta column is computed against — with it. Only
`--suite all` may write the scorecard now; a subset says where its results went
and leaves the record alone.

Still open: `grid_storage_tradeoffs` has no quality score, because the judge's
reply on that task was not JSON. Reported as no grade rather than a zero, which
would blame the run for the judge's bad day — but it is a real gap in the
scorecard and re-recording that one task is what closes it.

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

**The cassettes now exist.** 12 of the 14 tasks are recorded against
`gemini/gemini-3.5-flash-lite` — 180 exchanges, 818K, committed. `--suite all
--replay` runs them offline and two consecutive replays agreed on all 14
verdicts (`--compare`: *14 identical, 0 drifted*). The quality column is filled.

`gemini-3.5-flash` is still rate-limited to nothing — every request to it
exhausts three attempts and falls back — so the sweep was recorded against the
lite model deliberately rather than by accident, and the scorecard names it.
The lite model's own quota then ran out on task 13, which is why
`solar_efficiency` and `solid_state_batteries` have no cassettes and fall to the
simulator. They say so; they do not borrow a score.

**What recording immediately measured, all of it left failing:**

- **Three tasks fail `min_steps`** — `battery_comparison`,
  `grid_storage_tradeoffs`, `code_cache_bug`. The real planner does a comparison
  in two steps where the suite wants three. On the simulator all three were
  100%, which is exactly the blindness recording was supposed to remove. Not
  relaxed: the suite and the model disagree about how much fan-out a comparison
  needs, and that is worth looking at rather than editing away. Relaxing the
  expectation to match the model is the over-fitting this suite already warns
  about, one level up.
- **`code_cache_bug` scores 67% on quality** — the judge found no execution
  evidence in the artifacts. The code was written and reasoned about, not run.
- **`battery_team_review` completed 1 of 4 steps** during recording, so its
  cassette set is incomplete and the judge call misses on replay. The harness
  reports the miss with the command to re-record, which is the behaviour that
  entry was designed for.

**And then recording turned out not to be enough**, which is the more
interesting finding. The transport wrapped `llm.acompletion` and nothing else,
so every *tool* call was live on every replay. An agent's first turn searches
the web; the results land verbatim in the second turn's prompt; when the web
moves, that prompt changes and the second turn misses its own cassette.

Observed rather than reasoned about: `adversarial_trivial` replayed 7 hits / 0
misses at 04:48 and again at 04:55, then at 05:08 missed on Alice's second turn
with a *stable* new key — stable because the search had settled on new results,
not because a line of the repo had changed. Four tasks dropped to quality 0 with
no code change between the runs. A suite that goes red overnight for reasons
outside the repo is the exact failure the cassettes exist to prevent, and it had
simply been moved from the model to the tools.

`tool_registry.execute` is wrapped now, keyed on tool name and arguments. Only
tools whose answer comes from outside the office are served from disk —
`web_search` and `fetch_url`. **`write_file` must genuinely run on
replay**: its side effect is the artifact the structural checks look for, so
serving it a recorded "ok" leaves the workspace empty and scores the mission as
having produced nothing. The rule is "record what the office cannot control",
not "record every tool".

A cassette also records the model that answered it, and a replay resolving to a
different model is now refused. Found because CI's smoke step, running on the
default `gemini-3.5-flash`, replayed flash-lite cassettes with 16 hits and 0
misses and printed `gemini-3.5-flash` in the model column beside answers that
model never produced. The key cannot fix this on its own — `base_agent` calls
`acompletion` with no model, so every agent turn hashes `""` — and resolving the
default into the key invalidates every committed cassette, which was verified by
doing it and watching all 14 tasks go to `CassetteMiss`.

**And then the served result was reordered.** Even with tool recording in place,
every searching task still missed — with every *value* in the served result
correct. `Cassettes.save` writes `sort_keys=True`, and the prompt is built from
`json.dumps(tool_result)`, so replay produced `{answer, configured, count,
provider, query}` where the recording had `{query, answer, ...}`. The key changed
on key order alone. Found by dumping the missing prompt rather than reasoning
about it: alphabetical order is not an order any tool returns. Tool results are
stored as exact JSON bytes now, and the regression test compares `json.dumps` of
served against recorded rather than the values, because comparing values passes
on the broken version — which is exactly how this survived the first fix.

**Recording turned out to be three separate bugs, each hidden behind the last.**
The transport wrapped `llm.acompletion` and nothing else, so every *tool* call
was live on every replay. Each fix exposed the next:

1. **`web_search`** — an agent's first turn searches, the results land verbatim
   in the second turn's prompt, the web moves, the turn misses its own cassette.
   Observed as a suite that replayed clean at 04:55 and went red at 05:08 with
   nothing in the repo changed.
2. **The served result was reordered.** `Cassettes.save` writes `sort_keys=True`
   and the prompt is built from `json.dumps(tool_result)`, so replay produced an
   alphabetised dict where the recording had insertion order — the key changed
   on key order alone while every value matched. Stored as exact JSON bytes now.
3. **`list_files` and `read_file` read the repository.** A replayed prompt was
   found carrying `.git` byte sizes; another carried the whole of `TODO.md`,
   a file being edited while the suite ran.

So the rule is inverted from the obvious one. **Every tool that observes is
recorded; only `write_file` and `record_decision` really run**, because their
side effect is the mission's output — serving `write_file` a recorded "ok"
leaves the workspace empty and scores the run as having produced nothing.

**And one source that is not a tool.** Two parallel steps each record an episode
when they finish, so whether the second recalls the first one's depends on
completion order, and a replay orders them differently. `memory.render_hints`
pastes that block into the system prompt. Normalised out of the key like the
date line — contents only, not presence, since an agent that remembered nothing
was asked a different question.

**Where it stands: all 14 tasks recorded, on `openai/gpt-5-mini`.** `--suite all
--replay` runs the whole suite offline with no errors -- 99% structural, 95%
quality, $0.9983 of recorded spend, 21.7s median. Nothing falls to the simulator
any more, so no row is unjudged for want of a recording.

The three that had never been recorded were the last gap and are now real:
`adversarial_missing_tool`, `approach_standup` and `battery_team_review`. The
last of those used to complete 1 of 4 steps; it completes all four and scores
100% on both columns.

Both open questions from the flash-lite sweep close with it. The recall
normalisation is confirmed end to end, because every cassette in the suite was
recorded after it. `ev_charging_standards` replays clean at 100%/100%, so its
old misses were the partial recording rather than anything in the harness.

**The free models are gone**, which is what ended the sweep that was running
against them. `openai/minimax-m3-free` and the `openai/gemini-3.5-flash-lite`
fallback both answer `model_requires_purchase: this model's serving lanes
require a credit purchase; the $1 card verification is not enough`. That sweep
is why three tasks briefly carried *fallback*-model cassettes which
`_check_model` then refused to replay under the primary -- the guard working,
not a bug. Re-recording is against a paid model or not at all:

```bash
DEFAULT_MODEL=openai/gpt-5-mini FALLBACK_MODEL=openai/gpt-5-nano \
  python -m evals.runner --suite all --model openai/gpt-5-mini --record
```

**What the recorded suite still shows failing**, none of it introduced by the
sweep and none of it edited away:

- `adversarial_ambiguous` -- 86% structural, and the suite's only `pass@k` miss.
- `adversarial_trivial` -- 50% quality.
- `code_review_fix` -- 83% quality.
- `grid_storage_tradeoffs` -- unjudged, "reply was not JSON". That is the
  *judge's* reply, not the mission's: the task scores 100% structural and wrote
  its artifacts, so it is the judge call that needs looking at, not the run.

One recording-mode caveat worth keeping. `max_duration_sec` is measured on wall
clock, so a task can fail it while recording -- 231s and 278s against 150s and
240s budgets on the two new ones -- and pass comfortably on replay. The replay
scorecard is the one that counts.

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

