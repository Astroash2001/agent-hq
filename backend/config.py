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

    # API Keys (loaded from .env or OS environment)
    gemini_api_key: str = os.getenv("GEMINI_API_KEY", "")
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
    anthropic_api_key: str = os.getenv("ANTHROPIC_API_KEY", "")

    # Safety Guardrails
    max_agent_steps: int = 8  # Step capper to prevent runaway infinite loops
    max_tokens_budget_per_task: int = 150000

    # Planning (P1)
    # The planner asks the model for a DAG; heuristic buckets are the fallback.
    planner_enabled: bool = True
    planner_max_steps: int = 5
    planner_temperature: float = 0.1
    planner_timeout: float = 20.0

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

# Sync litellm / os environment variables if provided
if settings.gemini_api_key and not os.environ.get("GEMINI_API_KEY"):
    os.environ["GEMINI_API_KEY"] = settings.gemini_api_key
if settings.openai_api_key and not os.environ.get("OPENAI_API_KEY"):
    os.environ["OPENAI_API_KEY"] = settings.openai_api_key
if settings.anthropic_api_key and not os.environ.get("ANTHROPIC_API_KEY"):
    os.environ["ANTHROPIC_API_KEY"] = settings.anthropic_api_key
