# WP7 — Sandboxing, hardening, and shipping

**Paste this whole file as your opening message. This is one self-contained work
package. Do not start any other work package; two agents are working on this repo
in parallel and the file ownership below is what keeps you from colliding.**

---

## 0. Where you are

You are working on **AgentHQ**, a multi-agent LLM orchestration harness: Python
3.10.9, FastAPI backend, zero-build vanilla-JS frontend that renders a live 2D
pixel-art office as its observability surface. It runs on **Windows** — keep every
path `os.path`-joined or `pathlib`-based, and read §5 before you write a single
subprocess call, because the obvious approach does not work here.

Repo root: `C:\Users\avina\Desktop\agentic AI\agent-hq`

### Current state — verified, trust it, do not re-derive it

- Branch to start from: **`wp1-telemetry`** at commit `5eb9fc6`.
- **Create your own branch first: `git checkout -b wp7-hardening`.**
- Test suite: `cd backend && python -m pytest -q` → **155 passed** in ~85s.
  That number is the contract. It must be 155 + however many you add, and no
  existing test may be deleted, skipped, or weakened to make yours pass.
- The app runs with **zero API keys**. `python backend/main.py` must still serve
  a working office and complete a mission after your changes. This is
  non-negotiable and it is the single easiest thing to break in this WP.

```
backend/
  main.py           FastAPI app, ConnectionManager, /ws, static mount of frontend/
  orchestrator.py   plans -> DAG -> wave execution -> synthesis      [DO NOT TOUCH]
  base_agent.py     ReAct loop, per-step tool grants                 [DO NOT TOUCH]
  llm.py            THE single model call site                       [DO NOT TOUCH]
  tracing.py        span recorder -> data/missions/<id>/trace.jsonl  [DO NOT TOUCH]
  pricing.py        per-1M-token cost table                          [DO NOT TOUCH]
  contracts.py      artifact specs, pure functions                   [DO NOT TOUCH]
  critic.py         LLM-judge on artifacts                           [DO NOT TOUCH]
  memory.py         episodic store + competence routing              [DO NOT TOUCH]
  workspace.py      MissionWorkspace blackboard                      [read it, don't edit]
  config.py         pydantic-settings; every subsystem has a kill switch   [YOU EDIT]
  tool_registry.py  singleton registry, OpenAI-shaped schemas        [read it, don't edit]
  tools/
    terminal.py     <- YOURS
    file_ops.py     <- YOURS
    web_search.py, code_analysis.py, meeting.py
  tests/            conftest.py + 12 test modules
evals/              being built in parallel by another agent          [DO NOT TOUCH]
frontend/           the office UI                                     [DO NOT TOUCH]
tools/              asset pipeline (build_atlas.py etc)               [DO NOT TOUCH]
```

### Files you own in this work package

You may create or edit **only** these:

```
backend/tools/terminal.py
backend/tools/file_ops.py
backend/main.py
backend/config.py                     (append a settings block; change nothing existing)
backend/tests/test_sandbox.py         (new)
pyproject.toml                        (new)
Dockerfile                            (new)
docker-compose.yml                    (new)
.dockerignore                         (new)
LICENSE                               (new)
.github/workflows/ci.yml              (new)
requirements-dev.txt                  (append only, at the end of the file)
```

Docs: append your entries at the **very end** of `DECISIONS.md`, and add a
section at the **very end** of `TODO.md`. Do not edit `ARCHITECTURE.md`,
`PROTOCOL.md` or `README.md` — the other agent is in them, and end-of-file
appends are the only edits that merge cleanly.

**If you believe you must touch a `[DO NOT TOUCH]` file, stop and say so with your
reasoning instead of doing it.** A reasoned objection is wanted. A silent edit
there will be reverted wholesale.

---

## 1. Architectural invariants — violating any is a failed task

1. **`llm.py` is the only place a model is called.** Nothing in this WP needs a
   model call. If you find yourself adding one, you have misread the task.
2. **Everything the UI learns arrives as an `Event` on `event_bus`.** No polling,
   no direct WebSocket writes from tools.
3. **Every new subsystem gets a kill switch in `config.py`**, defaulting to the
   behaviour that keeps the demo alive with no extra credentials or services.
4. **Never silently change a default that alters existing behaviour.** This one
   bites hardest here: the terminal allowlist and the path jail *are* behaviour
   changes. That is the point of the WP — but they must be settings-driven, and
   the defaults must keep the existing tests and the no-key demo passing.
5. **Never leave a bare `except Exception: pass`.** This repo distinguishes
   transient from fatal deliberately; blanket swallowing undoes that.
6. **Never invent a fact about this repo.** If you need to know whether a
   function, setting, event type or test exists, open the file and look. Do not
   assume something is there because it would be sensible.

---

## 2. House style — match it exactly, this is what makes the repo distinctive

Comments explain **why**, anchored to an observed failure, in prose. They are
never a restatement of the code. Real examples from this codebase:

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

Rules:
- Use `--`, never an em dash.
- Section dividers are `# ----------------------------------------------------------------- name`
- Docstrings are short prose paragraphs, not `:param:` lists.
- When you fix something, the comment records concretely what went wrong before.
- Never write a comment that only says what the next line does.

Test names are sentences describing the guarantee:
`test_a_repair_that_rewrites_in_place_is_not_discarded`, not `test_repair_2`.

---

## 3. The problem you are solving

Verified facts about the current code — these are the liabilities:

**`backend/tools/terminal.py` is 47 lines and has no allowlist.** It calls
`asyncio.create_subprocess_shell(command, ...)` — full shell, arbitrary string,
inherited environment, `cwd` anywhere on disk. It is registered in
`tool_registry.py` as `terminal` and granted to the `coder` and `data_analyst`
agents, which means any model output that reaches those agents can run anything
the Python process can. The registered schema is:

```python
name="terminal",
parameters={"properties": {"command": {...}, "cwd": {...}}, "required": ["command"]}
function=execute_terminal
```

and the current signature is `execute_terminal(command: str, cwd: str = None,
timeout_seconds: int = 15)`. **Keep that name and those parameter names**, or the
registry stops dispatching to it.

**`backend/tools/file_ops.py` has no path containment.** `read_file(filepath)`
opens any path that exists. `list_files(directory)` lists any directory.
`write_file` is bound to the mission workspace when one is running
(`bind_workspace`) but falls back to an unrestricted `open(filepath, "w")` with
`os.makedirs` when it is not.

**`backend/main.py` line 88 sets `allow_origins=["*"]`** on `CORSMiddleware`.
`POST /api/emergency-stop` and `WS /ws` are unauthenticated, and `/ws` accepts a
`submit_task` message with no rate limit.

**A real finding, verified — flag it and fix it deliberately:**
`settings.workspace_root` is
`os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))`, and from
`backend/config.py` that resolves to `C:\Users\avina\Desktop\agentic AI` — the
**parent directory of the repository**, not the repository. Anything that jails
paths to `workspace_root` would therefore permit the whole parent folder and every
sibling project in it. Decide what the jail root should actually be, say so in
`DECISIONS.md`, and do not quietly redefine `workspace_root` if something else
reads it — check first with `grep -rn workspace_root backend/`.

**Missing entirely:** `pyproject.toml`, `Dockerfile`, CI, `LICENSE`.

---

## 4. What to build

### 4a. Harden `backend/tools/terminal.py`

Turn the liability into a talking point. The refusal path matters as much as the
allow path.

- **Allowlist** in `settings.terminal_allowed_commands`. Default to a
  conservative read-only set — `python`, `pytest`, `git status`, `git diff`,
  `git log`, and directory/file listing (see §5 on Windows before you pick the
  names). Parse with `shlex.split(command, posix=False)` and match on the
  **basename of argv[0]**, case-insensitively, with any `.exe` suffix stripped.
  Never substring-match the raw command string: `"rm -rf /"` contains `"git"`
  in plenty of realistic phrasings, and substring matching is defeated trivially.
- **Multi-word allowlist entries** (`git status`) must match on argv[0] *and*
  argv[1], or `git push --force` sails through a `git` entry.
- **Refuse shell metacharacters outright**: `;` `&` `|` `` ` `` `$(` `>` `<`
  `&&` `||` and newlines. Run with `shell=False` and an explicit argv list.
  `asyncio.create_subprocess_exec`, not `..._shell`.
- **Jail `cwd`**: resolve with `os.path.realpath`, then verify containment with
  `os.path.commonpath([root, target]) == root`. `commonpath` raises `ValueError`
  when the two paths are on different Windows drives — catch it and treat it as
  a refusal, not a crash. Symlinks and `..` must both be defeated; write a test
  for each.
- **Scrub the child environment.** Pass only `PATH` and `SYSTEMROOT` (Windows
  needs `SYSTEMROOT` or many binaries fail to start). No API key may reach a
  subprocess an agent chose to run. Test this explicitly by setting a fake
  `GEMINI_API_KEY` and asserting a child cannot read it.
- **Cap output** at `settings.terminal_max_output_chars`, truncating with a
  visible marker rather than silently.
- **Hard-kill on timeout**, and make sure the process is actually reaped.
- **Return refusals as a normal tool result**, not an exception:
  `{"success": False, "refused": True, "error": "<what was blocked and why>"}`.
  A refusal the model can read and route around beats an exception it retries
  blindly. Say which command was blocked and what is permitted.

### 4b. Apply the same containment to `backend/tools/file_ops.py`

`read_file` / `write_file` / `list_files` must resolve inside the bound
`MissionWorkspace` directory, or the repo root read-only, and nowhere else.

`workspace._safe_name` already exists and strips a *filename* to something safe —
read it, and extend the idea to full path resolution rather than duplicating it.
Keep the existing `bind_workspace` contract and the existing return shapes
(`{"success": ..., "filepath": ..., "content": ...}`), because `base_agent` JSON-
encodes these straight into the model's context and `test_mission_output.py`
asserts on them.

Note `test_mission_output.py::test_write_file_without_a_mission_still_writes_to_disk`
asserts the unbound fallback still writes. Decide whether that behaviour survives
inside the jail; if you change it, change that test and justify it explicitly in
your report. Do not delete it.

### 4c. Harden `backend/main.py`

- `settings.cors_origins` as a real list, defaulting to
  `["http://localhost:8080"]`. Set `allow_credentials=True` **only** when the
  list is not `["*"]` — the combination is rejected by browsers anyway and is a
  genuine misconfiguration.
- Optional shared token `settings.api_token`. When set, require it on `/ws`
  connect and on `POST /api/emergency-stop`. **When unset, everything is open** —
  local dev must be unchanged, and the no-key demo must not need a token.
- A per-connection rate limit on the `submit_task` websocket message.

### 4d. Ship it

- **`pyproject.toml`** — project metadata, `[tool.pytest.ini_options]` with
  `asyncio_mode`, and `[tool.ruff]`. Keep `requirements*.txt` working; do **not**
  migrate dependency management wholesale.
- **`Dockerfile`** — `python:3.10-slim`, non-root user, `requirements.txt` only,
  `EXPOSE 8080`, `HEALTHCHECK` hitting `/api/health`.
- **`docker-compose.yml`** — the app plus a mounted volume for `backend/data/`.
- **`.dockerignore`**.
- **`.github/workflows/ci.yml`** — matrix on Python 3.10 and 3.12: install dev
  requirements, `ruff check`, `pytest -q`.
  **The eval-suite CI step is NOT yours.** `evals/` is being built in parallel and
  does not exist on your branch. Leave exactly this marker where it goes:
  `# TODO(WP3): python -m evals.runner --suite smoke --replay` — and nothing more.
- **`LICENSE`** — MIT. Note in your `DECISIONS.md` entry that the art packs are
  separately licensed and the source packs are not in the repo.
- Delete the committed `backend/.pytest_cache/` and `.pytest_cache/` if present,
  and make sure `.gitignore` covers them.

### 4e. `backend/tests/test_sandbox.py`

Minimum coverage — each of these is a named guarantee:

- `rm -rf /` is refused.
- `ls; rm x` is refused for the metacharacter, before anything runs.
- `git status` is permitted; `git push --force` is refused despite `git` being allowed.
- A `cwd` of `../../` is refused.
- A symlink pointing outside the jail is refused.
- A subprocess cannot read `GEMINI_API_KEY` from its environment.
- Output truncation fires and is visible in the result.
- A timeout kills the child and returns a refusal-shaped result.
- `write_file` outside the workspace is refused.
- `read_file` on an absolute path outside the jail is refused.
- With the allowlist emptied via settings, behaviour is explicit (decide and pin
  it: empty means "refuse everything", not "allow everything").

---

## 5. Windows traps that will cost you an hour each

1. **`ls`, `dir`, `cat` and `type` are not executables on Windows.** `dir` and
   `type` are `cmd.exe` builtins with no binary on disk, so an allowlist entry
   for them plus `shell=False` can never run. Resolve argv[0] with
   `shutil.which()` and return a clear refusal when it does not resolve. Do not
   "fix" this by re-enabling the shell — that is the vulnerability you were sent
   to close. If you want directory listing to work cross-platform, implement it
   in Python rather than shelling out, or accept that `dir` is refused on this
   host and say so in the refusal message.
2. **pytest's `tmp_path` fixture fails on this machine** with
   `PermissionError: [WinError 5] ... pytest-of-avina`. Every test here uses
   `tempfile.mkdtemp(prefix="agenthq_...")` with `shutil.rmtree(..., ignore_errors=True)`
   in a fixture teardown instead. Follow that; `test_mission_output.py` has the
   pattern and a comment explaining why.
3. **`os.path.commonpath` raises `ValueError` across drives** (`C:` vs `D:`), and
   also on mixed absolute/relative input. Catch it.
4. **`backend/tests/conftest.py` clears every credential session-wide** and sets
   `llm_strict=False`, so the whole suite runs offline on the simulator. Do not
   fight this; if your test needs a key present, `monkeypatch.setattr(settings,
   "gemini_api_key", "test-key")` inside the test.
5. **Line endings**: git reports `LF will be replaced by CRLF`. Ignore it, it is
   pre-existing and not yours to fix.

---

## 6. How to work

1. Read every file you are about to change, in full, first. The comments in this
   codebase carry design rationale that is not recoverable from the code.
2. State your plan before implementing: files created, files modified, new
   settings and their defaults, new tests. Name anything you intend to change
   that this brief did not ask for, and why.
3. Implement the smallest coherent diff that fully satisfies the WP.
4. Run `cd backend && python -m pytest -q` and paste the tail of the output.
5. Verify the no-key path by hand: with every API key unset,
   `python -c "import main"` from `backend/`, and confirm a mission still runs.
6. Run `git diff --stat` and confirm you touched nothing outside §0's ownership
   list.
7. One commit on `wp7-hardening`:
   `WP7: sandboxing, hardening and shipping`, body explaining *why* each choice,
   with the test count before (155) and after.

## 7. What to report back

- The `pytest -q` tail.
- The `git diff --stat`.
- Your decision on the `workspace_root` finding in §3, and what you made the jail
  root instead.
- Anything you found that this brief got wrong about the repo. The brief was
  written from a reading of the code, and if it contradicts what you find, the
  code wins — say so rather than bending the code to match the brief.
- Anything you deliberately did not do, and why.

**Definition of done:** `docker compose up` serves a working office at
`localhost:8080`; the CI workflow is valid; `pytest -q` is green with your new
sandbox tests included; and the app still starts and completes a mission with an
empty `.env`.
