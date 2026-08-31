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

Similarity is lexical -- token overlap weighted by inverse document frequency.
That is deliberately unglamorous: it needs no embedding model, no extra
dependency and no API key, which keeps the "runs with nothing installed"
property the rest of the backend has. Swapping in embeddings later means
replacing `_similarity` and nothing else.
"""
import json
import logging
import math
import os
import re
import uuid
from collections import Counter
from typing import Any, Dict, List, Optional, Tuple

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
        self.load()

    # -------------------------------------------------------------- persistence

    def load(self):
        """Read the store. A missing or corrupt file starts an empty memory."""
        self.episodes = []
        self._df = Counter()
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
                    {"episodes": [e.model_dump() for e in self.episodes]},
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
        artifact: str = ""
    ) -> Episode:
        """Append one finished subtask to the log and persist."""
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

    def _similarity(self, query_tokens: List[str], episode: Episode) -> float:
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

    def recall(self, agent_id: str, query: str, k: Optional[int] = None) -> List[Episode]:
        """The `k` most similar things this agent has done, best first."""
        k = k or settings.memory_recall_k
        query_tokens = tokenize(query)
        scored: List[Tuple[float, Episode]] = []
        for episode in self.episodes:
            if episode.agent_id != agent_id:
                continue
            score = self._similarity(query_tokens, episode)
            if score > 0.05:
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

    def competence(self, agent_id: str, description: str) -> float:
        """
        How well this agent has historically done on work *like this*, 0..1.

        Similarity-weighted success rate, pulled toward 0.5 when the evidence is
        thin so three lucky runs cannot outrank a specialist's whole record.
        """
        query_tokens = tokenize(description)
        weighted_success = 0.0
        weight_total = 0.0
        for episode in self.episodes:
            if episode.agent_id != agent_id:
                continue
            similarity = self._similarity(query_tokens, episode)
            if similarity <= 0.05:
                continue
            weight_total += similarity
            if episode.outcome == "success":
                weighted_success += similarity

        if weight_total == 0.0:
            return 0.5

        raw = weighted_success / weight_total
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
