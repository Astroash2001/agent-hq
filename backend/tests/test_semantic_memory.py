"""
Semantic recall and quality-weighted routing.

Two claims are under test here, and they are separable.

The first is that similarity is no longer token overlap: "optimise the caching
layer" and "cache optimisation" share no token, so the lexical scorer returned
a hard 0.0 and the agent that had already done that exact work was never
recalled. The embedding path has to see them as the same job, and has to do it
with no key, no network and no dependency -- and identically in another
process, or a cached vector from yesterday would not be comparable to one
computed today.

The second is that routing now weighs *how well* work was done, not only
whether it ran. An agent that succeeded badly used to score exactly like one
that succeeded well, and an agent that has improved was judged forever on its
first week.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone

import pytest

import embeddings
from config import settings
from memory import AgentMemory, tokenize


BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture
def scratch():
    """
    A directory of our own.

    tempfile rather than pytest's tmp_path, for the reason the other suites
    give: on this machine tmp_path errors with WinError 5 on its base dir.
    """
    path = tempfile.mkdtemp(prefix="agenthq_memory_")
    yield path
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def store(scratch):
    """A memory of its own, on the hash provider, with nothing cached."""
    saved = {
        "embedding_provider": settings.embedding_provider,
        "embedding_cache_path": settings.embedding_cache_path,
        "memory_quality_weight": settings.memory_quality_weight,
        "memory_half_life_days": settings.memory_half_life_days,
        "memory_min_episodes_for_routing": settings.memory_min_episodes_for_routing,
    }
    settings.embedding_provider = "hash"
    settings.embedding_cache_path = os.path.join(scratch, "embeddings.json")
    embeddings.clear_cache()

    memory = AgentMemory(path=os.path.join(scratch, "memory.json"))
    yield memory

    for field, value in saved.items():
        setattr(settings, field, value)
    embeddings.clear_cache()


def _record(memory, agent_id, subtask, outcome="success", quality=None, age_days=0.0):
    episode = memory.record(
        agent_id=agent_id,
        agent_name=agent_id.title(),
        goal="Ship the release",
        subtask=subtask,
        outcome=outcome,
        quality=quality,
    )
    if age_days:
        stamp = datetime.now(timezone.utc) - timedelta(days=age_days)
        episode.timestamp = stamp.replace(tzinfo=None).isoformat()
    return episode


# ------------------------------------------------------------------ the vectors

def test_the_hash_provider_is_identical_in_another_process():
    """
    `hash()` is salted per process, so a vector built with it would not survive
    a restart -- let alone a machine. The provider uses SHA-256 for exactly
    this reason, and this is the assertion that keeps it that way.
    """
    here = embeddings._hash_embed("optimise the caching layer")
    code = (
        "import sys, json; sys.path.insert(0, sys.argv[1]);"
        "import embeddings;"
        "print(json.dumps(embeddings._hash_embed('optimise the caching layer')))"
    )
    out = subprocess.run(
        [sys.executable, "-c", code, BACKEND],
        capture_output=True, text=True, timeout=120,
    )
    assert out.returncode == 0, out.stderr
    there = json.loads(out.stdout.strip().splitlines()[-1])
    assert there == pytest.approx(here)


def test_a_paraphrase_scores_far_above_unrelated_work(store):
    paraphrase = store._similarity(
        tokenize("Optimise the caching layer for the API"),
        _record(store, "coder", "Cache optimisation for the API service"),
    )
    unrelated = store._similarity(
        tokenize("Optimise the caching layer for the API"),
        _record(store, "coder", "Design a poster for the summer festival"),
    )
    assert paraphrase >= settings.memory_similarity_floor
    assert unrelated < settings.memory_similarity_floor
    assert paraphrase > unrelated * 2


def test_embeddings_catch_what_the_lexical_scorer_scored_at_zero(store):
    """The whole point of the work package, as one assertion."""
    episode = _record(store, "coder", "cache optimisation")
    query = tokenize("optimise the caching layer")

    lexical = store._lexical_similarity(query, episode)
    semantic = store._similarity(query, episode)

    assert lexical == 0.0                                  # no shared token
    assert semantic >= settings.memory_similarity_floor    # same job, though


def test_an_unavailable_provider_falls_back_to_the_lexical_scorer(store):
    episode = _record(store, "coder", "profile and speed up the JSON parser")
    query = tokenize("speed up the JSON parser")

    settings.embedding_provider = "none"
    embeddings.clear_cache()
    store._query_vectors.clear()
    episode.embedding = None

    assert embeddings.available() is False
    assert store._similarity(query, episode) == store._lexical_similarity(query, episode)
    assert store._similarity(query, episode) > 0.0


def test_the_cache_is_hit_on_a_second_identical_call(store):
    embeddings.clear_cache()
    first = embeddings.embed(["cache optimisation", "cache optimisation"])
    # Two identical texts in one call: one miss, one hit.
    assert embeddings.stats["misses"] == 1
    assert embeddings.stats["hits"] == 1

    second = embeddings.embed(["cache optimisation"])
    assert embeddings.stats["misses"] == 1      # nothing new was embedded
    assert embeddings.stats["hits"] == 2
    assert second[0] == first[0]


# -------------------------------------------------------------- the old store

def test_an_old_store_loads_and_backfills(scratch):
    """
    A store written before `quality` and `embedding` existed has neither field.
    It must load, and it must not come back claiming every old episode was
    perfect -- an unscored episode is not a scored one.
    """
    path = os.path.join(scratch, "memory.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"episodes": [
        {
            "id": "ep_old1",
            "agent_id": "coder",
            "agent_name": "Bob",
            "goal": "Ship the release",
            "subtask": "Optimise the caching layer for the API",
            "outcome": "success",
            "summary": "done",
            "duration_sec": 3.0,
            "total_tokens": 400,
            "tools_used": ["read_file"],
            "artifact": "notes.md",
            "tokens": tokenize("Ship the release Optimise the caching layer for the API"),
            "timestamp": datetime.utcnow().isoformat(),
        }
    ]}, f)

    memory = AgentMemory(path=path)
    assert len(memory.episodes) == 1
    assert memory.episodes[0].quality is None
    assert memory.episodes[0].embedding is None

    recalled = memory.recall("coder", "cache optimisation")
    assert [e.id for e in recalled] == ["ep_old1"]
    assert memory.episodes[0].embedding, "recall should have backfilled the vector"

    # And the backfilled vector stays out of the file it was never in.
    memory.save()
    with open(path, "r", encoding="utf-8") as f:
        written = json.load(f)
    assert "embedding" not in written["episodes"][0]


# ----------------------------------------------------------------- the routing

def test_quality_separates_two_agents_that_both_succeeded(store):
    """`outcome` says the step ran. `quality` says whether it was any good."""
    for _ in range(4):
        _record(store, "coder", "Write the release notes", quality=0.30)
        _record(store, "writer", "Write the release notes", quality=0.95)

    assert store.competence("writer", "Write the release notes") > \
           store.competence("coder", "Write the release notes")

    # With the quality term switched off, both are perfect records and there is
    # nothing to choose between them -- which is exactly the old behaviour.
    settings.memory_quality_weight = 0.0
    assert store.competence("writer", "Write the release notes") == \
           pytest.approx(store.competence("coder", "Write the release notes"))


def test_a_better_agent_takes_the_step_only_past_the_margin(store):
    candidates = ["coder", "writer"]
    task = "Write the release notes"

    # Close: better, but not by the margin. The plan stands.
    for _ in range(4):
        _record(store, "coder", task, quality=0.60)
        _record(store, "writer", task, quality=0.85)
    assert store.competence("writer", task) > store.competence("coder", task)
    assert store.suggest_agent(task, candidates, current="coder") is None

    # Clear: the record now says something worth overriding the planner for.
    for _ in range(6):
        _record(store, "writer", task, quality=1.0)
        _record(store, "coder", task, outcome="failure", quality=0.15)
    suggestion = store.suggest_agent(task, candidates, current="coder")
    assert suggestion is not None
    new_id, new_score, old_score = suggestion
    assert new_id == "writer"
    assert new_score - old_score >= settings.memory_reroute_margin


def test_an_old_record_weighs_less_than_a_recent_one(store):
    """An agent that has improved should not be judged forever on its first week."""
    task = "Write the release notes"
    for _ in range(4):
        _record(store, "writer", task, quality=1.0, age_days=180)   # six half-lives
        _record(store, "coder", task, quality=0.55)                 # today

    settings.memory_half_life_days = 30.0
    with_decay = (store.competence("writer", task), store.competence("coder", task))

    # Same episodes, no decay: the old perfect record wins on merit alone.
    settings.memory_half_life_days = 1_000_000.0
    store._query_vectors.clear()
    without_decay = (store.competence("writer", task), store.competence("coder", task))

    assert without_decay[0] > without_decay[1]
    assert with_decay[0] < without_decay[0]
    assert with_decay[0] < with_decay[1]


def test_routing_fires_on_a_paraphrase_the_lexical_scorer_missed(store, capsys):
    """
    The acceptance case, end to end.

    Bob has optimised this cache four times. The planner hands the paraphrased
    version of that job to Eve. Under token overlap the two descriptions score
    0.0 against each other, Bob has no relevant history, and nothing reroutes.
    """
    task = "Optimise the caching layer"
    for _ in range(4):
        _record(store, "coder", "Cache optimisation for the checkout service", quality=0.9)
    for _ in range(4):
        _record(store, "creative", "Draft the launch blog post", quality=0.9)

    episode = store.episodes[0]
    lexical = store._lexical_similarity(tokenize(task), episode)
    semantic = store._similarity(tokenize(task), episode)

    suggestion = store.suggest_agent(task, ["coder", "creative"], current="creative")
    assert suggestion is not None
    assert suggestion[0] == "coder"

    # Same store, same task, scored the old way: no relevant history, no reroute.
    settings.embedding_provider = "none"
    embeddings.clear_cache()
    store._query_vectors.clear()
    for e in store.episodes:
        e.embedding = None
    assert store.suggest_agent(task, ["coder", "creative"], current="creative") is None

    print(f"\n'{task}'\n  vs '{episode.subtask}'"
          f"\n  lexical  {lexical:.3f}  -> no recall, no reroute"
          f"\n  semantic {semantic:.3f}  -> rerouted to {suggestion[0]} "
          f"({suggestion[2]:.3f} -> {suggestion[1]:.3f})")
