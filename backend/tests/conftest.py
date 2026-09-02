"""
Test-session setup.

The suite is meant to be hermetic: no network, no credentials, deterministic
output. It exercises the orchestration -- routing, the DAG, the workspace, the
office floor -- not the model, and the built-in simulator gives every agent a
stable reply to orchestrate.

Two things had to be pinned for that to hold once gateway routing arrived:

  * Credentials are cleared, so `llm_configured()` is False and no request is
    ever made. Without this the suite calls the real gateway on every agent
    step -- slow, and it fails outright when the account is out of credit
    (observed: four tests failing on HTTP 402 "Budget pool quota exhausted",
    which says nothing about the code under test).

  * `llm_strict` is switched off, because these tests deliberately run the
    simulator path. Strict mode exists to stop a misconfigured gateway hiding
    behind that same fallback during development, which is the opposite
    situation.
"""
import os

import pytest

from config import settings


@pytest.fixture(autouse=True, scope="session")
def _scratch_missions():
    """
    Keep the suite out of the real mission directory.

    The phase tests run whole missions, and once the workspace started
    mirroring artifacts to disk those runs began littering
    `backend/data/missions/` with fixture output that is indistinguishable
    from a real run.
    """
    import shutil
    import tempfile

    root = tempfile.mkdtemp(prefix="agenthq_test_missions_")
    original = settings.missions_path
    settings.missions_path = os.path.join(root, "missions")
    yield
    settings.missions_path = original
    shutil.rmtree(root, ignore_errors=True)


@pytest.fixture(autouse=True, scope="session")
def _offline_llm():
    """Force every test onto the offline simulator path."""
    saved = {
        "llm_api_key": settings.llm_api_key,
        "llm_api_base": settings.llm_api_base,
        "gemini_api_key": settings.gemini_api_key,
        "openai_api_key": settings.openai_api_key,
        "anthropic_api_key": settings.anthropic_api_key,
        # Cleared for the same reason as the model keys: with a real Tavily key
        # present the suite would make live searches and spend the account's
        # monthly quota to assert things that do not depend on the network.
        "groq_api_key": settings.groq_api_key,
        "tavily_api_key": settings.tavily_api_key,
        "llm_strict": settings.llm_strict,
    }
    saved_env = {k: os.environ.pop(k, None) for k in
                 ("LLM_API_KEY", "GEMINI_API_KEY", "OPENAI_API_KEY",
                  "ANTHROPIC_API_KEY", "GROQ_API_KEY", "TAVILY_API_KEY")}

    for field in ("llm_api_key", "llm_api_base", "gemini_api_key",
                  "openai_api_key", "anthropic_api_key", "groq_api_key",
                  "tavily_api_key"):
        setattr(settings, field, "")
    settings.llm_strict = False

    yield

    for field, value in saved.items():
        setattr(settings, field, value)
    for key, value in saved_env.items():
        if value is not None:
            os.environ[key] = value
