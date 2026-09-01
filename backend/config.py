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
    # Default model: gemini/gemini-2.5-flash or gemini-2.0-flash via LiteLLM
    default_model: str = "gemini/gemini-2.5-flash"
    fallback_model: str = "gemini/gemini-2.0-flash"
    temperature: float = 0.2
    max_tokens: int = 2048
    # Per-call ceiling. Was hardcoded at 12s in base_agent.py, which suited a
    # fast non-thinking model. Reasoning models think before answering and a
    # 12s cap cuts them off mid-thought -- and the failure is silent, because
    # the exception handler falls through to the simulator. 90s leaves room;
    # lower it for a fast model if you want the fallback to kick in sooner.
    llm_timeout: float = 90.0

    # API Keys (loaded from .env or OS environment)
    gemini_api_key: str = os.getenv("GEMINI_API_KEY", "")
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
    max_tokens_budget_per_task: int = 150000

    # Planning (P1)
    # The planner asks the model for a DAG; heuristic buckets are the fallback.
    planner_enabled: bool = True
    planner_max_steps: int = 5
    # Seats at the conference table, chair included. Mirrors the capacity on the
    # conference_table station in office.py; the planner is told this number so
    # it cannot invite more people than the room holds.
    meeting_capacity: int = 4
    planner_temperature: float = 0.1
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

    # Paths
    agents_dir: str = os.path.join(os.path.dirname(__file__), "agents")
    workspace_root: str = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

    model_config = SettingsConfigDict(
        env_file=".env",
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
                    or settings.openai_api_key or settings.gemini_api_key),
    }
    if settings.llm_user_agent:
        # "User-Agent" exactly -- litellm forwards extra_headers into the
        # OpenAI SDK, whose own default header wins on a case mismatch. A
        # lowercase "user-agent" here is silently dropped and the gateway
        # answers 401. Verified against litellm 1.81.10.
        route["extra_headers"] = {"User-Agent": settings.llm_user_agent}
    return route


def llm_configured() -> bool:
    """True when any credential is available, gateway or direct provider."""
    return bool(settings.llm_api_key or settings.gemini_api_key
                or settings.openai_api_key or settings.anthropic_api_key)



# Sync litellm / os environment variables if provided
if settings.gemini_api_key and not os.environ.get("GEMINI_API_KEY"):
    os.environ["GEMINI_API_KEY"] = settings.gemini_api_key
if settings.openai_api_key and not os.environ.get("OPENAI_API_KEY"):
    os.environ["OPENAI_API_KEY"] = settings.openai_api_key
if settings.anthropic_api_key and not os.environ.get("ANTHROPIC_API_KEY"):
    os.environ["ANTHROPIC_API_KEY"] = settings.anthropic_api_key
