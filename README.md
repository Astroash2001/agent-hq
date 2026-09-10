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
- **To delegate real multi-step work to**, over MCP -- see *Integrating* below.

**What it is not:** a production framework. LangGraph, CrewAI and AutoGen are
libraries you build products on. This is one opinionated harness with a face,
and it is honest about that.

---

## Integrating

**As a tool for your own agent.** `mcp_server.py` serves this office over stdio,
so Claude Code or any MCP client can hand it a job:

```json
{ "mcpServers": {
    "agenthq": { "command": "python", "args": ["-m", "mcp_server"], "cwd": "backend" }
} }
```

It exposes `run_mission(goal)`, `get_mission(id)` and `list_agents()`. Your agent
delegates a multi-step research or code task and gets back a report, the
artifacts, and what it cost.

**From Python.**

```python
from orchestrator import orchestrator
result = await orchestrator.handle_user_input("Compare X and Y, write a report")
```

**Over HTTP.** `GET /api/missions` lists past runs, `/{id}` returns one with its
plan and step statuses, `/{id}/artifacts/{name}` returns a file's text,
`/{id}/trace` returns the NDJSON span log, and `POST /{id}/resume` picks up a
mission a killed process left unfinished. Contract in [[PROTOCOL]].

---

## How a mission runs

```mermaid
flowchart TD
    U([Your goal]) --> P[planner.py<br/>LLM → validated DAG]
    P --> M{{memory.py<br/>reroute on record?}}
    M --> S[orchestrator.py<br/>semaphore-bounded scheduler]

    S --> A1[Specialist step<br/>ReAct loop]
    S --> A2[Specialist step<br/>ReAct loop]
    S --> MT[Meeting step<br/>attendees object, chair decides]

    A1 --> O[office.py<br/>walk to station, hold it]
    A2 --> O
    O --> T[tool_registry.py<br/>6 built-in + any MCP server]

    A1 --> W[(workspace.py<br/>mission artifacts)]
    A2 --> W
    MT --> W
    W --> C[contracts.py → critic.py<br/>shape, then judgement]
    C -->|revise, budget left| S
    C -->|accept| R[report.md + mission.json]

    S -.every transition.-> DB[(runstore.py<br/>SQLite — resumable)]
    S -.every span.-> TR[(trace.jsonl<br/>replayable)]
    S -.every event.-> WS{{/ws → the office}}
```

Each of those boxes is one module, and each one is switchable off in
`backend/config.py` — turning a subsystem off returns the harness to how it
behaved before that subsystem existed.

---

## Run it in one command

```bash
docker compose up          # → http://localhost:8080
```

No key required. Without a credential every agent falls back to a deterministic
simulator that produces realistic tool calls and outputs, so a fresh clone runs a
complete mission end to end — and every number that came from the simulator is
tagged `~estimated` rather than presented as a reading.

Or without Docker:

```bash
pip install -r requirements.txt
cd backend && python main.py        # uvicorn on http://localhost:8080
```

`main.py` mounts `frontend/` as static files, so the UI is served from the same
origin and the WebSocket at `/ws` connects with no CORS or port juggling.

---

## Is it any good? — the eval harness

`evals/` holds 14 golden tasks across four shapes (research, code, meetings,
adversarial), a cassette record/replay transport that makes a scored run
reproducible, and an LLM judge that scores each rubric line against the **artifact
files** — never the synthesised briefing. The briefing's execution graph and
handoff list are bookkeeping and would score identically on a mission where every
specialist produced nothing; grading them measures the assembler, not the work.

```bash
python -m evals.runner --suite all --replay      # offline, no key, deterministic
```

| task | mode | structural | quality | cost |
|---|---|---|---|---|
| `adversarial_ambiguous` | replay | 83% | 100% | $0.0702 |
| `adversarial_missing_tool` | replay | 100% | 100% | $0.0314 |
| `adversarial_trivial` | replay | 100% | 50% | $0.0175 |
| `approach_standup` | replay | 100% | 100% | $0.0508 |
| `battery_comparison` | replay | 100% | 100% | $0.1146 |
| `battery_team_review` | replay | 100% | 100% | $0.1438 |
| `code_cache_bug` | replay | 100% | 100% | $0.0721 |
| `code_review_fix` | replay | 100% | 83% | $0.0283 |
| `ev_charging_standards` | replay | 100% | 100% | $0.0564 |
| `grid_storage_tradeoffs` | replay | 100% | — | $0.1115 |
| `heat_pump_handoff` | replay | 100% | 100% | $0.0681 |
| `india_news_brief` | replay | 100% | 100% | $0.0301 |
| `solar_efficiency` | replay | 100% | 100% | $0.1064 |
| `solid_state_batteries` | replay | 100% | 100% | $0.0969 |
| **all 14 on gpt-5-mini** | **replay** | **99%** | **95%** | **$0.9983** |

**structural** is the `expect` block — step count, agents involved, artifacts
produced — deterministic, no model involved. **quality** is `evals/judge.py`
scoring each rubric line against the artifact files. Every one of the fourteen
replays from committed cassettes recorded against `openai/gpt-5-mini`, so no row
falls to the simulator and no row borrows a score. `grid_storage_tradeoffs`
shows `—` because the judge's reply on that task was not JSON: an unreadable
grade is reported as no grade rather than a zero, which would blame the run for
the judge's bad day.

**The replay needs nothing from your environment.** Each task's cassettes carry
the models they were recorded under, in `_recording.json` beside them, and the
run resolves both the default and the fallback from there. That matters because
`cassette_key` hashes the model name a call asked for: the critic, the synthesis
pass and the judge all name `fallback_model`, so a machine configured
differently used to miss every one of those keys and quietly turn a scored suite
into an unjudged one. Two consecutive full replays under a deliberately wrong
`DEFAULT_MODEL` now come back byte-identical on all 14 verdicts —
`--compare` prints `DETERMINISTIC`.

**Recording is what makes those rows worth reading, and it is unflattering.** On
the simulator all fourteen read 100%. Against a real model `adversarial_trivial`
scores **50%** on quality, `code_review_fix` **83%**, and `adversarial_ambiguous`
drops to **83%** structural — the suite's only `pass@k` miss. None of that has
been tuned away: a suite you edit to match the model measures the edit.

Getting there took three recording sweeps, because two of them proved the
cassettes were not reproducible at all:

- **Tool calls were live on every replay.** The transport wrapped
  `llm.acompletion` and nothing else. An agent's first turn searches the web and
  the results land verbatim in the *second* turn's prompt — so when the web moved,
  that prompt changed and the turn missed its own cassette. A suite that replayed
  clean at 04:55 went red at 05:08 with nothing in the repo having changed.
  `tool_registry.execute` is wrapped now, for tools that reach outside the office
  only; `write_file` must really run, because its side effect is what gets scored.
- **Then the served result was reordered.** Cassettes are written with
  `sort_keys=True`, and the prompt is built from `json.dumps(tool_result)` — so
  replay produced an alphabetised dict where the recording had insertion order,
  and the key changed on key order alone while every value matched. Stored as
  exact JSON bytes now.
- **And a cassette could be served for the wrong model.** `base_agent` calls
  `acompletion` with no model argument, so every agent turn hashed `""` and the
  key never distinguished models. CI's smoke step was replaying flash-lite
  recordings under flash with 16 hits and 0 misses, printing the wrong name in
  the model column. A call that *names* a model is refused on a mismatch now,
  and an unnamed one is labelled with the model the cassette says answered —
  refusing it instead was a fourth fault, and the loudest: it compared against
  `DEFAULT_MODEL`, which in replay names a provider the run never reaches, so
  the documented offline command failed on all 14 tasks on any machine whose
  env had moved on. That is what `_recording.json` fixes.

**Re-recording now costs money.** The free lanes this project had been using —
`openai/minimax-m3-free` and the `gemini-3.5-flash-lite` fallback — both answer
`model_requires_purchase` since September 2026, so a sweep names a paid model
explicitly:

```bash
DEFAULT_MODEL=openai/gpt-5-mini FALLBACK_MODEL=openai/gpt-5-nano \
  python -m evals.runner --suite all --model openai/gpt-5-mini --record
```

The cost column is real USD from `mission.json`, priced by `backend/pricing.py`
at the model that actually answered. It reads $0.0000 for any model with no row
there, because `pricing.py` refuses to guess — the suite then marks the budget
check unscored rather than passing it, and the server warns at startup.

A task's **latency** budget is unscored on replay for the same reason: served
from disk, what is left on the clock is the machine, not the provider. Scoring
it was the last thing making the offline suite non-deterministic — the same
cassettes passed `battery_comparison`'s duration check on one run and failed it
on another. `--live` and `--record` still enforce it, where the number means
something.

Those rows were not always green, and the way they went green is the point of
having them. Five failed on the first scorecard: the fallback planner answered
"What does SSD stand for?" with a four-step fan-out, never produced a meeting
step for a goal that asked for a standup, and ended every mission in a file
called `briefing.md` whatever had been asked for. The suite is what made each
of those visible, and `evals/scorecard.md` still carries the **fail → pass**
line for all five.

The honest caveat: those fixes are keyword heuristics in `heuristic_plan`,
written against tasks the suite already contained. They are defensible on their
own terms — do not hire four specialists to answer a definition — but a suite
you fix code against is a suite you can over-fit to, and this one is small.

The live scorecard, with a delta against the previous run, is
[`evals/scorecard.md`](evals/scorecard.md).

---

## The office, as an instrument

The right-hand HUD is not a status page. Four panels read the same event stream
the renderer does (contract in [[PROTOCOL]]):

- **Execution plan** — the DAG as a real graph, ranked by dependency depth so a
  fan-out reads as a fan-out. A node's fill is the agent that owns it, its ring is
  how far it got, and it carries what it cost. A `⇄` badge means memory took the
  step off the planner's pick; a scored badge means a reviewer judged it, in red
  when it was rejected and completed anyway because the repair budget was spent.
- **Mission cost** — live cost, tokens, wall clock and a per-step latency strip.
  Every figure is per-mission, and marked `~est` when it came from the simulator.
- **Trace replay** — pick any past mission and drive the office from its
  `trace.jsonl` instead of the live socket: play, pause, scrub, 1×–8×. The
  renderer is not forked; recorded spans are turned back into the same events the
  WebSocket sends. **This makes the office a time-travel debugger for multi-agent
  runs** — every past run is watchable again, at the speed you choose.
- **Mission output** — the goal, the finished briefing, and every artifact with
  its real body.

---

## What is actually interesting here

| Decision | Why | Where |
|---|---|---|
| **Typed retry classification** | A 429 is "later", a 401 is "no". Retrying the second only hides it — so the default is *not* to retry. | `llm.py` |
| **Contract, then critic** | Regex checks the shape for free; a small model judges the substance. A rejected step is re-dispatched at most once, because an unbounded repair loop is how an agent system spends a whole budget on one step. | `contracts.py`, `critic.py` |
| **Resumable execution** | Every transition is written to SQLite *before* its event is emitted, so a killed process can be picked up: completed steps are skipped and their artifacts rehydrated. | `runstore.py` |
| **Quality-weighted routing** | Memory blends success rate with the critic's score and decays old episodes by half-life, so an agent that succeeded badly stops outranking one that succeeded well. | `memory.py` |
| **Semantic recall with no dependency** | Hashed word + character-trigram embeddings: no key, no network, identical across processes. "Optimise the caching layer" and "cache optimisation" score 0.000 lexically and 0.176 semantically. | `embeddings.py` |
| **Meetings that argue** | Attendees each get one bounded turn to object before the chair sums up — concurrent, capped, and billed to their own token usage. | `debate.py` |
| **Sandboxed by default** | The terminal tool is an allowlist over `argv[0]`, `shell=False`, jailed cwd, scrubbed environment. Refusals come back as readable payloads the model can route around. | `tools/terminal.py` |
| **Real telemetry** | Provider token counts, priced per model that actually answered, one span per mission/step/turn/tool/call. | `pricing.py`, `tracing.py` |
| **Plans checked against tool grants** | A step declares the tools it cannot be done without, and one assigned to a specialist that lacks them is moved. The failure it prevents is not a crash — it is an agent with no `terminal` writing a plausible report with an invented number in it. | `planner.py` |
| **A conclusion, not a closing sentence** | The briefing used to end on "System state is healthy", which printed identically whether the goal was answered or four stubs were filed. A small model now draws the finding from the artifact bodies — and when there is no key, the briefing says it has no conclusion rather than reaching for the reassuring noise. | `synthesis.py` |
| **A kill switch that stops the mission** | Cancelling the agents only stopped the current turn: `run()` clears its own cancel flag, so the next step came up clean. The scheduler now refuses to dispatch, and waits on the halt alongside the steps so a stuck agent cannot outlast it. | `orchestrator.py` |

Full rationale for each, including the ones that did not work, is in
[[DECISIONS]].

---

## MCP

The office speaks Model Context Protocol in both directions.

**Other people's tools, in here.** Copy `backend/mcp_servers.example.json` to
`backend/mcp_servers.json`, and set `MCP_ENABLED=true`:

```json
{ "mcpServers": {
    "filesystem": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "./backend/data/missions"],
      "grant_to": { "coder": ["read_file", "write_file"], "data_analyst": "*" }
    },
    "docs": { "url": "https://example.com/mcp", "grant_to": ["researcher"] }
} }
```

Every tool a server lists is registered as `mcp__<server>__<tool>` and is then an
ordinary tool: same `tool.called` / `tool.result` events, same timing, and the
agent walks to the **Integrations Bench** station to use it. A server that will
not connect is a logged warning — the office still opens.

`grant_to` picks who may call what, as a list (every tool, to those agents) or a
map (named tools per agent, `"*"` for all). The map exists because the list is
all-or-nothing, and an agent granted a filesystem server gets its `delete_file`
along with its `read_file` — the wrong default for the server type most likely to
be pointed at a real disk. Omit `grant_to` and every specialist gets everything.

**This office, out there.** `python -m mcp_server` (from `backend/`) serves
`run_mission`, `get_mission` and `list_agents` over stdio, so Claude Code or any
MCP client can drive the harness:

```json
{ "mcpServers": {
    "agenthq": { "command": "python", "args": ["-m", "mcp_server"], "cwd": "backend" }
} }
```

Both need `pip install -r requirements-dev.txt` (the `mcp` package is imported
lazily and is not required to run the app).

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
pip install -r requirements-dev.txt
cd backend && python -m pytest -q      # 444 tests, all passing
```

CI runs the same suite on Python 3.10 and 3.12, plus `ruff check` and the eval
smoke suite against its committed cassettes.

There is no JS test runner — adding one would be this project's first node build
step. Two hand-run validators cover what matters instead:

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
| Interop | `backend/mcp_client.py`, `backend/mcp_server.py` | MCP in both directions |
| Telemetry | `backend/tracing.py`, `backend/pricing.py` | One span per unit of work; real token counts, priced per model |
| Agent engine | `backend/base_agent.py` | ReAct loop: reason → tool call → observe → repeat |
| Event fabric | `backend/event_bus.py` | In-memory async pub/sub, 200-event rolling history |
| Tools | `backend/tool_registry.py`, `backend/tools/` | 6 built-in tools, sandboxed, plus any MCP server |
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
| `researcher` | Alice | Search, RAG & discovery | web_search, read_file |
| `coder` | Charlie | Code & bug hunting | terminal, code_analysis, read_file, write_file |
| `data_analyst` | Bob | Data & analytics | read_file, list_files, terminal |
| `creative` | Eve | Writing & synthesis | write_file, read_file |

Adding an agent is a new JSON file in `backend/agents/` plus a desk in
`office_map.js` — see [[ARCHITECTURE]].

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

---

## License

MIT for the harness and server code — see [`LICENSE`](LICENSE). The pixel-art
source packs are third-party licensed and are **not** in this repository; the
built atlas is committed so a clone runs without them. See [[STACK]].
