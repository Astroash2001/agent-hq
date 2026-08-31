# AgentHQ

A multi-agent LLM harness with a **live 2D pixel-art office** as its interface. Five
named agents sit at desks in a rendered office; when you dispatch a task, the boss
decomposes it, hands subtasks to specialists, and you watch the work happen — task
orbs fly between desks, speech bubbles pop, the terminal streams logs, and a token
gauge fills.

The office is not decoration. It *is* the observability layer.

> Docs map: [[ARCHITECTURE]] · [[STACK]] · [[DECISIONS]] · [[PROTOCOL]] · [[TODO]]

---

## Running it

Two ways, depending on what you're working on.

### Full stack (backend + UI)

```bash
cd backend
python main.py            # uvicorn on http://localhost:8080
```

`main.py` mounts `frontend/` as static files, so the UI is served from the same
origin and the WebSocket at `/ws` connects with no CORS or port juggling.

**No `requirements.txt` exists yet** — see [[TODO]]. Currently installed:
`fastapi uvicorn pydantic pydantic-settings litellm pytest pytest-asyncio httpx`.

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
`backend/.env` or the environment. **Without a key the system still runs fully** —
`BaseAgent` falls back to a deterministic simulator that produces realistic tool
calls and outputs. See [[DECISIONS]].

### Tests

```bash
cd backend && python -m pytest -q     # 32 tests, all passing
```

---

## Major components

| Area | Path | What it does |
|---|---|---|
| API + WebSocket | `backend/main.py` | FastAPI app, connection manager, event broadcast, static mount |
| Task routing | `backend/orchestrator.py` | Plans a goal into a DAG, runs the independent steps together, synthesises a briefing |
| Planner | `backend/planner.py` | LLM plan -> validated DAG, with the old keyword buckets as the fallback |
| Mission workspace | `backend/workspace.py` | Shared artifact store; one step's output becomes the next one's context |
| Spatial runtime | `backend/office.py` | Tools bound to stations; agents walk to them and queue when one is busy |
| Episodic memory | `backend/memory.py` | Records every finished subtask; drives recall and adaptive routing |
| Agent engine | `backend/base_agent.py` | ReAct loop: reason → tool call → observe → repeat |
| Event fabric | `backend/event_bus.py` | In-memory async pub/sub, 200-event rolling history |
| Tools | `backend/tool_registry.py`, `backend/tools/` | 6 tools: terminal, web_search, read_file, write_file, list_files, code_analysis |
| Agent definitions | `backend/agents/*.json` | 5 manifests — identity, prompt, tool grants |
| Office renderer | `frontend/js/canvas.js` | Tilemap, A* pathing, depth sort, lighting, doors, labels |
| Office data | `frontend/js/office_map.js` | The whole floor plan as data — 40×24 tiles |
| HUD | `frontend/js/hud.js` | Agent dock, terminal, token gauge, task queue, memo |
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

## Current status

**Working end to end.** Prompt → planned DAG → parallel specialist execution over a
shared workspace, with agents walking to the stations their tools live at →
synthesised Markdown briefing, streamed live to the office and HUD. 32 tests pass.

**Where the effort has gone recently:** the office rendering. It has been rebuilt
against a series of reference images — real wall courses, warm baked lighting,
composed workstations, a glazed conference room with a working automatic door, and
a validated prop layout (213 props, `check_layout.py` clean).

**Recently landed on the backend:** real LLM planning into a validated DAG,
parallel execution of independent steps, a shared workspace so specialists build
on each other's output, tool-gating stations agents must walk to and queue for,
and an episodic memory that both primes agents and can re-point a step at
whichever specialist has the better record. New events are documented in
[[PROTOCOL]]; every subsystem has a kill switch in `config.py`.

**The honest gaps** (detail in [[TODO]]):

- Walk times ignore walls, so a backend route and a drawn route can disagree
- Nothing marks an agent run a failure yet, so `success_rate` is 1.0 for everyone
  and adaptive rerouting will not fire in practice
- Memory similarity is lexical token overlap, not semantic
- Token counts are **estimates**, not real usage, on the simulator path
- No `requirements.txt`, no pinned dependency set
- 15 authored sprites and 8 orphaned PNGs are dead weight in the atlas
