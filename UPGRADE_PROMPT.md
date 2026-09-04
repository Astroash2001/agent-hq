# AgentHQ — Harness Hardening Brief

**Paste this whole file as your opening message. Then execute ONE work package per
session, in the numbered order. Do not attempt more than one WP in a single run.**

---

## 0. Who you are and what this repo is

You are working on **AgentHQ**, a multi-agent LLM orchestration harness written in
Python 3.10.9 with a FastAPI backend and a zero-build vanilla-JS frontend that
renders a live 2D pixel-art office as its observability surface. It runs on
**Windows** (keep all Python paths `os.path`-joined or `pathlib`-based).

The project already works end to end. Your job is **not** to rewrite it. Your job is
to close specific engineering gaps that stand between "impressive demo" and
"credible AI infrastructure." Every existing behaviour must survive.

### Verified current state — trust this, do not re-derive it

```
backend/
  main.py           229 LOC  FastAPI app, ConnectionManager, /ws, static mount of frontend/
  orchestrator.py   809 LOC  Singleton. Plans -> DAG -> wave execution -> synthesis
  planner.py        370 LOC  LLM plan -> validate_plan() repair -> PlanStep[]; heuristic_plan fallback
  base_agent.py     504 LOC  ReAct loop (max 8 steps), per-step tool grants, cancellable
  llm.py            229 LOC  THE single model call site. Typed retry + cross-provider fallback
  memory.py         287 LOC  Episodic store (JSON), IDF-weighted lexical recall, competence routing
  workspace.py      222 LOC  MissionWorkspace blackboard; produces/consumes artifacts
  office.py         439 LOC  Spatial runtime: tool stations, walk_to, use_tool_station, hold_meeting
  tool_registry.py  267 LOC  Singleton registry, OpenAI-shaped schemas, emits tool.called/tool.result
  event_bus.py       79 LOC  In-memory async pub/sub, 200-event rolling history
  schemas.py        188 LOC  AgentStatus, EventType, AgentManifest, Plan, PlanStep, DAGNode, Task, TokenUsage
  config.py         236 LOC  pydantic-settings; every subsystem has a kill switch here
  tools/            terminal.py, web_search.py (Tavily), file_ops.py, code_analysis.py, meeting.py
  agents/           orchestrator.json, researcher.json, coder.json, data_analyst.json, creative.json
  tests/            93 tests, ALL PASSING (`cd backend && python -m pytest -q` -> 93 passed in ~65s)
  data/memory.json                     episodic store
  data/missions/<task_id>/              mission.json + report.md + agent-written artifacts
frontend/js/
  app.js            298 LOC  WebSocket router; switch on event.type; standalone offline simulation
  canvas.js        1249 LOC  Tilemap renderer, A* pathing, depth sort, baked lighting, auto doors
  hud.js            732 LOC  Agent dock, terminal log, token gauge, task queue, memo panel
  office_map.js     688 LOC  40x24 tile floor plan as data; 213 props
tools/              Asset pipeline (build_atlas.py, gen_tiles.py, index_pack.py, check_layout.py)
Docs: README.md ARCHITECTURE.md STACK.md DECISIONS.md PROTOCOL.md TODO.md
```

### The five agents

| id | name | role | tools |
|---|---|---|---|
| `orchestrator` | Boss Dave | planning & synthesis | — (chairs meetings) |
| `researcher` | Alice | search, RAG, discovery | web_search, read_file |
| `coder` | Charlie | code & bug hunting | terminal, code_analysis, read_file, write_file |
| `data_analyst` | Bob | data & analytics | read_file, list_files, terminal |
| `creative` | Eve | writing & synthesis | write_file, read_file |

### Architectural invariants — violating any of these is a failed task

1. **`llm.py` is the only place a model is called.** `acompletion()` there owns retry
   classification (`_TRANSIENT_STATUS` / `_FATAL_STATUS` / `_FATAL_TEXT`), provider-
   supplied `retryDelay` honouring, jittered backoff, and one cross-provider
   fallback. Never add a second call site. Never call `litellm` directly elsewhere.
2. **Everything the UI knows arrives as an `Event` on `event_bus`.** No polling, no
   direct WebSocket writes from orchestrator/agents/tools. New information means a
   new `EventType` in `schemas.py`, documented in `PROTOCOL.md`, plus a case in
   `app.js`.
3. **Every new subsystem gets a kill switch in `config.py`**, defaulting to the
   behaviour that keeps the demo alive without extra credentials or services.
4. **`settings.llm_strict` semantics are sacred.** When true, a failed LLM call
   raises instead of silently dropping to the simulator, and a failed plan raises
   instead of falling back to `heuristic_plan`. Any new fallback path you add MUST
   respect it — a misconfiguration must never look like a successful run.
5. **The app must still start and run a full mission with zero API keys**, via
   `base_agent._simulate_specialist_action` and `planner.heuristic_plan`.
6. **Do not touch `tools/` (the asset pipeline), `frontend/js/canvas.js` rendering
   internals, `frontend/js/office_map.js`, `furniture/`, or `frontend/assets/`**
   unless a work package explicitly says to. The art is done.
7. **All 93 existing tests keep passing.** Run `cd backend && python -m pytest -q`
   before you finish and paste the tail of the output. If a test must change because
   a contract legitimately changed, say so explicitly and justify it.

### House style — match it exactly, this is what makes the repo distinctive

Comments in this codebase explain **why**, anchored to an observed failure, in prose.
They are not restatements of the code. Study these real examples from the repo:

```python
# Tokens burned across the whole roster.
#
# Agents keep their own running totals (BaseAgent.token_usage), so the
# mission figure is just their sum -- there is no separate ledger to keep
# in step with them.

# Prefer what the agent actually wrote over what it said about it.
# `result_text` is the closing chat message -- filing that as the
# artifact meant downstream steps consumed "Task completed after 4
# execution steps." instead of the 2.4KB report the agent produced.

# Exponential with jitter. Jitter matters when a wave of agents is
# rate-limited together -- without it they all come back at the same instant
# and trigger the same limit again.
```

Rules: use `--` not em dashes. Section dividers are
`# ----------------------------------------------------------------- name`.
Docstrings are short prose paragraphs, not `:param:` lists. When you fix something,
the comment records what went wrong before, concretely. Never write a comment that
only says what the next line does.

---

## 1. What you are building toward

Three sentences, and every decision should serve them:

> AgentHQ is a multi-agent orchestration harness that is **measurable** (a golden
> eval suite with judge-scored artifacts and per-mission cost accounting),
> **durable** (missions survive a process kill and resume), and **inspectable**
> (every run emits a structured trace that can be replayed frame-by-frame through
> the office UI as a time-travel debugger).

That last clause is the differentiator no other agent project has. Protect it.

---

## 2. Work packages

Execute in order. WP1 and WP2 unblock WP3; WP3's numbers feed WP5 and WP8.

---

### WP1 — Real cost, latency and trace telemetry

**Problem.** `base_agent._simulate_specialist_action` returns hardcoded token counts
(90–180 prompt, 40–95 completion), so the HUD gauge is fiction whenever no key is
set. `settings.max_tokens_budget_per_task` (400000) is checked only between waves in
`orchestrator._execute_plan`. Nothing records dollars. Nothing survives the run
except `mission.json` and the WebSocket payload, so a finished mission cannot be
inspected afterward.

**Create `backend/pricing.py`.** A module-level dict of per-1M-token input/output USD
prices keyed by litellm model string, covering at minimum the Gemini, OpenAI,
Anthropic and Groq families this repo's `config.py` mentions. Expose
`price(model, prompt_tokens, completion_tokens) -> float` and
`is_priced(model) -> bool`. Unknown models return `0.0` and are reported as
`"unpriced"` rather than silently costing nothing — an unpriced model must be
visible, not invisible.

**Create `backend/tracing.py`.** A span recorder, not a wrapper around an SDK:

- `Span` dataclass: `span_id`, `parent_id`, `kind` (`"mission" | "step" | "turn" |
  "tool" | "llm"`), `name`, `t_start`, `t_end`, `attrs: dict`, `status`,
  `error: str | None`.
- `Trace` holds the span list for one mission and serialises to newline-delimited
  JSON.
- `tracer.span(kind, name, parent, **attrs)` as an async context manager that records
  duration and captures exceptions into `status="error"` **without swallowing them**.
- On mission end, write `backend/data/missions/<task_id>/trace.jsonl`, one span per
  line, ordered by `t_start`. NDJSON specifically, so a partial file from a killed
  process is still parseable — WP4 depends on that.
- Gate the module on `settings.tracing_enabled` (default `True`; it has no external
  dependency, so there is no reason to default it off).
- Optional OpenTelemetry export behind `settings.otel_enabled` (default `False`) and
  `settings.otel_endpoint`. Import `opentelemetry` lazily inside the function so a
  missing package cannot break startup. Add the OTel packages to
  `requirements-dev.txt` only, with a comment saying they are optional.

**Modify `backend/llm.py`.** Return real usage alongside the response. Add a small
`LLMResult` NamedTuple (`response`, `model`, `prompt_tokens`, `completion_tokens`,
`cost_usd`, `attempts`, `fell_back: bool`) or attach the fields to the returned
object — pick one and be consistent across all call sites. Read usage from the
litellm response (`response.usage.prompt_tokens` / `.completion_tokens`, guarding for
`None`), and record which model in the fallback chain actually answered. **Do not
change the retry/fallback logic itself.**

**Modify `backend/base_agent.py`.** Feed real usage into `self.token_usage` when the
LLM path answered. On the simulator path, keep the estimates but tag them
`"estimated": True` in the `token.update` payload so the UI can mark the gauge as
unmeasured. Open an `llm` span per turn and a `tool` span per tool call.

**Modify `backend/orchestrator.py`.** One `mission` span wrapping
`handle_user_input`, one `step` span per `_run_step`. Extend the `mission.json`
manifest with `cost_usd`, `unpriced_models: []`, and a `per_step` array carrying
`step_id`, `agent_id`, `model`, `prompt_tokens`, `completion_tokens`, `cost_usd`,
`duration_sec`, `tool_calls`, `retries`, `fell_back`. Extend the `task.completed`
event payload with `cost_usd` and `tokens_estimated: bool`.

**Add `backend/tests/test_telemetry.py`.** Cover: pricing arithmetic on a known
model; an unknown model reports unpriced rather than silently zero-cost; a trace file
is written and every line parses as JSON; span parent/child ids form a single tree
rooted at the mission span; `mission.json` carries `per_step` with one entry per plan
step; the simulator path sets `tokens_estimated: true`.

**Acceptance.** A mission run with no API key produces a valid `trace.jsonl` and a
`mission.json` whose `per_step` covers every step. A mission run with a key reports a
non-zero `cost_usd` matching hand-computed arithmetic from the pricing table.
`pytest -q` still green.

---

### WP2 — Artifact contracts and a bounded critic/repair loop

**Problem.** Steps return free text. `orchestrator._best_artifact` already exists
because an agent's closing chat message once got filed as the artifact, and
`_run_step` already appends an explicit "Deliverable: write your full output to
`<name>`" instruction because a researcher once filed a 39-byte stub as the research
the rest of the mission was built on. Both are patches for a missing contract:
nothing ever *checks* that an artifact is acceptable before downstream steps consume
it.

**Create `backend/contracts.py`.**

- `ArtifactSpec`: `min_chars`, `required_sections: list[str]` (Markdown headings that
  must be present), `must_not_match: list[str]` (regexes catching known stubs — the
  literal `"Task completed after \d+ execution steps"` is the first entry),
  `require_citations: bool`.
- Infer a spec from the artifact filename plus the step description, with a
  permissive default. A `*_report.md` demands more than a `*_notes.md`.
- `check(artifact, spec) -> CheckResult` with `ok: bool`, `failures: list[str]`, and
  `severity`. Pure functions, no LLM, no I/O — this layer must be fast and testable.

**Create `backend/critic.py`.** The LLM-judge half, gated on
`settings.critic_enabled` (default `True`) and skipped entirely when no key is
configured:

- `critique(goal, step_description, artifact_text) -> Critique` with
  `verdict: "accept" | "revise"`, `score: float` in 0–1, `reasons: list[str]`, and
  `guidance: str` — the concrete instruction handed back to the agent.
- One call through `llm.acompletion` with `temperature=0.0` and a strict JSON reply,
  reusing `planner._extract_json` rather than writing a second JSON scraper.
- Cheap by construction: `settings.critic_model` defaults to
  `settings.fallback_model` so criticism does not cost more than the work.

**Modify `orchestrator._run_step`.** After the artifact is filed and before
`memory.record`:

1. Run `contracts.check`. On failure, re-dispatch the same step to the same agent
   with the failure list appended to the task description. At most
   `settings.max_repair_attempts` (default `1`) extra attempts.
2. If contracts pass and the critic is enabled, run `critique`. On `"revise"`,
   re-dispatch once with `guidance` appended.
3. Emit a new `EventType.STEP_CRITIQUED` carrying `step_id`, `verdict`, `score`,
   `reasons`, `attempt`. Add it to `PROTOCOL.md` and give `app.js` a case that logs it
   to the HUD terminal and shows a brief bubble over the agent.
4. Pass the final `score` into `memory.record` as a new `quality: float` field on
   `Episode`. **Default it for existing records so `data/memory.json` still loads** —
   `memory.load` must tolerate episodes written before this field existed.

**Why this must be bounded.** An unbounded repair loop is how agent systems burn a
budget on one step. The cap is the feature. Every repair attempt gets its own `step`
span child in the trace, so the cost of quality is visible in WP1's numbers.

**Add `backend/tests/test_contracts.py` and `backend/tests/test_critic.py`.** Cover:
the stub regex catches `"Task completed after 4 execution steps."`; a short artifact
fails `min_chars`; a missing required heading is reported by name; repair stops at the
cap and the step still completes rather than hanging; a critic `"revise"` triggers
exactly one re-dispatch; an old `memory.json` without `quality` still loads; critic
disabled is a clean no-op.

**Acceptance.** Feed a mission whose researcher returns a stub (mock it) and show the
contract catching it, one repair firing, and the trace containing both attempts.

---

### WP3 — The evaluation harness

**This is the keystone. A harness without evals is a demo.** Everything else in this
brief is in service of being able to run this and get a number.

**Create `evals/` at the repo root:**

```
evals/
  tasks/                 one YAML per mission, 12 minimum
  cassettes/             recorded LLM exchanges for deterministic replay
  runner.py              executes a suite, writes results
  judge.py               LLM-as-judge rubric scoring
  replay.py              record/replay transport for llm.acompletion
  report.py              renders scorecard.md + scorecard.json
  scorecard.md           committed, regenerated, human-readable
  README.md              how to run it, what each number means
```

**Task file schema.** Keep it small enough that adding a task is trivial:

```yaml
id: battery_comparison
goal: "Compare sodium-ion and lithium-ion home batteries and recommend one."
tags: [research, synthesis, multi_step]
expect:
  min_steps: 3
  max_steps: 5
  agents_involved: [researcher, creative]
  artifacts: ["*_report.md"]
  rubric:
    - "Names at least two specific battery chemistries with figures"
    - "States a recommendation with a stated reason"
    - "Cites at least one source when web_search was available"
  must_not:
    - "Contains placeholder text or a stub completion message"
budget:
  max_cost_usd: 0.25
  max_duration_sec: 180
```

Seed the 12 tasks across four shapes, so the suite exercises the harness rather than
one happy path: **pure research** (2), **research then synthesis with a real handoff**
(3), **code analysis and a fix** (2), **a mission that requires a meeting step** (2),
**deliberately adversarial** (3) — an ambiguous goal, a goal that should produce a
1-step plan, and a goal whose obvious agent lacks the needed tool (this last one
tests the `TODO.md` gap where the planner does not see agents' tool grants).

Mine `backend/data/missions/` for real prior goals — `task_2f4a2b87` (home battery
comparison), `task_903fb391` (solar efficiency), `task_dfa2e629` (solid-state
battery) and `task_00ffebf4` (Indian news brief) are already in the repo with their
artifacts. Use them, so the golden set reflects real usage.

**`evals/replay.py` — determinism.** An eval suite that gives a different answer each
run measures nothing.

- Record mode (`--record`): wrap `llm.acompletion`, hash the request (model +
  messages + tool schemas, canonically JSON-serialised) into a key, store the
  response under `cassettes/<task_id>/<hash>.json`.
- Replay mode (default in CI): serve from cassette; a miss is a hard error naming the
  missing key, never a live call. A silent fall-through to the network is how a
  deterministic suite becomes a flaky one.
- Implement by monkeypatching the `llm.acompletion` symbol, not by editing `llm.py`.
  The harness must not know the eval layer exists.

**`evals/judge.py`.** Score each rubric line independently against the produced
artifacts: `pass | fail | partial` with a one-sentence reason. Use a fixed judge model
from `settings.judge_model` at `temperature=0.0`. Score the **artifact files**, not
the synthesised briefing — `_synthesize_results` is string assembly, and grading it
grades the template. Emit both a per-rubric table and an aggregate `quality` in 0–1.

**`evals/runner.py`.** CLI: `python -m evals.runner --suite all --model
gemini/gemini-2.5-flash --replay`. Runs each task through the real
`orchestrator.handle_user_input`, collects `mission.json` + `trace.jsonl`, applies the
structural `expect` checks and the judge, and writes
`evals/results/<timestamp>_<model>.json`. Support `--models a,b,c` to sweep and
`--repeat n` to compute pass@k and variance. **Never mutate
`backend/data/memory.json`** — point `settings.memory_path` at a temp file for the
duration, or the suite trains the thing it is measuring.

**`evals/report.py` -> `scorecard.md`.** One committed table:

| task | model | structural | quality | cost | p50 latency | pass@3 |
|---|---|---|---|---|---|---|

Plus a per-model summary row, a regression delta against the previous committed
scorecard, and the list of tasks that changed verdict. **Commit the scorecard.** It is
the single most persuasive file in the repository.

**Acceptance.** `python -m evals.runner --suite all --replay` runs offline with no API
key, is byte-deterministic across two consecutive runs, and regenerates
`scorecard.md`. Include a real scorecard from at least two different models in your
final commit.

---

### WP4 — Durable, resumable execution

**Problem.** `orchestrator.active_tasks` and `self.workspaces` are in-memory dicts.
`mission.json` is written once, at the end. Kill the process mid-mission and the run
is gone with no record of how far it got. Separately, `_execute_plan`'s
`max_parallel_agents` cap is a slice of the ready set (`ready[:limit]`), so a wave of
five runs 3 then 2 instead of starting the fourth the moment a slot frees — already
flagged in `TODO.md`.

**Create `backend/runstore.py`.** SQLite via stdlib `sqlite3` — no new dependency, and
a single file is easy to inspect. Tables: `missions` (id, goal, status, plan_source,
started_at, ended_at, cost_usd) and `steps` (mission_id, step_id, agent_id, status,
attempt, produces, consumes, artifact_path, started_at, ended_at, tokens, cost_usd,
error). Write-through: a step's status change hits the DB *before* the event is
emitted, so the DB is never behind what the UI showed. WAL mode, and every write in a
short transaction — a killed process must leave a readable file.

**Modify `orchestrator`:**

- `handle_user_input` gains `resume_from: str | None`. On resume, load the mission and
  its completed steps, rehydrate `MissionWorkspace` from the artifact files already on
  disk in `data/missions/<id>/`, and treat completed steps as satisfied dependencies.
  Re-run only `pending`, `running` (interrupted) and `failed` steps.
- Replace the slice cap with `asyncio.Semaphore(settings.max_parallel_agents)` held
  around each `_run_step`, and dispatch the whole ready set. The semaphore is what
  makes the cap a real concurrency limit instead of a batch size.
- Per-step timeout `settings.step_timeout_sec` (default `300`) via `asyncio.wait_for`.
  A timeout is a step failure, recorded to memory as one — which matters, because
  failure recording is what makes competence rerouting real.
- Step-level retry: `settings.step_max_attempts` (default `2`) for *transient* step
  failures only. Reuse `llm.is_transient` for the classification rather than writing a
  second one. A step that fails because the agent produced garbage is WP2's problem,
  not a retry candidate.

**Add `GET /api/missions`, `GET /api/missions/{id}`,
`POST /api/missions/{id}/resume` to `main.py`,** plus
`GET /api/missions/{id}/trace` serving the NDJSON for WP8.

**Add `backend/tests/test_runstore.py` and `test_resume.py`.** Cover: a mission killed
after step 2 resumes and runs only steps 3+; completed artifacts are rehydrated and
readable by the resumed steps; the semaphore never permits more than
`max_parallel_agents` concurrent `_run_step` calls (assert with a counter and a
recorded high-water mark); a step timeout is recorded as a failure and reaches memory;
a transient step failure retries and a garbage-output failure does not.

**Acceptance.** Start a mission, kill the process mid-flight, restart,
`POST /api/missions/{id}/resume`, and the mission finishes correctly without redoing
completed work. Demonstrate the high-water-mark assertion passing.

---

### WP5 — Semantic memory and quality-weighted routing

**Problem.** `memory._similarity` is IDF-weighted token overlap. It cannot tell that
"speed up the cache" and "optimise memoisation" are the same task — `TODO.md` says so,
and notes that swapping in embeddings means replacing `_similarity` and nothing else.
Separately, `competence()` and `suggest_agent()` now have failure data (fixed in a
recent commit) but still no *quality* signal — an agent that technically succeeded
badly scores the same as one that succeeded well.

**Create `backend/embeddings.py`.** A pluggable provider:

- `embed(texts: list[str]) -> list[list[float]]` with providers `"litellm"` (uses the
  key material already configured) and `"hash"` — a deterministic hashed-bag-of-words
  fallback with cosine similarity that needs no key and no network.
  `settings.embedding_provider` defaults to `"hash"` so the no-key path keeps working,
  and `settings.embedding_model` names the real one.
- Disk cache at `backend/data/embeddings.json` keyed by SHA-256 of the text. Embedding
  the same episode text on every recall is pure waste.

**Modify `memory.py`.**

- Replace `_similarity` with a cosine over embeddings, keeping the lexical path as the
  fallback when embedding is unavailable. Keep the method signature identical.
- Episodes gain `embedding` and the `quality` field from WP2. **`load()` must tolerate
  the existing `data/memory.json` with neither field** — backfill lazily, never crash
  on an old store.
- `competence(agent_id, description)` becomes quality-weighted: blend success rate
  with mean `quality` on similar episodes. Document the weighting in a comment and keep
  the existing minimum-episode-count and margin gates, so a cold or thin store still
  cannot override the planner.
- Add decay: `settings.memory_half_life_days` (default `30`) down-weights old
  episodes, so an agent that improved is not judged forever on its first week.

**Add `backend/tests/test_semantic_memory.py`.** Cover: the hash provider is
deterministic across processes; paraphrases score higher than unrelated text; an old
`memory.json` loads and backfills; a high-success/low-quality agent loses to a
lower-success/high-quality one only past the margin; decay changes ranking as
expected; the embedding cache is hit on a second identical call.

**Acceptance.** Show a `route.changed` event firing for a paraphrased task that the
lexical scorer demonstrably missed. Include the before/after similarity numbers.

---

### WP6 — Real agent-to-agent interaction, and MCP

**Problem A: meetings are a monologue.** `orchestrator._run_meeting` holds the
attendees at the conference table for the whole call, but only the chair runs a ReAct
loop. The docstring is honest that N loops would multiply cost for no more
information — which is true of *reading*, and false of *disagreeing*.

**Add a debate round, gated on `settings.debate_enabled` (default `True`) and
`settings.debate_rounds` (default `1`).** In `_run_meeting`, before the chair
synthesises: give each attendee exactly one turn — a single `llm.acompletion` call
each, no tool loop, no ReAct — asked to state the strongest objection to the artifacts
on the table from their own specialism. Run the attendee turns concurrently under
`asyncio.gather`; they are independent by construction. Collect their positions into
the chair's context, then let the chair's existing ReAct loop run with
`record_decision` as before. Emit a new `EventType.AGENT_ARGUED` per position so the
office shows speech bubbles going around the table — this is the most legible
multi-agent moment in the whole product and it should be visible.

Cost discipline: one bounded call per attendee, capped by `settings.meeting_capacity`
(4, mirroring the `conference_table` station capacity in `office.py`). Each turn is its
own trace span, so the price of debate is measurable in WP1's numbers.

**Problem B: no MCP.** The tool layer is a private registry. Model Context Protocol is
the interop standard, and supporting it is cheap credibility.

**Create `backend/mcp_client.py`.** Read server definitions from
`backend/mcp_servers.json` (stdio and HTTP transports), list each server's tools at
startup, and register them into the existing `tool_registry` with a namespaced name
(`mcp__<server>__<tool>`). They must flow through `tool_registry.execute` unchanged,
so `tool.called` / `tool.result` events, timing and the office station walk all keep
working — bind MCP tools to a new `integrations` station in `office.py` at a free tile,
so agents physically walk somewhere to use them. Gate on `settings.mcp_enabled`
(default `False`, since it needs external servers), and make a failed server
connection a logged warning that does not block startup.

**Create `backend/mcp_server.py`.** Expose AgentHQ's own capabilities over MCP:
`run_mission(goal)`, `get_mission(id)`, `list_agents()`. Then Claude Code or any MCP
client can drive the harness. Ship a `mcp_servers.example.json` and a README section
showing the config.

**Add `backend/tests/test_debate.py` and `test_mcp.py`** (the latter against a stub
in-process server, not a real one). Cover: each attendee gets exactly one call and no
tool loop; attendee turns run concurrently; debate disabled is byte-identical to the
current behaviour; an MCP tool appears in `get_schemas_for_agent` only for agents
granted it; an unreachable MCP server logs and startup still succeeds.

**Acceptance.** A meeting mission's trace shows N attendee spans plus a chair span, and
the recorded decision references an objection an attendee actually raised.

---

### WP7 — Sandboxing, hardening, and shipping

**Problem.** `tools/terminal.py` runs `execute_terminal(command, cwd, timeout)` with no
allowlist, reachable by the `coder` and `data_analyst` agents. `main.py` sets
`allow_origins=["*"]` with `allow_credentials=True`. `POST /api/emergency-stop` and
`WS /ws` are unauthenticated. `TODO.md` flags all of these. There is no `Dockerfile`,
no CI, no `pyproject.toml`, no `LICENSE`.

**Harden `tools/terminal.py`.** Turn the liability into a talking point:

- Command allowlist in `settings.terminal_allowed_commands` (default a conservative
  read-only set: `python`, `pytest`, `ls`/`dir`, `cat`/`type`, `git status`,
  `git diff`, `git log`). Parse with `shlex.split` and match the argv[0] basename —
  never substring-match a command string, which `rm -rf` trivially defeats.
- Deny shell metacharacters entirely (`;`, `&&`, `||`, `|`, backtick, `$(`,
  redirects). Run with `shell=False` and an explicit argv list.
- Jail `cwd` under the mission workspace directory, resolved with `os.path.realpath`
  and checked with `os.path.commonpath`, so `..` and symlinks cannot escape.
- Scrub the child environment: pass only `PATH` and `SYSTEMROOT`, so no API key reaches
  a subprocess an agent chose to run.
- Cap output at `settings.terminal_max_output_chars` and hard-kill on timeout.
- Return refusals as a clear tool result explaining what was blocked and why — a
  refusal the model can read and route around beats an exception it retries blindly.

**Apply the same containment to `tools/file_ops.py`.** `read_file` / `write_file` /
`list_files` must resolve inside the bound `MissionWorkspace` directory, or the repo
root read-only, and nowhere else. `workspace._safe_name` already exists; extend the
idea to path resolution rather than duplicating it.

**Harden `main.py`.** `settings.cors_origins` as a real list defaulting to
`["http://localhost:8080"]`; `allow_credentials=True` only when the list is not
`["*"]`. An optional shared token (`settings.api_token`) required on `/ws` connect and
on `POST /api/emergency-stop` when set; unset means open, so local dev is unchanged.
Add a per-connection rate limit on `submit_task`.

**Ship it.**

- `pyproject.toml` — project metadata, `[tool.pytest.ini_options]` with
  `asyncio_mode`, `[tool.ruff]`. Keep `requirements*.txt` working; do not migrate
  dependency management wholesale.
- `Dockerfile` — `python:3.10-slim`, non-root user, `requirements.txt` only,
  `EXPOSE 8080`, `HEALTHCHECK` hitting `/api/health`.
- `docker-compose.yml` — the app plus a mounted volume for `backend/data/`.
- `.github/workflows/ci.yml` — matrix on Python 3.10 and 3.12: install dev
  requirements, `ruff check`, `pytest -q`, then
  `python -m evals.runner --suite smoke --replay` on the cassettes. **The eval suite
  running in CI is the whole point of WP3 being real.**
- `LICENSE` — MIT. Note in `STACK.md` that the art packs are separately licensed and
  the source packs are not in the repo.
- `.dockerignore`, and delete the committed `backend/.pytest_cache/` and
  `.pytest_cache/` per `TODO.md`.

**Add `backend/tests/test_sandbox.py`.** Cover: `rm -rf /` refused; `ls; rm x` refused
for metacharacters; a `cwd` of `../../` refused; `git status` permitted; a subprocess
cannot read `GEMINI_API_KEY` from its environment; output truncation fires; timeout
kills the child; `write_file` outside the workspace refused.

**Acceptance.** `docker compose up` serves a working office at `localhost:8080`. CI
green on both Python versions. Paste the sandbox test output.

---

### WP8 — The DAG panel, the cost panel, and trace replay

**This WP is what makes the project memorable. Do not rush it.**

**Problem.** `orchestrator._emit_dag` emits `EventType.DAG_UPDATE` on every status
change; `app.js`'s switch has **no case for it**, and `hud.updateActiveDagNode` /
`hud.setQueue` are dead code nothing calls. The entire orchestration story — the thing
the whole backend exists to do — has no visual. There is also no hero image in the
README (`Preview.png` was deleted).

**8a. Wire the DAG.** Add a `dag.update` case in `app.js`. Render the graph in `hud.js`
as an SVG panel: nodes coloured by the owning agent's manifest colour, edges from
`dependencies`, node state as `pending / running / completed / failed`, a pulse on
`running`. Lay it out in dependency layers (longest-path from roots) left-to-right, so
a fan-out reads as a fan-out. Overlay a `route.changed` badge on a rerouted node and a
`STEP_CRITIQUED` badge with its score. Match the existing retro CRT aesthetic in
`frontend/css/style.css` — do not introduce a new visual language, and add no build
step or framework. Vanilla JS, as the rest of the frontend is.

**8b. Cost & telemetry panel.** A HUD panel showing live mission cost, tokens by
agent, wall-clock elapsed, and per-step latency as a small bar strip. Mark the figure
`~estimated` when `tokens_estimated` is true, so the simulator never lies to the
viewer.

**8c. Trace replay — the differentiator.** Add a replay mode to the frontend: load a
`trace.jsonl` from `GET /api/missions/{id}/trace`, and drive `canvas.js` and `hud.js`
from the recorded spans instead of the live WebSocket, with play/pause, a scrub bar,
and speed control. Reuse the existing event-routing path in `app.js` by synthesising
events from spans — do not fork the renderer, and do not touch `canvas.js`'s drawing
internals. Add a `GET /api/missions` picker so any past mission can be replayed.

This turns the office from a live dashboard into a **time-travel debugger for
multi-agent runs**. Say exactly that in the README. Nobody else has it.

**8d. README as a shopfront.** Restructure so the first screen sells the harness, not
the game:

1. One-sentence positioning, then a **GIF** — a mission planning, fanning out, the DAG
   lighting up, the cost ticking, the memo landing.
2. An architecture diagram (Mermaid, so it renders on GitHub and stays diffable).
3. The eval scorecard table, inline, with real numbers.
4. "Run it in one command" — the Docker line.
5. Then the office, framed as the observability layer it is.
6. Then the design decisions: typed retry classification, contract-then-critic,
   resumable execution, quality-weighted routing.

**Fix the stale docs, which currently cost real credibility.** `README.md` says
"32 tests, all passing" — it is 93. `TODO.md` lists resolved items as open and has an
entire "Testing" section claiming 11 tests in `test_phase1..4.py`. Someone who runs
`pytest`, sees the mismatch, and then discounts every other claim in the README is a
lost reader. Update `README.md`, `ARCHITECTURE.md`, `PROTOCOL.md` (every new
`EventType` from WP1/2/6 with its payload shape), `TODO.md`, and `DECISIONS.md` (add a
decision entry for each significant choice you make here — pricing table over an API,
SQLite over a service, hash embeddings as the default, cassettes over live eval
calls).

**Acceptance.** A recorded GIF committed to the repo. The DAG panel visibly
progressing through waves. Replay of a past mission from `trace.jsonl` reproducing the
original run. Docs matching reality — verified by running `pytest -q` and quoting the
count.

---

## 3. How to work

**Per work package, in this order:**

1. Read every file the WP touches, in full, before writing anything. This codebase's
   comments carry design rationale that is not recoverable from the code alone —
   `llm.py`'s error classification and `orchestrator._best_artifact` in particular.
2. State your plan: files created, files modified, new settings with defaults, new
   `EventType`s, new tests. Name anything you intend to change that the WP did not ask
   for, with a reason.
3. Implement. Smallest coherent diff that fully satisfies the WP.
4. `cd backend && python -m pytest -q`. Paste the tail.
5. Update `PROTOCOL.md` for new events, `ARCHITECTURE.md` for new modules,
   `DECISIONS.md` for non-obvious choices, `TODO.md` for what you closed and any new
   gap you opened.
6. One commit per WP: `WP<n>: <what changed>`, body explaining why, plus the test count
   before and after.

**Rules that are not negotiable:**

- **Never invent a fact about this repo.** If you need to know whether something
  exists, read the file. Do not assume a function, setting, event type or test is
  present because it would be sensible.
- **Never break the no-API-key path.** After every WP, `python backend/main.py` with an
  empty `.env` must serve a working office and complete a mission.
- **Never add a runtime dependency without justifying it and pinning it.** SQLite,
  hashlib, shlex and asyncio are stdlib — prefer them. OpenTelemetry and any embedding
  client are dev-optional and lazily imported.
- **Never silently change a default that alters existing behaviour.** New subsystems
  default to the current behaviour, or to the variant that needs no credentials.
- **Never leave a bare `except Exception: pass`.** This repo distinguishes transient
  from fatal deliberately; blanket swallowing undoes that.
- **Do not touch the asset pipeline, the tilemap, the sprite atlas, or the renderer's
  drawing internals.** WP8 adds panels and a replay driver; it does not restyle the
  office.
- **If a WP turns out to be wrong for the codebase, say so and propose the alternative
  before implementing it.** A reasoned objection is wanted; silent substitution is not.

**Definition of done for the whole brief:**

- `pytest -q` green, with roughly 160+ tests.
- `python -m evals.runner --suite all --replay` deterministic and offline, and
  `scorecard.md` committed with results from two or more models.
- `docker compose up` serves the app; CI green on 3.10 and 3.12.
- A mission killed mid-flight resumes and finishes.
- A past mission replays through the office from its `trace.jsonl`.
- `README.md` opens with a GIF, a Mermaid architecture diagram, and the real
  scorecard, and every number in the docs matches what the code actually does.
