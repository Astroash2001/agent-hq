"""
Episodic memory and adaptive routing.

Every subtask an agent finishes is written down: what it was asked, what it
produced, whether it worked, how long it took, what it cost. Two things use that
record.

1. **Recall.** When an agent picks up a task, the most similar things it has done
   before are pasted into its prompt, so it starts from experience rather than
   from nothing.
2. **Routing.** The planner still chooses who does what, but a step can be
   re-pointed at a different specialist when the record says that one is
   measurably better at this kind of work. Routing stops being a fixed mapping
   and starts being something the office learns.

Similarity used to be lexical -- token overlap weighted by inverse document
frequency -- which could not tell that "optimise the caching layer" and "cache
optimisation" are the same task: they share no token, so the score was exactly
0.0. It is now a cosine over embeddings (see `embeddings.py`), with that
lexical scorer kept as the fallback for when embeddings are unavailable. The
promise made when the lexical version was written held: `_similarity` was the
only thing that had to change.

The default embedding provider is the hashed one, so this still needs no
dependency and no API key.

Routing weighs three things rather than one. Whether a step *ran* (`outcome`)
was all the record ever held, so an agent that technically succeeded badly
scored like one that succeeded well; `quality` from the critic now carries the
difference, and an episode's weight halves every `memory_half_life_days`, so an
agent that has improved is not judged forever on its first week.
"""
import json
import logging
import math
import os
import re
import uuid
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import embeddings
from config import settings
from schemas import Episode

logger = logging.getLogger("AgentHQ.Memory")


_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "how", "in",
    "into", "is", "it", "of", "on", "or", "that", "the", "then", "this", "to",
    "with", "using", "use", "your", "you", "we", "i", "please", "make", "get",
    "run", "do", "can", "should", "will", "its", "their", "there", "these",
    "task", "step", "work", "output", "result", "results", "based",
}

_TOKEN_RE = re.compile(r"[a-z0-9_]+")


def tokenize(text: str) -> List[str]:
    """Lowercase word tokens, stopwords and one-character noise removed."""
    return [
        t for t in _TOKEN_RE.findall((text or "").lower())
        if len(t) > 2 and t not in _STOPWORDS
    ]


class AgentMemory:
    """
    A flat, append-only episode log with a JSON file behind it.

    Small on purpose. The store is capped at `memory_max_episodes` and trimmed
    oldest-first, so it stays a fast in-process index rather than something that
    needs a database.
    """

    def __init__(self, path: Optional[str] = None):
        self.path = path or settings.memory_path
        self.episodes: List[Episode] = []
        self._df: Counter = Counter()   # token -> number of episodes containing it
        # One recall scores the query against every episode this agent owns, so
        # the query text would otherwise be embedded a few hundred times per
        # call. Keyed by the token tuple; bounded because a long-lived server
        # sees a new query on every step.
        self._query_vectors: Dict[Tuple[str, ...], List[float]] = {}
        self.load()

    # -------------------------------------------------------------- persistence

    def load(self):
        """
        Read the store. A missing or corrupt file starts an empty memory.

        A store written before `quality` and `embedding` existed has neither
        field, and must still load: both are optional on `Episode`, so an old
        episode arrives with `quality=None` (unscored, which is not the same as
        perfect) and `embedding=None` (backfilled on first use, from the text
        that is already there). Nothing migrates the file on disk.
        """
        self.episodes = []
        self._df = Counter()
        self._query_vectors = {}
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                payload = json.load(f)
            for item in payload.get("episodes", []):
                try:
                    self.episodes.append(Episode(**item))
                except Exception:
                    continue
            self._reindex()
            logger.info(f"Loaded {len(self.episodes)} episodes from {self.path}")
        except Exception as e:
            logger.warning(f"Could not read memory store {self.path}: {e}. Starting empty.")

    def save(self):
        """Write the store atomically so a crash mid-write cannot corrupt it."""
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = f"{self.path}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(
                    # `embedding` is excluded on purpose -- see the field's
                    # comment in schemas.py. It is derived data with its own
                    # content-addressed cache; writing it here would double the
                    # size of the store to save arithmetic that takes
                    # microseconds.
                    {"episodes": [e.model_dump(exclude={"embedding"})
                                  for e in self.episodes]},
                    f, ensure_ascii=False, indent=1
                )
            os.replace(tmp, self.path)
        except Exception as e:
            logger.warning(f"Could not write memory store {self.path}: {e}")

    def _reindex(self):
        self._df = Counter()
        for episode in self.episodes:
            for token in set(episode.tokens):
                self._df[token] += 1

    # ------------------------------------------------------------------ writing

    def record(
        self,
        agent_id: str,
        agent_name: str,
        goal: str,
        subtask: str,
        outcome: str,
        summary: str = "",
        duration_sec: float = 0.0,
        total_tokens: int = 0,
        tools_used: Optional[List[str]] = None,
        artifact: str = "",
        quality: Optional[float] = None
    ) -> Episode:
        """
        Append one finished subtask to the log and persist.

        `quality` is the critic's score for the artifact, or None when nothing
        judged this run -- critic disabled, or no credential. None rather than a
        default number on purpose: an unscored episode is not a perfect one, and
        anything that later weights routing by quality has to be able to tell
        the difference.
        """
        episode = Episode(
            id=f"ep_{uuid.uuid4().hex[:10]}",
            agent_id=agent_id,
            agent_name=agent_name,
            goal=goal,
            subtask=subtask,
            outcome=outcome,
            summary=summary[:400],
            duration_sec=round(duration_sec, 2),
            total_tokens=total_tokens,
            tools_used=tools_used or [],
            artifact=artifact,
            quality=quality,
            tokens=tokenize(f"{goal} {subtask}")
        )
        self.episodes.append(episode)
        for token in set(episode.tokens):
            self._df[token] += 1

        if len(self.episodes) > settings.memory_max_episodes:
            overflow = len(self.episodes) - settings.memory_max_episodes
            self.episodes = self.episodes[overflow:]
            self._reindex()

        self.save()
        return episode

    # ------------------------------------------------------------------ reading

    def _idf(self, token: str) -> float:
        """Rare tokens carry the signal; 'code' in every episode carries none."""
        return math.log((len(self.episodes) + 1) / (1 + self._df.get(token, 0))) + 1.0

    def _lexical_similarity(self, query_tokens: List[str], episode: Episode) -> float:
        """IDF-weighted overlap, normalised to roughly 0..1."""
        if not query_tokens or not episode.tokens:
            return 0.0
        q = set(query_tokens)
        e = set(episode.tokens)
        shared = q & e
        if not shared:
            return 0.0
        numerator = sum(self._idf(t) for t in shared)
        denominator = sum(self._idf(t) for t in q) or 1.0
        return min(numerator / denominator, 1.0)

    def _query_vector(self, query_tokens: List[str]) -> Optional[List[float]]:
        key = tuple(query_tokens)
        if key in self._query_vectors:
            return self._query_vectors[key]
        vector = embeddings.embed_one(" ".join(query_tokens))
        if vector:
            # Bounded: a server that runs for a week sees a new query on every
            # step, and this is a convenience cache, not the real one.
            if len(self._query_vectors) >= 256:
                self._query_vectors.clear()
            self._query_vectors[key] = vector
        return vector

    def _episode_vector(self, episode: Episode) -> Optional[List[float]]:
        """
        The episode's vector, embedded on first use and kept on the episode.

        Embedded from `tokens` rather than the raw text so both sides of the
        cosine see the same preprocessing -- stopwords are noise on one side or
        neither, never one side only.
        """
        if episode.embedding:
            return episode.embedding
        if not episode.tokens:
            return None
        vector = embeddings.embed_one(" ".join(episode.tokens))
        if vector:
            episode.embedding = vector
        return vector

    def _warm_vectors(self, episodes: List[Episode]):
        """
        Embed everything in `episodes` that has no vector yet, in one call.

        Scoring embeds lazily per episode, which is free for the hash provider
        and three hundred network round trips for a real one. Batching the
        backfill before a scan is what keeps a cold store from turning one
        recall into one call per episode.
        """
        if not embeddings.available():
            return
        pending = [e for e in episodes if not e.embedding and e.tokens]
        if not pending:
            return
        vectors = embeddings.embed([" ".join(e.tokens) for e in pending])
        for episode, vector in zip(pending, vectors):
            if vector:
                episode.embedding = vector

    def _similarity(self, query_tokens: List[str], episode: Episode) -> float:
        """
        How alike these two pieces of work are, 0..1.

        Cosine over embeddings when they are available, and the old lexical
        overlap when they are not -- a provider that is switched off, or one
        that failed. The signature is unchanged from the lexical-only version:
        everything that scores similarity goes through here.
        """
        if not query_tokens or not episode.tokens:
            return 0.0

        query_vector = self._query_vector(query_tokens)
        episode_vector = self._episode_vector(episode) if query_vector else None
        if query_vector and episode_vector:
            # Signed hashing can put a pair very slightly below zero; clamp so
            # this returns the same 0..1 range the lexical path does, which is
            # what every threshold in this module is keyed to.
            return max(0.0, min(1.0, embeddings.cosine(query_vector, episode_vector)))

        return self._lexical_similarity(query_tokens, episode)

    def recall(self, agent_id: str, query: str, k: Optional[int] = None) -> List[Episode]:
        """The `k` most similar things this agent has done, best first."""
        k = k or settings.memory_recall_k
        query_tokens = tokenize(query)
        mine = [e for e in self.episodes if e.agent_id == agent_id]
        self._warm_vectors(mine)
        scored: List[Tuple[float, Episode]] = []
        for episode in mine:
            score = self._similarity(query_tokens, episode)
            if score >= settings.memory_similarity_floor:
                scored.append((score, episode))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [episode for _, episode in scored[:k]]

    def render_hints(self, agent_id: str, query: str) -> str:
        """Recalled episodes formatted as a prompt block, or "" if there are none."""
        episodes = self.recall(agent_id, query)
        if not episodes:
            return ""
        lines = ["Relevant things you have done before in this office:"]
        for episode in episodes:
            verdict = "worked" if episode.outcome == "success" else "did NOT work"
            lines.append(
                f"- \"{episode.subtask[:110]}\" -- {verdict}"
                f"{', took %.1fs' % episode.duration_sec if episode.duration_sec else ''}"
                f"{'. Notes: ' + episode.summary[:160] if episode.summary else ''}"
            )
        lines.append("Reuse what worked. Do not repeat what did not.")
        return "\n".join(lines)

    # ------------------------------------------------------------------ scoring

    def stats(self, agent_id: str) -> Dict[str, Any]:
        """Lifetime record for one agent."""
        mine = [e for e in self.episodes if e.agent_id == agent_id]
        if not mine:
            return {"runs": 0, "successes": 0, "success_rate": 0.0,
                    "avg_duration_sec": 0.0, "avg_tokens": 0}
        successes = sum(1 for e in mine if e.outcome == "success")
        return {
            "runs": len(mine),
            "successes": successes,
            "success_rate": round(successes / len(mine), 3),
            "avg_duration_sec": round(sum(e.duration_sec for e in mine) / len(mine), 2),
            "avg_tokens": int(sum(e.total_tokens for e in mine) / len(mine)),
        }

    def _decay(self, episode: Episode) -> float:
        """
        Age weight: 1.0 today, 0.5 one half-life ago, and so on.

        An unparseable or missing timestamp weighs 1.0 rather than 0 -- an
        episode with a broken clock is still evidence, and quietly discarding
        it would make the record depend on a field nothing else reads.
        """
        half_life = settings.memory_half_life_days
        if half_life <= 0:
            return 1.0
        try:
            stamp = datetime.fromisoformat(episode.timestamp)
        except (TypeError, ValueError):
            return 1.0
        # Episodes are stamped with `utcnow()`, which is naive. Treat a naive
        # stamp as UTC rather than local, or every episode ages by the
        # machine's offset the moment it is written.
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        age_days = (datetime.now(timezone.utc) - stamp).total_seconds() / 86400.0
        if age_days <= 0:
            return 1.0
        return 0.5 ** (age_days / half_life)

    def competence(self, agent_id: str, description: str) -> float:
        """
        How well this agent has historically done on work *like this*, 0..1.

        Three signals, weighted by how similar and how recent each episode is:

          * whether the step ran at all (`outcome`),
          * how good the critic said the work was (`quality`),
          * how much relevant evidence there is at all.

        The first two are blended by `memory_quality_weight` -- but only over
        episodes something actually scored. An unscored episode is not a
        perfect one, so it contributes to the success rate and abstains from
        the quality term, which means a store with no critic data (no key, or
        the critic switched off) scores exactly as it did before quality
        existed, whatever the weight is set to.

        The result is pulled toward 0.5 when the evidence is thin, so three
        lucky runs cannot outrank a specialist's whole record.
        """
        query_tokens = tokenize(description)
        mine = [e for e in self.episodes if e.agent_id == agent_id]
        self._warm_vectors(mine)
        weighted_success = 0.0
        weight_total = 0.0
        weighted_quality = 0.0
        quality_weight_total = 0.0
        for episode in mine:
            similarity = self._similarity(query_tokens, episode)
            if similarity < settings.memory_similarity_floor:
                continue
            weight = similarity * self._decay(episode)
            weight_total += weight
            if episode.outcome == "success":
                weighted_success += weight
            if episode.quality is not None:
                quality_weight_total += weight
                weighted_quality += weight * max(0.0, min(1.0, episode.quality))

        if weight_total == 0.0:
            return 0.5

        raw = weighted_success / weight_total
        if quality_weight_total > 0.0:
            mean_quality = weighted_quality / quality_weight_total
            blend = settings.memory_quality_weight
            raw = (1.0 - blend) * raw + blend * mean_quality

        confidence = min(weight_total / settings.memory_confidence_weight, 1.0)
        return 0.5 + (raw - 0.5) * confidence

    def suggest_agent(
        self,
        description: str,
        candidates: List[str],
        current: str
    ) -> Optional[Tuple[str, float, float]]:
        """
        Propose a better specialist for a step, or None to leave the plan alone.

        Only fires when the challenger has enough relevant history to be worth
        trusting and beats the planner's pick by a clear margin, so a cold or
        thinly-populated memory never overrides the plan.
        """
        if not settings.memory_enabled or len(self.episodes) < settings.memory_min_episodes_for_routing:
            return None

        current_score = self.competence(current, description)
        best_id, best_score = current, current_score

        for candidate in candidates:
            if candidate == current:
                continue
            if len(self.recall(candidate, description, k=99)) < 2:
                continue  # not enough relevant history to challenge on
            score = self.competence(candidate, description)
            if score > best_score:
                best_id, best_score = candidate, score

        if best_id != current and (best_score - current_score) >= settings.memory_reroute_margin:
            return best_id, best_score, current_score
        return None

    def clear(self):
        """Wipe the store. Used by tests and the reset endpoint."""
        self.episodes = []
        self._df = Counter()
        self.save()


# Global singleton memory.
memory = AgentMemory()
