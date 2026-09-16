# AgentHQ

**A multi-agent LLM harness you can watch, price, resume and replay — with a live
2D pixel-art office as its debugger.**

Dispatch a goal and the boss plans it into a DAG. Specialists run the independent
steps at the same time, walk across the office to the station each tool lives at,
queue when one is busy, hand each other real files, argue at the conference table,
and get their work checked before anything downstream reads it. Every mission
leaves a trace you can replay through the same office, step by step.

![Four agents are called to the conference room, walk across the office, and
argue at the table while the execution graph fills in and the cost ticks
up](docs/hero.gif)

*Twenty-five seconds of a real mission: the boss calls a standup, three
specialists walk to the conference room, and the DAG, token gauge and cost
panel move as they work. Nothing here is a mock-up.*

The office is not decoration. It **is** the observability layer: a queue at the
ops console is a real `asyncio` queue, and moving a station in the map changes how
long missions take.

> Docs map: [[ARCHITECTURE]] · [[STACK]] · [[DECISIONS]] · [[PROTOCOL]] · [[TODO]]

---

## What this is for

**A reference implementation you can watch.** Multi-agent orchestration is
normally a wall of logs; here it is a floor plan. That makes it useful in three
ways, in the order people actually use it:

- **To understand how agent systems work.** The plan, the fan-out, the queue at a
  busy tool, the argument at the table -- each is a real mechanism, drawn.
- **To lift patterns from.** `pricing.py` refuses to guess a rate rather than
  quietly report $0.00. `runstore.py` writes a transition before emitting its
  event, so a killed process cannot have shown you a step it never recorded.
  `evals/replay.py` records tool calls as well as model calls, because a live
  `web_search` in turn one changes turn two's prompt. The reasoning is in the
  files, not just the result.

**What it is not:** a production framework. LangGraph, CrewAI and AutoGen are
libraries you build products on. This is one opinionated harness with a face,
and it is honest about that.

Full rationale for each decision, including the ones that did not work, is in
[[DECISIONS]].

---

## What a mission leaves behind

**One file.** A mission used to scatter `extraction.md`, `guardrails.md`,
`report.md` and a manifest across a directory, and the answer to the question
asked was in whichever of them you opened last. Now every step's output is
folded into a single `report.md` and the rest are discarded
(`single_file_output`, on by default). Its shape is fixed:

```
You asked → ✅ The answer → outcome → files → 📄 the deliverable, in full
→ ▸ Everything behind it: 📎 each other artifact, the graph, handoffs, run record
```

The answer is first because that is what is read; the evidence is folded into a
`<details>` block because it is what is checked.

**Three artifacts on disk:** `report.md`, `mission.json`, `trace.jsonl`. The
download button zips all three (`GET /api/missions/{id}/bundle`). The PDF
button prints the report through the browser — real selectable text, not a
screenshot of one, which is what the previous html2canvas export produced.

**An outcome you can trust.** A mission that finished with a rejected step is
`unverified`, not `completed`, and the step is named. SQL an agent printed but
never executed is recorded as `provenance.unexecuted_sql`. A figure the
conclusion asserts that appears in no artifact is flagged. An artifact that ends
mid-sentence is rejected rather than filed.

## Grading a run without reading it

A report is written to look finished, so judging one by reading it finds a
different problem every time. `evals/mission_check.py` reads the evidence
underneath instead — `mission.json`, `trace.jsonl`, the artifacts — and applies
fixed rules, so the same run gets the same verdict whoever asks:

```bash
python -m evals.mission_check --latest 3
python -m evals.mission_check --base-url https://your-app.onrender.com --latest 1
python -m evals.mission_check --latest 1 --spec evals/fixtures/scouting_rules.json
```

Generic rules cover outcome, verdicts, artifact attribution, tool use, sandbox
refusals, SQL provenance, grounding and budget. A `--spec` adds rules for one
known task: figures that must match an answer key, text that must not appear,
tools a step must have called successfully. Standard library only, and exit 1
on any FAIL, so CI or a script can gate on it.

## Running it hosted

`render.yaml` and the `Dockerfile` deploy it as-is. Two settings decide who may
drive it:

- `AGENTHQ_API_TOKEN` — a bearer token the frontend prompts for and stores. With
  `ENVIRONMENT=production` and no token set, the server **refuses to start**
  rather than come up open by accident.
- `ALLOW_UNAUTHENTICATED=true` — the deliberate override for a public demo. It
  is a real decision: every visitor's mission spends your provider credit, so
  set a spending cap at the provider before you use it.

`max_concurrent_missions` (1 by default) is what stops a public URL from running
ten missions at once on one free instance. Mission ids are validated and the
resolved path is checked against the missions directory, because `%2e%2e%2f` in
that position once served `memory.json`.

On a free plan there is no disk: missions are wiped on sleep and on every
redeploy. Download the bundle if you want to keep one.

---

## Working on it

### Frontend only (art / layout work)

```bash
cd frontend
python -m http.server 8899
```

The WebSocket fails to connect and `app.js` falls back to a scripted standalone
simulation, so the office animates without a backend. This is the fast loop for
map and rendering changes.

### LLM keys

Optional. Set `GEMINI_API_KEY` (or `OPENAI_API_KEY` / `ANTHROPIC_API_KEY`) in
`backend/.env` or the environment, or point `LLM_API_BASE` at an
OpenAI-compatible gateway. See [[DECISIONS]].

### Tests

```bash
# Run a whole mission with no browser and no API key, and print what happened:
# an annotated timeline off the event bus, then a per-subsystem verdict drawn
# from the events rather than from anything the script assumes.
cd backend && python scripts/watch_mission.py "your goal here"
python scripts/watch_mission.py --twice     # the second run shows memory recall
```

```bash
pip install -r requirements-dev.txt
cd backend && python -m pytest -q      # 616 passed, 2 skipped
```

CI runs the same suite on Python 3.10 and 3.12, `ruff check`, `node --check` on
every frontend file, and the eval smoke suite.

**What the eval step in CI does and does not prove.** It runs two tasks end to
end — planning, scheduling, the workspace, contracts, the run store, the trace
— and fails if either errors. It does **not** score answer quality: the
cassettes are recordings of a paid provider and are not committed, so `--replay`
falls back to the built-in simulator and says `mode=simulate`. Record your own
with `--suite all --record` and a key. `--require-cassettes` turns that
fallback into a refusal, which is what to add to CI once recordings exist. See
[`evals/scorecard.md`](evals/scorecard.md).

There is no JS test runner — adding one would be this project's first node build
step. CI parses every JS file, and two hand-run validators cover the rest:

```bash
node tools/check_replay.js      # a real trace.jsonl through the real replay.js
python tools/check_layout.py    # every prop's pixel rectangle against the map
```

The dev set additionally needs `node` on PATH — `preview_map.py` and
`check_layout.py` read the real `office_map.js` through it rather than keeping a
second copy of the layout that could drift. Rebuilding the atlas also needs the
art source packs, which are not in this repo; the built atlas is committed, so
you only need them to regenerate it. See [[STACK]].

---

## Major components

| Area | Path | What it does |
|---|---|---|
| API + WebSocket | `backend/main.py` | FastAPI app, connection manager, event broadcast, static mount |
| Orchestration | `backend/orchestrator.py` | Plans a goal into a DAG, runs independent steps together, repairs and synthesises |
| Planner | `backend/planner.py` | LLM plan → validated DAG, with keyword buckets as the fallback |
| Mission workspace | `backend/workspace.py` | Shared artifact store; one step's output becomes the next one's context |
| Spatial runtime | `backend/office.py` | Tools bound to stations; agents walk to them and queue when one is busy |
| Episodic memory | `backend/memory.py`, `backend/embeddings.py` | Every finished subtask, recalled semantically; quality-weighted routing |
| Durability | `backend/runstore.py`, `backend/api_missions.py` | SQLite run store, mission history, resume, trace endpoint |
| Quality | `backend/contracts.py`, `backend/critic.py` | Is the artifact usable, and is it any good |
| Conclusion | `backend/synthesis.py` | What the mission found, drawn from the artifacts |
| Meetings | `backend/debate.py` | One bounded objection per attendee before the chair decides |
| Telemetry | `backend/tracing.py`, `backend/pricing.py` | One span per unit of work; real token counts, priced per model |
| Agent engine | `backend/base_agent.py` | ReAct loop: reason → tool call → observe → repeat |
| Event fabric | `backend/event_bus.py` | In-memory async pub/sub, 200-event rolling history |
| Tools | `backend/tool_registry.py`, `backend/tools/` | 11 built-in tools, sandboxed and grant-checked |
| Provenance | `backend/provenance.py` | SQL an agent showed but never ran, and figures with no source |
| API hardening | `backend/api_guard.py` | Optional bearer token, mission-id validation, concurrency cap |
| Grading | `evals/mission_check.py` | Grades a finished mission, local or against a deployed URL |
| Office renderer | `frontend/js/canvas.js` | Tilemap, A* pathing, depth sort, lighting, doors, labels |
| Office data | `frontend/js/office_map.js` | The whole floor plan as data — 40×24 tiles |
| HUD | `frontend/js/hud.js` | Dock, terminal, token gauge, DAG panel, cost panel, mission output |
| Trace replay | `frontend/js/replay.js` | Drives the office from a recorded `trace.jsonl` |
| Asset pipeline | `tools/*.py` | Sprite indexing, synthesis, atlas build, layout validation |

Full detail in [[ARCHITECTURE]].

---

## The agents

| ID | Name | Role | Tools |
|---|---|---|---|
| `orchestrator` | Boss Dave | Planning & synthesis | — |
| `researcher` | Alice | Search, RAG & discovery | web_search, fetch_url, read_file, search_workspace, calculate |
| `coder` | Charlie | Code review & suggested changes | read_file, list_files, search_workspace, write_file, calculate |
| `data_analyst` | Bob | Data & analytics | query_data, calculate, read_file, list_files, search_workspace, describe_image |
| `creative` | Eve | Writing & synthesis | write_file, read_file, web_search, search_workspace, calculate |

`write_file` is granted for the duration of any step that promises an artifact,
so an agent without it can still file a deliverable.

## The tools

| Tool | Risk | What it does |
|---|---|---|
| `web_search` | read | Ranked snippets from the live web (Tavily), or an honest refusal |
| `fetch_url` | execute | One named http/https page or API as text |
| `read_file` / `list_files` | read | Jailed to the mission and the repo |
| `search_workspace` | read | Matching lines across the mission's files, with line numbers |
| `query_data` | read | Read-only SQL over a CSV or JSON file |
| `calculate` | read | Exact arithmetic, AST-sandboxed |
| `describe_image` | read | What is in an image in the mission workspace |
| `write_file` | write | Jailed to the mission workspace |
| `review_artifacts` / `record_decision` | write | Conference table only |

**Nothing runs code.** There is no terminal and no code-execution tool, and
none can be hired: either one gave a model arbitrary execution as the user
running the server, which no file sandbox can contain. The coder reads code
and proposes changes as diffs or revised files, written only into the mission
workspace, for a person to review and apply. It never claims to have run or
timed anything.

**Agents cannot read secrets.** File tools refuse `.env` and other secret
files, the run store and memory, `evals/` (answer keys) and every other
mission's directory. The app itself still loads `backend/.env` as normal.

**Risk** is metadata on the registry entry, not documentation: it is what lets
code that hands tools to an agent know which ones are dangerous without keeping
its own list that drifts. An unknown tool is assumed `execute`.

**A grant is enforced, not assumed.** `tool_registry.execute` refuses a tool
outside the caller's list. Until that check existed, a grant held only because
an agent is *shown* schemas for its own tools — a habit of the model rather
than a property of the system, and one that stops holding the moment a tool
list is chosen at runtime rather than written by hand.

Three of these needed a guardrail shape the repo did not have:

- `fetch_url` is the first outbound request whose destination an LLM chooses.
  `web_search` only ever talks to one hardcoded host. So: scheme allowlist,
  refusal of any name resolving into private address space — checked on every
  redirect hop, not just the URL given — and a byte ceiling. Without it,
  `http://169.254.169.254/` reads the cloud metadata service and
  `http://localhost:8080/` lets an agent drive this server through its own tool.
- `query_data` is read-only by **SQLite's own authorizer**, not by a keyword
  scan. Case, comments and whitespace all defeat a blocklist; the authorizer
  sees parsed operations.
- `calculate` allowlists **AST node types**, so no name, call or attribute can
  be reached at all. Refusing everything that is not literal arithmetic leaves
  nothing to be clever with.

Adding a permanent agent is a new JSON file in `backend/agents/` plus a desk in
`office_map.js` — see [[ARCHITECTURE]].

## Hiring mid-mission

When a step declares tools that **no single agent holds**, the office hires a
contractor for it. They come in through the street door, take one of the two
spare desks in the bullpen, do the step, and are gone when the mission ends.

`planner.regrant_steps` has always been able to see this coming — it moves a
step to whoever holds the tools it needs, and when nobody does it says so and
gives up. `foundry.py` is what to do instead.

Three rules, and the first two are the whole safety argument:

- **A tool must exist.** The catalogue is `tool_registry.catalogue()`, the
  authority on what this office can actually do. Granting a name with no
  function behind it does not create a capability; it creates an agent that
  calls a tool, gets nothing, and writes a confident report around the hole. A
  step needing `query_postgres` is **not** hired for. It fails, naming what is
  missing — which is a request to go and write that tool.
- **Exactly what the step declared.** Not a preset, not everything. That is
  both tighter and more capable than a fixed allowance: nothing is pre-banned,
  and nobody accumulates a tool they are not using. The permanent five hold
  theirs for every mission forever; a contractor holds two for one step.
- **Mission-scoped.** Persisted in the run store so a resume rebuilds them,
  never written to `agents_dir`. A roster that grows by itself is one nobody
  curated, and every future planner prompt would pay to list it.

The model writes the contractor's *identity* — name, speciality, how to work.
It never writes the tool list: that comes from the gap and is not read back
from the reply, so a design naming `terminal` does not get `terminal`. With no
credential the design is deterministic instead, because the no-key path is the
one most likely to be someone's first look at this.

**Two hard caps**, and the smaller wins: `max_spawned_agents` (2), and the two
spare desks in the bullpen. The office refuses rather than seating two
contractors on one tile — an office that lies about who is in it is worse than
one that says it is full.

A contractor is drawn as an existing character hue-rotated and wearing a
visitor badge, and its dock card has a dashed edge. There are five characters
in the atlas and the packs that made them are gigabytes; borrowing one is the
honest option, and the badge is what stops a twenty-minute hire looking like
permanent staff.

---

## The honest gaps

Kept current in [[TODO]], which is the file to read before trusting anything
above. The ones worth knowing up front:

- **Memory matches surface words, not meaning.** "Speed up the cache" and
  "optimise memoisation" score 0 against each other. Real synonymy needs
  `embedding_provider="litellm"`, which costs a call per uncached text.
- **The conclusion's figures are grounded, its claims are not.** Every number it
  asserts is checked against the artifacts and flagged if absent, but
  "sodium-ion is cheaper" grounds against nothing — that needs an entailment
  judgement, which is a different kind of check.
- **The planner's shortcuts are English-only.** A goal that asks for a meeting
  without using a meeting-ish word gets solo desks.
- **108 unused frames** from the source packs are still in the atlas. The 14
  provably dead authored sprites were pruned; these are a palette for layout
  work, and nothing independently confirms they are dead rather than unplaced.
- **A replay cannot seek backwards.** Dragging the scrubber left replays from
  zero, because the office is a state machine with no inverse for "the agent
  walked to the library".
- **The eval suite in CI is structural, not graded.** No cassettes are
  committed, so `--replay` runs the simulator. See the note above and
  [`evals/scorecard.md`](evals/scorecard.md).
- **The budget can be overspent by one turn.** It is checked before each of an
  agent's turns, so a step that crosses the line mid-turn finishes that turn
  and is then given one closing turn with `write_file` only. The cap is a
  ceiling with a known overshoot, not a hard stop.
- **Provenance is not truth.** The harness can prove which query ran, which
  page was opened and which figure came from which file. Whether the page was
  right about the world is outside what any of this checks.

---

## License

MIT for the harness and server code — see [`LICENSE`](LICENSE). The pixel-art
source packs are third-party licensed and are **not** in this repository; the
built atlas is committed so a clone runs without them. See [[STACK]].
