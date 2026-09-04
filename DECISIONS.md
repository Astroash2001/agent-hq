# Decisions

Choices already made and the reasoning behind them, so they don't get re-litigated.
Several of these look wrong at first glance and are deliberate.

Context: [[README]] · structure in [[ARCHITECTURE]] · versions in [[STACK]] ·
open items in [[TODO]].

---

## Backend

### An event bus sits between the agents and the socket

`orchestrator`, `base_agent` and `tool_registry` never touch a WebSocket. They
`event_bus.emit(...)`; `main.py` registers itself with `subscribe_all` and
broadcasts. Adding a second consumer — a log file, a recorder, a second UI — is one
`subscribe` call and touches nothing else.

The bus also keeps a 200-event rolling history, which is what makes reconnects
work: a browser that drops gets the last 30 events replayed and is immediately back
in sync rather than staring at an empty office.

### The LLM is optional, and there is a real simulator behind it

`base_agent._call_llm` tries LiteLLM only when an API key is present, with a 12s
timeout, and **falls through to `_simulate_specialist_action` on any exception**.

The simulator is not a stub — it inspects the agent id and task text and returns a
plausible tool call on the first pass, then a formatted result on the second, so
the full ReAct loop, tool execution, event stream and UI animation all exercise
identically. This means the office demos reliably with no key, no network and no
spend, and a provider outage degrades to a working demo instead of a dead screen.

The cost is that token counts on the simulator path are **estimates**, not real
usage. Tracked in [[TODO]].

### LiteLLM rather than a provider SDK

One call shape for Gemini, OpenAI and Anthropic. Switching models is a string in
`config.py`. `drop_params = True` absorbs the parameter differences between
providers rather than branching on them.

### Planning is an LLM call, with the keyword buckets kept as the floor

`planner.py` asks the model for a DAG and validates it hard. The four intent
buckets it replaced are still there as `heuristic_plan`, and they are still what
runs with no key, on a provider outage, or when the model returns something that
will not validate twice in a row.

Keeping both is the same bet `base_agent` already made: the interesting path is
the model, but the demo must never depend on it. Planning stays instant, free and
deterministic whenever it has to be.

### The office is a constraint, not a view

Tools are bound to stations and agents walk to them (`office.py`). This is the
one decision that looks like decoration and is not: a station holds one agent, so
two specialists that need the same tool serialise, and moving a station in the
map genuinely changes how long a mission takes.

The alternative — keeping tools callable from anywhere and animating the walk
afterwards — was rejected because it makes the floor plan cosmetic, which is what
every other project with a rendered agent office does. `spatial_enabled` turns it
off if the cost ever outweighs the point.

### Memory similarity is lexical, on purpose

`memory.py` scores with IDF-weighted token overlap rather than embeddings. It is
worse at recognising paraphrase, and it needs no model, no key, no extra
dependency and no network — which keeps the property that the whole system runs
with nothing installed. `_similarity` is the single function to replace when that
trade stops being worth it.

### Rerouting is announced, never silent

When memory overrides the planner's choice of specialist it emits
`route.changed` with both scores and a reason. An office that quietly reassigns
work is impossible to reason about; the whole point of the visualisation is that
you can see why something happened.

### Agent manifests carry presentation, not just prompts

`AgentManifest` holds `system_prompt` and `tools` alongside `name`, `plate_verb`,
`plate_icon` and `color`. One file defines both what an agent *is* and how it
appears in the office, so adding an agent doesn't mean editing two systems that can
drift apart.

### The backend serves the frontend

`main.py` mounts `frontend/` at `/`. Same origin means the WebSocket URL is derived
from `window.location` and there is no CORS or port configuration to get wrong.
The permissive CORS middleware is a dev convenience and should be tightened before
any deployment — [[TODO]].

---

## Frontend

### No framework and no build step

The UI is one canvas and a sidebar of DOM elements updated by direct manipulation.
A framework would add a build, a dependency tree and a virtual DOM to manage
roughly twenty elements, while the actual complexity — the renderer — lives in
canvas code a framework doesn't help with. Editing a file and refreshing is the
whole loop.

### The office floor plan is data, not code

`office_map.js` exports one `OFFICE_MAP` object. The renderer knows how to draw
*any* map; it knows nothing about this particular office. Moving a desk is editing
a number, and `check_layout.py` and `preview_map.py` both read the same file, so
validation and headless preview can never drift from what the browser draws.

### Labels render in screen space, everything else in world space

Plates and speech bubbles are drawn after the world transform is popped. Drawing
them inside it multiplied a 7px font by the zoom — blurry and far too small.
Positions are still world-derived; only the glyphs escape the scaling.

### Lighting is baked into two maps, not evaluated per frame

A *light map* (ambient plus additive radial pools) is composited with `multiply`;
a *glow map* (fixture cones, lamp halos) is composited with `lighter` **after**, so
emitted light sits on top of the shadow instead of being darkened with it.

Both are built once — sixteen radial gradients per frame cost more than the rest of
the renderer combined, and the lights never move. Image smoothing is re-enabled for
these two draws only, because nearest-neighbour upscaling of a gradient bands
visibly.

### The lighting values were solved, not eyeballed

The reference's floor runs `(56,25,10)` unlit to `(134,78,41)` lit. Our floor
paints at `(161,91,62)`, so the required multipliers are `(0.35,0.27,0.16)` and
`(0.83,0.86,0.66)` — a light map running `rgb(89,70,41)` to `rgb(212,219,168)`.
`LIGHT_G` exceeds `LIGHT_R` because the ambient is already red-heavy: reaching a
neutral-warm peak takes proportionally more green and blue.

### Desks are barely tinted, and that is the second attempt

The reference's desks are darker than *its* floor. Ours would be darker than a
floor that is already dark. Tinting to `0.58` and again to `0.72` both landed the
desk surface on the floor's own value and made the desks disappear.

Matching the reference properly needs the floor lightened to about `(198,126,84)`.
That was built and compared side by side: the lighter floor loses the warm dimness
the whole scene depends on, and the dark desks then vanish into the unlit corners
instead. Light desks on a dark floor keep both contrast and mood.

### The floor is synthesised, not from the pack

Every 48×48 block in `Room_Builder_Floors` was scored against the reference's
measured `(168,104,62)`, requiring warm hue and horizontal banding. The best match
is at `(128,192)` — but it is markedly more saturated and busier than the
reference's quiet planks. Measured, the synthesised block wins. The pack floors are
deliberately unused.

### Doors are generated from the wall they sit in

The pack's sliding-glass door was 32px deep with warm tan panels against a 10px
run of pale glazing — mismatched on both depth and material, and cropping fixes one
while worsening the other.

So door leaves are cut from the wall sprite itself at atlas build time. Same
source, same palette, same depth: it matches by construction, and if the wall
changes the door follows automatically. Each leaf gets a frame outline and a pull
handle, because cut straight from the wall a closed door was pixel-identical to it
— correct for glass, useless for a user.

### The atlas and its manifest share one cache-busting token

The browser caches them independently. After a rebuild it will happily pair a fresh
PNG with a stale JSON, and every frame then reads from the wrong rectangle — the
office renders as scrambled furniture. One shared `?v=` per load keeps the pair
consistent.

---

## Asset pipeline

### Sprites are picked by index, never by pixel rectangle

`index_pack.py` segments a sheet (or reads a pre-cropped Singles folder), writes a
JSON index of exact bounding boxes, and renders a numbered contact sheet. A pick in
`build_atlas.py` is a `(sheet, index)` pair; the box comes from the index.

Hand-reading coordinates off a 256×1248 sheet is how the atlas previously ended up
with clipped sofas and bookshelves missing their top shelf.

**Exception:** the wall and border sheets are one continuous atlas with no
transparent gutters — component segmentation fuses them into a single blob — so
those few tiles are cut by explicit coordinate, with the reasoning noted inline.

### The office-pack singles are trimmed on load

All 339 are padded into a uniform 32×48 frame. Untrimmed, every prop would sit up
to 40px above its own baseline. `pack_source.trim()` crops it, and `index_pack.py`
uses the *same* function so the contact sheets report the size the atlas will
actually get.

### LimeZu became the style anchor, replacing Pixel Life

The original `build_atlas.py` header explicitly rejected LimeZu as too soft-shaded
for a Pixel Life base. The reference the office is now built against *is* LimeZu's
style, so the anchor flipped. Older packs now fill only what LimeZu lacks — and
Modern Interiors is a *home* pack with no office equipment at all, which is why
Modern Office was a separate purchase.

### Some props are authored because no pack has them

Planter troughs, the 208px boardroom table, and the conference rug. Both packs top
out around 80px for a table; the largest rug anywhere is 56×58. Tiling a short one
leaves a seam every five tiles, and a row of single pots does not read like a hedge
trough. These are drawn in `gen_tiles.py` with deterministic per-pixel jitter so
they never read as a repeating pattern.

### Layout is validated geometrically, and the validator knows the exceptions

`check_layout.py` computes real pixel rectangles from the atlas and gates a
rebuild. It carries deliberate exemptions rather than false positives: wall-mounted
items share floor space with what stands below them; partition runs *are* walls, so
a room's side wall meeting the building's south wall is the correct join; and a
chair drawn up to a table's long edge is furniture, not a collision — a boardroom
seats seven a side and every one of them overlaps the slab.

### Verification is headless screenshots, not eyeballing the browser

`preview_map.py` renders without a browser; Playwright drives real Chromium for the
full page. Both are scriptable, so a change can be rendered, measured against the
reference numerically, and compared side by side in one command.

---

## Telemetry

### A pricing table in the repo, not a pricing API

`pricing.py` hardcodes per-1M-token rates. A live pricing endpoint would mean a
network call on a path that must work with no credentials at all, and a cost
figure that changes under you between two runs of the same eval — which is the
opposite of what a scorecard is for. The table is versioned with the code, so a
number in a committed scorecard can be traced to the rates that produced it.

The cost of that choice is staleness, and it is paid deliberately: a model with no
row is reported as **unpriced** rather than priced at zero. `price()` returning
`0.0` is meaningless on its own, so it is always paired with `is_priced()`, and
`mission.json` names every model it could not price in `unpriced_models`. A total
that admits what it omitted is worth more than a tidy one that hides it.

### NDJSON appended as spans close, then sorted at the end

`trace.jsonl` is written one line per span, as each span finishes. A single JSON
document would be unreadable if the process died mid-mission, which is precisely
the run whose trace you most want. Append order is close order though, and a
reader wants the run as it happened — so `finalise()` rewrites the file in start
order once the mission ends. A killed process leaves the file unsorted and fully
parseable; a finished one leaves it sorted.

Ordering is `(t_start, seq)`, not `t_start` alone. `time.time()` is about 15ms
granular on Windows, so the steps of a parallel wave are routinely stamped
identically and a plain sort silently fell back to close order — observed with a
two-step fan-out written shortest-first. `seq` is a process-monotonic counter
stamped when the span opens, and it ships in the file so a replay reader can
reproduce the same order.

### Span parentage in a ContextVar, not in every signature

The alternative was threading a span or trace id through `handle_user_input`,
`_run_step`, `BaseAgent.run`, `_call_llm`, `tool_registry.execute` and back. A
`ContextVar` gets the same tree for free, because asyncio copies the context when
it creates a task: a step span set inside `_run_step` is inherited by every turn
and tool call that step makes, and the steps `asyncio.gather` runs in one wave get
independent copies, so they cannot become each other's children.

It also means tracing is genuinely optional. With `tracing_enabled` off, `span()`
still yields a `Span` object that call sites can set attributes on — it is simply
never recorded — so not one call site needs an `if`.

### The LLM call returns what it cost, not just what it said

`llm.acompletion` returns an `LLMResult`, with the provider response on
`.response`. It previously returned the raw response and discarded everything the
retry loop knew: which model in the fallback chain actually answered, how many
attempts it took, and what the tokens were worth. The callers then invented the
missing numbers — `base_agent` defaulted to 100/50 tokens when a response carried
no usage block, and priced every model in the roster at two hardcoded Gemini flash
constants, so a mission run on Claude reported a Gemini bill.

An out-parameter (`usage_out={}`) would have avoided changing any call site, and
was rejected: a future call site can forget to pass it and silently stop being
counted. A return value cannot be forgotten.

---

## Artifact quality

### Two checkers, not one: a regex layer and a model layer

`contracts.py` is pure and free, so it runs on every artifact and the decision to
run it is never a decision. It answers "is this the right shape": is it empty, is
it the known stub, is it far shorter than a deliverable of this kind should be,
does it have the structure it promised. `critic.py` answers the question regexes
cannot -- "does this actually do what the step was asked to do" -- and costs a
model call, so it runs second and only on artifacts that already passed the cheap
layer. Paying for a judgement on an artifact that is visibly a stub is waste.

The split also means the offline path keeps a real check. Contracts run with no
credential; the critic does not.

### The critic never judges the simulator

With no key configured there is no judge. The built-in simulator would be grading
its own output through the same fallback that wrote it, and a verdict produced
that way looks like a measurement while being nothing of the kind -- which is the
exact failure the telemetry work was undertaken to stop.

For the same reason a *simulated* step is checked but never re-dispatched. The
simulator returns the same canned reply to the same input, so a repair spends the
budget to receive a byte-identical artifact. The verdict is still emitted, so the
office shows the work was judged and found wanting; only the pointless retry is
suppressed.

### One shared repair budget, and a rejected step still completes

`max_repair_attempts` (default 1) is the number of EXTRA dispatches a step may
have, and contract failures and critic revisions draw on the same allowance -- a
step cannot be repaired twice by taking one from each. An unbounded loop is how
an agent system spends a whole mission on one step.

When the budget runs out the step **completes** rather than failing. A step that
failed would strand everything downstream of it in the DAG over what is usually a
quality problem, not a broken one. The rejection is instead carried in the
`step.critiqued` event as `repairing: false`, and the UI is told to render that
case loudly, because a capped repair drawn like an accept is a lie.

### A skipped judgement is never a rejection

Every degenerate path in `critic.py` -- disabled, no key, empty artifact, network
failure, unparseable reply, a verdict outside the two allowed values -- resolves
to `accept` with `skipped=True`. The repair loop costs a real dispatch, and
spending it because a judge misparsed its own JSON is worse than letting ordinary
work through. `skipped` is kept separate from `verdict` so a caller can tell "was
judged and passed" from "was never judged".

### Quality is Optional[float], and None means unmeasured

`Episode.quality` defaults to `None`, not to 1.0 or 0.5. Every episode in
`data/memory.json` predates the field and must still load; and a run with no
critic genuinely did not measure this. A numeric default would record "perfect"
for both cases, and the quality-weighted routing that reads this later would be
weighting invented data.

---

## Evals

### Cassettes over live eval calls

A suite that needs a key cannot run in CI, and one that calls a provider gives a
different answer every run -- so it measures the provider's mood as much as the
code. Model exchanges are recorded once and committed, and a scored run replays
them. `replay.py` monkeypatches the `llm.acompletion` symbol rather than editing
`llm.py`, because the harness must not know the eval layer exists.

A cassette miss in replay mode is a **hard error naming the key**, never a
fall-through to the network. A silent fall-through is exactly how a deterministic
suite turns into a flaky one that nobody trusts and everybody re-runs.

### The cassette key ignores today's date

`config.date_context()` puts `Today's date is <weekday, dd Month yyyy>.` at the
top of every agent system prompt and the planner's. That is deliberate and
correct. It also means the request bytes change at every midnight, so a cassette
recorded yesterday would miss today and the entire suite would go red overnight
for a change that has nothing to do with the code. The date is normalised out of
the hash rather than frozen in the harness -- the hash is the eval layer's
concern, and the harness is not.

### A missing cassette degrades to the simulator, and says so

`--replay` on a fresh clone with no recordings would be a wall of misses. Instead
the runner falls back to the built-in simulator and labels the row `simulate`,
with the model column reading `(simulator)` rather than naming a model that was
never called. Such a row still carries a real structural score -- planning,
scheduling, the workspace, the spatial runtime and the contract layer all ran --
but its quality column is `—` and the scorecard states in prose that this is not
a passing quality score. The alternative, letting an offline run print a number
in the quality column, would be the same class of lie the telemetry work was
undertaken to remove.

### Determinism is verdicts, not measurements

`runner.determinism_signature` compares every check's `ok`, the structural score,
the rubric verdicts, artifacts, steps, cost and span count -- and deliberately
excludes wall-clock duration and each check's `detail` string. The duration check
renders the clock into its own text ("10.9s of 120s"), so comparing whole rows
reported every run as non-deterministic over a number that is supposed to vary.
A signal that cries wolf on every run is one everybody learns to ignore.

### The judge grades artifacts, never the briefing

`_synthesize_results` is string assembly around a template. Grading its output
grades the template -- and it would score well on a mission where every
specialist produced nothing, because the template renders identically either way.
`judge.py` reads the artifact files and skips `report.md` outright.

Each rubric line is scored independently as pass/partial/fail with a reason,
rather than as one overall mark. A single "out of ten" is unattributable: you
cannot tell which requirement it docked points for, so you cannot fix anything
from it. A criterion the judge omits counts as **fail**, so a truncated reply
cannot raise its own score by leaving out what it could not satisfy.

---

## Durable execution

### SQLite in the repo, not a service

The run store is one file, written with the stdlib `sqlite3` module. Postgres or
Redis would add a dependency, a container and a connection string to a project
whose whole pitch is that it starts with no credentials at all -- to record a
handful of rows per mission. WAL mode plus one short transaction per write means
a hard kill leaves a readable database rather than a lock file and a half-written
page, which matters here far more than throughput ever will.

Every method is failure-tolerant by design: an unwritable store logs once and
becomes a no-op. Bookkeeping that can take a mission down has turned a durability
feature into a new way to lose one.

### The database is written before the event is emitted

Not after. If the event goes first, a process killed in the gap has shown the
user a completed step the store has no record of -- and the resume, reading the
store, runs that step a second time and overwrites an artifact that was already
correct. The ordering is the whole contract.

For the same reason only `completed` is resumable. A step recorded `running` was
in flight when the process died: its outcome is unknown and its artifact may be
half written, so re-running it is the only safe reading of that state.

### A resume re-uses the stored plan and never re-plans

Re-planning would be a different mission wearing the same id. The artifacts on
disk were produced against the original graph, and a freshly planned one would
`consumes` files that no step in it produces. Memory routing is skipped on a
resume for the same reason -- a step that has already run cannot change hands
now, and letting the router move a still-pending step mid-mission would silently
change the graph the completed work was built for.

This is why the plan is written to the store the moment planning finishes rather
than at the end with everything else.

### The workspace is rehydrated from the artifacts on disk

`MissionWorkspace` is in-memory and dies with the process, but its mirror in
`data/missions/<id>/` does not. That mirror was built so a finished mission would
outlive the browser tab; it turns out to be what makes a resume possible at all.
Without it a resumed step `consumes` an artifact the store has never heard of,
receives an empty context, and redoes upstream work that is sitting on disk
beside it. `report.md`, `mission.json` and `trace.jsonl` are skipped -- they are
records *of* the run, not artifacts *from* it.

### A semaphore, not a slice

`max_parallel_agents` was applied as `ready[:limit]`, which made it a batch size:
a wave of five ran three, waited for all three to finish, then ran two, leaving
slots idle whenever one step finished sooner than its neighbours. It is now an
`asyncio.Semaphore` held around each `_run_step`, with the whole ready set
dispatched, and the scheduler re-evaluates on every completion rather than
draining a wave -- so a step starts the moment either a slot frees or its
dependencies land.

`_peak_active_steps` records the high-water mark, because it is the only honest
way to assert the cap works. A counter read at rest reads zero however broken the
scheduler is, and the old slice never exceeded the cap either -- the entire
difference between the two is *when* the fourth step starts.

### A step deadline is terminal, and is not called a timeout

`step_timeout_sec` (300s) is enforced with `asyncio.wait_for`, and the slot is
taken *before* the clock starts -- a step queued behind the concurrency cap has
not begun work, and timing it out for waiting its turn would punish it for the
scheduler's decision rather than its own.

Exceeding it is **not** retried. The step has already spent the entire time
budget, and the usual cause -- a model that will not stop -- recurs on the next
attempt at the same price. The exception is called `StepDeadlineExceeded` rather
than anything containing "Timeout", because `llm.is_transient` reads that
substring in a class name as retryable: a name that quietly re-enables the retry
it was meant to prevent is a trap for whoever edits this next.

Genuine transient failures do get `step_max_attempts` (2), classified by
`llm.is_transient` rather than a second classifier -- one place decides what
"worth trying again" means, or the two drift and a 401 starts being retried in
one of them. A step that failed because the agent produced garbage is
`contracts.py` and `critic.py`'s problem, not a retry candidate: retrying it just
buys the same garbage twice.

### Four endpoints in their own router

`api_missions.py` rather than four more handlers in `main.py`. It keeps the
durable-execution surface in one place, and `main.py` -- which carries every
other endpoint inline -- takes a two-line include instead of a large hunk in the
file most likely to be edited by something else at the same time.
