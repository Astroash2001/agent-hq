import datetime as _dt
import os

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "AgentHQ Multi-Agent Harness"
    environment: str = "development"
    debug: bool = True

    # Server settings
    host: str = "0.0.0.0"
    port: int = 8080

    # LLM Settings
    # Default model on Experiential Labs gateway
    default_model: str = "openai/deepseek-v4-flash"
    fallback_model: str = "openai/gpt-5.6-luna"
    temperature: float = 0.2
    # Output ceiling per call. 2048 suited a non-reasoning model; a reasoning
    # model spends this budget on thinking BEFORE it emits any text, so a hard
    # turn returns finish_reason="length" with empty content and no error.
    # Observed on gpt-5.5: a research turn carrying three tool results burned
    # the whole 2048 on reasoning and produced nothing, which the agent loop
    # then filed as a 39-byte stub.
    max_tokens: int = 16000
    # Per-call ceiling. Was hardcoded at 12s in base_agent.py, which suited a
    # fast non-thinking model. Reasoning models think before answering and a
    # 12s cap cuts them off mid-thought -- and the failure is silent, because
    # the exception handler falls through to the simulator. 90s leaves room;
    # lower it for a fast model if you want the fallback to kick in sooner.
    llm_timeout: float = 90.0

    # --- retry ---------------------------------------------------------------
    # A transient provider failure used to kill the whole mission: nothing
    # retried, so one 429 ended the run. llm.py retries the transient cases
    # only -- a 401 or a 404 still fails on the first attempt, because those
    # will fail identically forever and delaying the error only hides it.
    llm_max_retries: int = 3
    llm_backoff_base: float = 1.5      # seconds; doubles each attempt
    llm_backoff_max: float = 30.0      # ceiling for the computed backoff
    # A provider that names its own delay is honoured up to this. Gemini asks
    # for 28s on the free tier, which is worth waiting; an absurd value is not.
    llm_retry_max_wait: float = 60.0
    # After retries are spent, try settings.fallback_model once. Because
    # litellm picks the provider from the model prefix, this crosses providers:
    # an OpenAI primary can fall back to Gemini.
    llm_fallback_enabled: bool = True

    # --- web search ----------------------------------------------------------
    # Tavily backs the web_search tool. Without a key the tool says plainly
    # that no search is available, which is what lets an agent fall back on
    # its own knowledge instead of retrying an empty tool until its step
    # budget is gone.
    tavily_api_key: str = os.getenv("TAVILY_API_KEY", "")
    # "basic" is one credit and fast; "advanced" digs deeper for two.
    search_depth: str = os.getenv("SEARCH_DEPTH", "basic")
    # Hard ceiling on searches per agent task, enforced in the ReAct loop.
    # Every result suggests another query worth trying, so without a cap an
    # agent keeps going until its step budget is gone -- eighteen searches on
    # one subtask, and a 3-step mission costing 144k tokens.
    search_max_calls: int = int(os.getenv("SEARCH_MAX_CALLS", "5"))
    # Results per search. Each one is carried in the message history and
    # re-sent on every later turn, so this multiplies through the whole step.
    search_max_results: int = int(os.getenv("SEARCH_MAX_RESULTS", "3"))
    # Tavily can return a direct synthesised answer alongside the snippets.
    search_include_answer: bool = True

    # API Keys (loaded from .env or OS environment)
    gemini_api_key: str = os.getenv("GEMINI_API_KEY", "")
    groq_api_key: str = os.getenv("GROQ_API_KEY", "")
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
    anthropic_api_key: str = os.getenv("ANTHROPIC_API_KEY", "")

    # --- Gateway routing ---------------------------------------------------
    # Set llm_api_base to send every request through an OpenAI-compatible
    # gateway (AgentRouter, OpenRouter, a self-hosted proxy) instead of to a
    # provider directly. Leave it empty and nothing changes: litellm keeps
    # resolving the provider from the model prefix, as it did before.
    #
    # With a base set, the model string needs the `openai/` prefix so litellm
    # uses its OpenAI adapter against that base -- `anthropic/...` would send
    # the request to api.anthropic.com and ignore the gateway entirely.
    llm_api_base: str = os.getenv("LLM_API_BASE", "")
    llm_api_key: str = os.getenv("LLM_API_KEY", "")

    # Some gateways gate on client identity as well as the key. AgentRouter
    # rejects unrecognised clients with 401 "unauthorized client detected" --
    # which reads like a bad key but is not; the same key works once the
    # User-Agent is one it accepts.
    llm_user_agent: str = os.getenv("LLM_USER_AGENT", "")

    # When true, a failed LLM call raises instead of dropping to the built-in
    # simulator, and a failed plan raises instead of falling back to keyword
    # buckets. Both fallbacks are good in production and actively harmful while
    # you are configuring credentials, because a 401 looks like a working run.
    llm_strict: bool = os.getenv("LLM_STRICT", "true").lower() in ("1", "true", "yes")

    # Safety Guardrails
    max_agent_steps: int = 8  # Step capper to prevent runaway infinite loops
    # Whole-mission ceiling, enforced between waves. This is runaway protection,
    # not a spend target: it should sit well above what real work costs, or it
    # stops the work instead of the runaway. 150k was set when web_search was a
    # mock; with live search a routine 3-step mission spent 144,156 of it,
    # because the ReAct loop re-sends its history -- including every search
    # result -- on every turn.
    max_tokens_budget_per_task: int = 400000

    # Planning (P1)
    # The planner asks the model for a DAG; heuristic buckets are the fallback.
    planner_enabled: bool = True
    planner_max_steps: int = 5
    # Seats at the conference table, chair included. Mirrors the capacity on the
    # conference_table station in office.py; the planner is told this number so
    # it cannot invite more people than the room holds.
    meeting_capacity: int = 4
    planner_temperature: float = 0.1

    # --- debate (WP6) --------------------------------------------------------
    # Attendees used to sit at the table while only the chair thought. Each one
    # now gets exactly one turn to object before the chair sums up: one call,
    # no tools, no ReAct loop, run concurrently because the turns are
    # independent. Off restores the silent-attendee meeting exactly.
    debate_enabled: bool = True
    # Two, so the meeting is an argument rather than a row of statements. Round
    # one is each attendee's position; every round after it is a REPLY round --
    # they are shown the transcript and told to name who they are answering and
    # where they have changed their mind. At one round `earlier` is always empty
    # and nobody replies to anybody, which is a meeting in shape only.
    #
    # The cost is linear and small: rounds x seats x debate_max_tokens, and a
    # turn is three sentences. Set it back to 1 for the cheaper silent version.
    debate_rounds: int = 2
    # A per-turn output ceiling, and the reason the round is affordable. An
    # objection is three sentences; the meeting-wide cost is this times the
    # number of seats times the number of rounds, which is the arithmetic the
    # default keeps small.
    debate_max_tokens: int = 350
    # The planner makes one call and it gates the whole mission, so it gets the
    # same headroom as an agent step. At 20s a thinking model falls back to the
    # keyword buckets before it has finished answering -- which looks exactly
    # like the model ignoring you.
    planner_timeout: float = 60.0

    # Scheduling (P2)
    # Independent steps run together; the cap keeps the office readable and
    # stops a wide plan from opening a dozen provider connections at once.
    parallel_enabled: bool = True
    max_parallel_agents: int = 3

    # Spatial runtime (P3)
    # Tools are bound to stations and agents must walk to them. Turning this off
    # restores the old behaviour where any tool is callable from any desk.
    spatial_enabled: bool = True
    walk_seconds_per_tile: float = 0.06
    min_walk_seconds: float = 0.25
    max_walk_seconds: float = 2.0
    station_wait_timeout: float = 45.0

    # --- mission output ------------------------------------------------------
    # Every mission leaves a directory behind: its artifacts, report.md, and a
    # mission.json describing the run. Without this the workspace was purely
    # in-memory and the result only ever existed in the open browser tab.
    persist_missions: bool = True
    missions_path: str = os.path.join(os.path.dirname(__file__), "data", "missions")

    # Files a user drops with a task, before a mission exists to own them. They
    # are copied into the mission workspace at dispatch and the staged copy is
    # what gets cleaned up, so a browser that uploads and then never submits
    # leaves rubbish here rather than in a half-made mission directory.
    attachments_path: str = os.path.join(
        os.path.dirname(__file__), "data", "attachments")
    # Per file. An agent reads an attachment as text into its prompt, and a
    # prompt is the thing that costs money -- 2MB of text is roughly 500k
    # tokens, which is more than the whole mission budget.
    max_attachment_bytes: int = 2 * 1024 * 1024
    # How much of an attachment reaches the agent's prompt. Same reasoning and
    # the same number as workspace.MAX_INJECTED_CHARS: long enough to carry the
    # substance, short enough that four of them do not blow the context window.
    max_attachment_chars: int = 12000

    # --- telemetry -----------------------------------------------------------
    # Every mission also leaves a trace.jsonl beside its artifacts: one span per
    # mission, step, turn, tool call and model call. On by default because it
    # needs no credential, no service and no extra package -- and because the
    # numbers it carries (real tokens, real cost, real latency) are the ones the
    # HUD was previously inventing.
    tracing_enabled: bool = True
    # Span attributes are written on every span. Without a ceiling an agent's
    # reply or a whole search result ends up in the trace, which then costs more
    # to read than the artifacts it describes.
    trace_max_attr_chars: int = 400
    # Optional mirror to an OTLP collector. Off by default: it needs a collector
    # running somewhere and the opentelemetry packages, which are in
    # requirements-dev.txt only and imported lazily, so neither is needed to run.
    otel_enabled: bool = False
    otel_endpoint: str = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "")

    # --- durable execution (WP4) ---------------------------------------------
    # Every mission and every step transition is written to a SQLite file before
    # the matching event is emitted, so a process killed mid-mission leaves a
    # record of exactly how far it got and can be resumed instead of re-run.
    # stdlib sqlite3, so this adds no dependency and no service.
    runstore_enabled: bool = True
    runstore_path: str = os.path.join(os.path.dirname(__file__), "data", "runs.db")
    # Ceiling on one step, enforced with asyncio.wait_for. A model that will not
    # stop, or a station that never frees, used to hang the whole graph behind
    # it with nothing in the UI to say why. A timeout is recorded as a step
    # failure -- which matters, because failure recording is what makes the
    # competence rerouting in memory.py real.
    step_timeout_sec: float = 300.0
    # Total attempts at a step whose failure looks transient (llm.is_transient
    # decides, so there is one classifier rather than two that drift apart). A
    # step that failed because the agent produced garbage is contracts.py and
    # critic.py's problem, not a retry candidate, and a step that exhausted
    # step_timeout_sec has already spent the time budget -- neither is retried.
    step_max_attempts: int = 2

    # --- artifact quality (WP2) ----------------------------------------------
    # Steps used to return free text that nothing ever checked, which is why
    # `_best_artifact` and the "Deliverable:" instruction in _run_step exist --
    # both make a good artifact likelier, neither verifies one. Contracts are
    # regex-cheap and always on; the critic costs a model call and is skipped
    # when no credential is configured.
    contracts_enabled: bool = True
    critic_enabled: bool = True
    # Empty means settings.fallback_model -- the small, cheap one. Criticism
    # that costs more than the work is not worth having.
    critic_model: str = ""
    # The briefing's closing verdict used to be a fixed sentence -- "System
    # state is healthy and ready for the next command" -- printed whether the
    # mission had answered the goal or produced four empty files. This asks a
    # small model to draw the conclusion from the artifacts instead. One call,
    # no tools, and skipped entirely without a credential, which leaves the
    # deterministic step-count line exactly as it was.
    synthesis_enabled: bool = True
    # Empty means settings.fallback_model.
    synthesis_model: str = ""
    # A conclusion, not a second report. The artifacts are already in the
    # briefing directly below it, so anything longer is restating them.
    # Enough for a lead table plus its explanation. At 600 a conclusion that
    # opened with a four-row table was cut mid-word, and the cut is the first
    # thing a reader sees. `synthesis._whole_sentences` trims any fragment that
    # still gets through.
    synthesis_max_tokens: int = 1000
    # The eval suite's rubric grader (evals/judge.py). Fixed and separate from
    # the models under test: grading a model with itself measures its
    # self-consistency, not the quality of its output. Empty means
    # settings.fallback_model.
    judge_model: str = ""
    # EXTRA dispatches of a failed step, beyond the first. The cap is the
    # feature: an unbounded repair loop is how an agent system spends a whole
    # mission budget on one step. Shared across contract and critic failures,
    # so a step cannot be repaired twice by taking one from each.
    max_repair_attempts: int = 1

    # Memory (P6)
    memory_enabled: bool = True
    memory_path: str = os.path.join(os.path.dirname(__file__), "data", "memory.json")
    memory_recall_k: int = 3
    memory_max_episodes: int = 500
    # Routing only overrides the planner once the store has this many episodes,
    # and only when the challenger beats the pick by this margin.
    memory_min_episodes_for_routing: int = 6
    memory_reroute_margin: float = 0.12
    # Similarity weight at which a competence score is fully trusted.
    memory_confidence_weight: float = 2.0

    # --- semantic memory (WP5) -----------------------------------------------
    # "hash" is a deterministic hashed bag of words and character trigrams: no
    # key, no network, no dependency, identical across processes. "litellm"
    # uses a real embedding model over the credentials already configured, and
    # is the only one of the two that understands synonymy. "none" keeps the
    # old IDF-weighted token overlap. The default is the one that needs
    # nothing, so a fresh clone behaves the same as it always did.
    embedding_provider: str = os.getenv("EMBEDDING_PROVIDER", "hash")
    embedding_model: str = os.getenv("EMBEDDING_MODEL", "gemini/text-embedding-004")
    # Buckets in the hashed vector. 256 keeps collisions rare enough at the
    # 500-episode store cap while staying cheap to cosine over.
    embedding_dim: int = 256
    # A whole word is worth more than one of its trigrams, or a long word
    # would outweigh an exact match on a short one purely by having more
    # substrings. 1.0 / 0.5 was picked by measuring paraphrase separation
    # against unrelated text on the episodes already in data/memory.json.
    embedding_word_weight: float = 1.0
    embedding_ngram_weight: float = 0.5
    # Vectors are cached in memory for every provider and written to this file
    # only for the ones that cost a call -- a hashed vector is 0.17ms of
    # arithmetic, and persisting 2000 of them is six megabytes of derived JSON
    # rewritten every time a new episode lands.
    embedding_cache_path: str = os.path.join(os.path.dirname(__file__), "data", "embeddings.json")
    embedding_cache_max: int = 2000

    # Similarity below this is not recall, it is noise. The lexical scorer
    # returned a hard 0.0 for anything with no shared token, so its 0.05
    # threshold meant "any overlap at all"; a cosine over hashed n-grams always
    # returns something, so the floor has to be measured rather than assumed.
    # On this repo's own episodes: unrelated pairs land at 0.00-0.08, a
    # paraphrase sharing no token at all lands at 0.18, and one sharing a word
    # or two at 0.38-0.61. 0.15 sits in the gap.
    memory_similarity_floor: float = 0.15
    # How much of a competence score is *how well* the work was done rather
    # than whether it ran. 0 reproduces the old success-rate-only behaviour;
    # only episodes the critic actually scored contribute, so a store with no
    # quality data scores exactly as it did before whatever this is set to.
    memory_quality_weight: float = 0.4
    # An episode's weight halves every this many days, so an agent that has
    # improved is not judged forever on its first week. Set very high to
    # disable decay.
    memory_half_life_days: float = 30.0

    # Paths
    agents_dir: str = os.path.join(os.path.dirname(__file__), "agents")

    # --- Sandboxing and hardening (WP7) --------------------------------------
    # The jail root is the repository root (one level up from backend/). An
    # earlier `workspace_root` pointed one level higher still, at the parent of
    # the repo, which would have leaked sibling projects into any path check --
    # it was replaced rather than fixed, and is gone. jail_root restricts path
    # traversal and file operations to this repository.
    #
    # There is no code-execution tool, so no terminal allowlist either: see
    # `tool_registry._register_default_tools` for why both were removed.
    jail_root: str = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

    # File ops path containment kill switch.
    file_ops_sandbox_enabled: bool = True
    # Inside the jail but still not an agent's to read (`file_ops.denied_reason`).
    # File names that hold secrets, matched case-insensitively on the basename.
    read_denied_names: list[str] = [".env", ".env.*", "*.pem", "*.key"]
    # Directories under jail_root. `evals` holds ground-truth answers and judge
    # rubrics: a mission that can read the answer key validates against it
    # instead of doing the work, which is what a recorded run was seen to do.
    read_denied_dirs: list[str] = [".git", "evals"]
    # The run store, memory and embedding cache are denied by their own paths,
    # and so is every mission directory but the one running.

    # --- fetch_url: outbound requests to an address the model chooses --------
    # A guardrail class the harness did not have. Every other outbound call
    # goes to a host this code names (Tavily, the LLM provider); this one goes
    # where an agent says. See tools/fetch_url.py for why each of these exists.
    fetch_url_timeout: float = 15.0
    fetch_url_max_chars: int = 20000
    # Every hop is re-checked against the address policy, so this is a cost
    # bound rather than a safety one.
    fetch_url_max_redirects: int = 5
    fetch_url_user_agent: str = "AgentHQ/1.0 (+https://github.com/agent-hq)"

    # --- query_data ---------------------------------------------------------
    # Rows returned to the agent. Its reply history is re-sent every turn, so
    # this is a token budget as much as a result cap.
    query_data_max_rows: int = 200
    # Rows loaded from the file into the in-memory table. Well above the result
    # cap because an aggregate over 50k rows should still be right.
    query_data_max_source_rows: int = 50000

    # --- the foundry: hiring a specialist mid-mission -----------------------
    # Off by default. A roster that changes itself is a real change in what the
    # harness does, and it should be switched on deliberately rather than
    # discovered.
    foundry_enabled: bool = True
    # Hard ceiling on contractors per mission. The office also has only two
    # spare desks (office_map.js `spawnSeats`), and the smaller of the two
    # wins -- so this cannot quietly seat more people than the room holds.
    max_spawned_agents: int = 2
    # The design call writes the contractor's identity, never its tools. Off
    # falls to the deterministic design, which is also what runs with no key.
    foundry_design_enabled: bool = True
    # Empty means settings.fallback_model -- the small one, like the critic.
    foundry_model: str = ""
    foundry_max_tokens: int = 400

    # --- describe_image: the one tool whose implementation is a model call ---
    # Empty means default_model. Set VISION_MODEL when the default cannot see:
    # the failure is a refusal from the provider, not a wrong answer, so it is
    # loud -- but it is only fixable here.
    vision_model: str = os.getenv("VISION_MODEL", "")
    describe_image_max_tokens: int = 1500
    # Checked against the BASE64 length, not the file size, because base64 is
    # what enters the prompt and it is about a third larger than the bytes.
    # 8MB encoded is roughly a 6MB image, and already an expensive question.
    describe_image_max_encoded_chars: int = 8 * 1024 * 1024

    # --- read_file ----------------------------------------------------------
    # Characters returned in one read. The agent's history is re-sent on every
    # remaining turn of a step, so this is a token budget as much as a cap --
    # but it is paired with offset/next_offset now, so a file larger than this
    # is paged rather than silently halved.
    read_file_max_chars: int = 8000

    # --- search_workspace ---------------------------------------------------
    search_workspace_max_results: int = 100
    search_workspace_max_file_bytes: int = 2_000_000

    # API authentication and network hardening (api_guard.py).
    # When set, every /api/ route but /api/health and the /ws socket require it.
    # When empty, the API is open -- which production refuses to start with.
    api_token: str = os.getenv("AGENTHQ_API_TOKEN", "")
    cors_origins: list[str] = ["http://localhost:8080"]

    # Rate limiting on WebSocket task submissions (seconds between submissions).
    # 0 or negative disables rate limiting.
    ws_submit_rate_limit: float = 1.0
    # See api_guard.py. Production with no api_token refuses to start unless
    # this is set, which is the explicit way to say "only I can reach it".
    allow_unauthenticated: bool = False
    # Missions running at once, across every connection. Each one is real
    # spend, and a per-connection rate limit does nothing against a second
    # connection. 0 means no cap.
    max_concurrent_missions: int = 1

    # Both files are absolute and anchored to this module, because a relative
    # ".env" is resolved against the process's working directory -- and only
    # `python main.py`, run from backend/, happens to have the right one. Run
    # the documented way from the repo root, `python -m evals.runner` loaded no
    # credential at all: every task fell to the simulator, the judge reported
    # "no model configured", and no cassette could ever be recorded.
    #
    # backend/.env is listed last because the later file wins, which keeps the
    # behaviour of a run started from backend/ exactly as it was.
    model_config = SettingsConfigDict(
        env_file=(
            os.path.join(os.path.dirname(__file__), "..", ".env"),
            os.path.join(os.path.dirname(__file__), ".env"),
        ),
        env_file_encoding="utf-8",
        extra="ignore"
    )


settings = Settings()


def llm_route() -> dict:
    """
    The routing kwargs every litellm call needs, in one place.

    Returns {} when no gateway is configured, so callers can splat it
    unconditionally and behave exactly as they did before.
    """
    if not settings.llm_api_base:
        return {}
    route = {
        "api_base": settings.llm_api_base,
        "api_key": (settings.llm_api_key or settings.anthropic_api_key
                    or settings.openai_api_key or settings.gemini_api_key
                    or settings.groq_api_key),
    }
    if settings.llm_user_agent:
        # "User-Agent" exactly -- litellm forwards extra_headers into the
        # OpenAI SDK, whose own default header wins on a case mismatch. A
        # lowercase "user-agent" here is silently dropped and the gateway
        # answers 401. Verified against litellm 1.81.10.
        route["extra_headers"] = {"User-Agent": settings.llm_user_agent}
    return route


def date_context() -> str:
    """
    Today's date, as a line to prepend to a system prompt.

    A model has no idea what day it is -- its sense of "now" is its training
    cutoff. Asked what happened in the news today, one cited a Reuters article
    from three weeks earlier and headed it "Today", because nothing in the
    conversation gave it a date to measure against.

    This states the fact and stops there. It deliberately does not tell the
    model what to do with it: whether a request needs recent sources, and how
    recent, is the model's judgement to make from the user's actual words --
    not something the harness should infer by looking for words like "today".

    Local system date rather than UTC, because "today" for the person at the
    keyboard is their today.
    """
    stamp = _dt.datetime.now().strftime("%A, %d %B %Y")
    return f"Today's date is {stamp}."


def llm_configured() -> bool:
    """True when any credential is available, gateway or direct provider."""
    return bool(settings.llm_api_key or settings.gemini_api_key
                or settings.openai_api_key or settings.anthropic_api_key
                or settings.groq_api_key)



# Sync litellm / os environment variables if provided
if settings.gemini_api_key and not os.environ.get("GEMINI_API_KEY"):
    os.environ["GEMINI_API_KEY"] = settings.gemini_api_key
if settings.openai_api_key and not os.environ.get("OPENAI_API_KEY"):
    os.environ["OPENAI_API_KEY"] = settings.openai_api_key
if settings.anthropic_api_key and not os.environ.get("ANTHROPIC_API_KEY"):
    os.environ["ANTHROPIC_API_KEY"] = settings.anthropic_api_key
if settings.groq_api_key and not os.environ.get("GROQ_API_KEY"):
    os.environ["GROQ_API_KEY"] = settings.groq_api_key
