"""
Text embeddings, with a provider that needs no key and no network.

`memory._similarity` was IDF-weighted token overlap, which cannot tell that
"optimise the caching layer" and "cache optimisation" are the same task -- they
share no token, so the score is exactly 0.0 and the record of the agent that
already did that work is never recalled.

Two providers, chosen by `settings.embedding_provider`:

  * **hash** (default) -- a deterministic hashed bag of words *and character
    trigrams*, signed-hashed into `settings.embedding_dim` buckets and
    L2-normalised. No dependency, no credential, no network, and identical
    across processes and machines, which is what keeps the eval suite and the
    "runs with nothing installed" property intact. Words alone would only
    reproduce the lexical scorer in vector form; the trigrams are what put
    `cache`/`caching` and `optimise`/`optimisation` near each other.

  * **litellm** -- a real embedding model, over the key material already
    configured. It is the only one of the two that understands synonymy
    ("speed up" ~ "accelerate"), and it costs a call per uncached text.

The honest limit of the default: hashed n-grams measure *surface* similarity,
so they catch morphology and word order but not synonyms with no letters in
common. That is still strictly more than token overlap could see, and where it
fails it degrades to roughly the old behaviour rather than to nonsense.

The cache is content-addressed (SHA-256 of provider|model|dim|text), so one
episode's text is embedded once rather than once per recall. It is held in
memory for every provider and written to disk only for the ones that cost
something: a hashed vector is 0.17ms of arithmetic, while the JSON to persist
2000 of them is six megabytes rewritten every time a new episode lands. A
litellm vector is a network round trip and a fraction of a cent, so that one is
worth keeping across restarts.
"""
import hashlib
import json
import logging
import math
import os
import re
from typing import Dict, List, Optional, Tuple

from config import settings, llm_route

logger = logging.getLogger("AgentHQ.Embeddings")

_TOKEN_RE = re.compile(r"[a-z0-9_]+")

# cache key -> vector
_cache: Optional[Dict[str, List[float]]] = None
_cache_dirty = False

# Set when the configured provider has failed once. A provider that is down or
# unauthorised will be down or unauthorised for the next 300 episodes too, and
# retrying it per episode turns one recall into 300 timeouts. Callers see an
# empty result and fall back; the warning is logged once.
_unavailable = False

# Cheap counters, read by the tests to prove the cache is actually hit.
stats = {"hits": 0, "misses": 0, "provider_calls": 0}


# ------------------------------------------------------------------- the cache

def _cache_key(text: str) -> str:
    material = (f"{settings.embedding_provider}|{settings.embedding_model}"
                f"|{settings.embedding_dim}|{text}")
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _persists() -> bool:
    """
    Whether this provider's vectors are worth a file.

    Only the ones that cost a call. Recomputing a hashed vector is cheaper than
    reading it back, and persisting them would leave megabytes of derived JSON
    to be rewritten on every new episode.
    """
    return settings.embedding_provider not in ("", "none", "hash")


def _load_cache() -> Dict[str, List[float]]:
    global _cache
    if _cache is not None:
        return _cache
    _cache = {}
    path = settings.embedding_cache_path
    if not _persists() or not os.path.exists(path):
        return _cache
    try:
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
        vectors = payload.get("vectors", {})
        if isinstance(vectors, dict):
            _cache = {k: v for k, v in vectors.items() if isinstance(v, list)}
        logger.info(f"Loaded {len(_cache)} cached embeddings from {path}")
    except Exception as e:
        # A corrupt cache is not a reason to fail a mission -- it is derived
        # data, and every entry can be recomputed from its text.
        logger.warning(f"Could not read embedding cache {path}: {e}. Starting empty.")
        _cache = {}
    return _cache


def save_cache():
    """Write the cache atomically. No-op when nothing new was embedded."""
    global _cache_dirty
    if not _cache_dirty or _cache is None:
        return
    if not _persists():
        _cache_dirty = False
        _trim_cache()
        return
    path = settings.embedding_cache_path
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        _trim_cache()
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"vectors": _cache}, f)
        os.replace(tmp, path)
        _cache_dirty = False
    except Exception as e:
        logger.warning(f"Could not write embedding cache {path}: {e}")


def _trim_cache():
    """
    Oldest-first trim.

    Dicts keep insertion order, so this drops the entries least recently
    *added*. Every dropped vector is recomputable from its text.
    """
    if _cache is None or len(_cache) <= settings.embedding_cache_max:
        return
    overflow = len(_cache) - settings.embedding_cache_max
    for key in list(_cache.keys())[:overflow]:
        _cache.pop(key, None)


def clear_cache():
    """Drop the in-process cache and the counters. Used by the tests."""
    global _cache, _cache_dirty, _unavailable
    _cache = {}
    _cache_dirty = False
    _unavailable = False
    stats.update({"hits": 0, "misses": 0, "provider_calls": 0})


# ---------------------------------------------------------------- the providers

def _feature_bucket(feature: str, dim: int) -> Tuple[int, float]:
    """
    (index, sign) for one feature.

    SHA-256 rather than `hash()`: Python salts string hashing per process, so
    `hash("cache")` differs between runs and the vectors would not be
    comparable across a restart -- let alone across machines, which is what a
    committed cache and a replayed eval both need.

    The sign is the standard signed-hashing trick: two features colliding in
    one bucket cancel half the time instead of always reinforcing, which stops
    unrelated texts drifting upward as the dimension fills.
    """
    digest = hashlib.sha256(feature.encode("utf-8")).digest()
    index = int.from_bytes(digest[:4], "big") % dim
    sign = 1.0 if digest[4] & 1 else -1.0
    return index, sign


def _hash_embed(text: str) -> List[float]:
    dim = max(16, settings.embedding_dim)
    vector = [0.0] * dim
    for token in _TOKEN_RE.findall(text.lower()):
        index, sign = _feature_bucket(f"w:{token}", dim)
        vector[index] += sign * settings.embedding_word_weight

        # `^` and `$` mark the ends, so three letters at the start of a word
        # are a different feature from the same three in the middle.
        padded = f"^{token}$"
        for i in range(len(padded) - 2):
            index, sign = _feature_bucket(f"c:{padded[i:i + 3]}", dim)
            vector[index] += sign * settings.embedding_ngram_weight

    norm = math.sqrt(sum(v * v for v in vector))
    if norm == 0.0:
        return vector
    return [v / norm for v in vector]


def _litellm_embed(texts: List[str]) -> List[List[float]]:
    """One batched call to a real embedding model. Raises on failure."""
    import litellm  # lazy: the hash provider must not need it imported

    stats["provider_calls"] += 1
    response = litellm.embedding(
        model=settings.embedding_model,
        input=texts,
        **llm_route(),
    )
    vectors = [item["embedding"] for item in response["data"]]
    if len(vectors) != len(texts):
        raise ValueError(
            f"embedding provider returned {len(vectors)} vectors for {len(texts)} texts")
    return vectors


# --------------------------------------------------------------------- the API

def available() -> bool:
    """False when embeddings are switched off, or the provider has failed."""
    return settings.embedding_provider not in ("", "none") and not _unavailable


def embed(texts: List[str]) -> List[List[float]]:
    """
    Vectors for `texts`, cached by content.

    Returns `[]` when embeddings are unavailable -- switched off, or a provider
    that failed. An empty result is the signal for the caller to fall back to
    what it did before; it is deliberately not a vector of zeros, which would
    look like a real answer that happens to be orthogonal to everything.
    """
    global _cache_dirty, _unavailable
    if not texts or not available():
        return []

    cache = _load_cache()
    keys = [_cache_key(t) for t in texts]

    # Deduplicated by key, not just filtered: two steps of one mission often
    # carry the same description, and embedding it twice in the same call is
    # the exact waste the cache exists to stop.
    missing: Dict[str, str] = {}
    for text, key in zip(texts, keys):
        if key not in cache and key not in missing:
            missing[key] = text
    stats["misses"] += len(missing)
    stats["hits"] += len(texts) - len(missing)

    if missing:
        try:
            if settings.embedding_provider == "litellm":
                fresh = _litellm_embed(list(missing.values()))
            else:
                fresh = [_hash_embed(t) for t in missing.values()]
        except Exception as e:
            # One warning, then the provider is done for this process.
            _unavailable = True
            logger.warning(
                f"Embedding provider '{settings.embedding_provider}' failed ({e}); "
                f"falling back to lexical similarity for the rest of this process."
            )
            return []
        for key, vector in zip(missing.keys(), fresh):
            cache[key] = vector
        _cache_dirty = True
        save_cache()

    return [cache[k] for k in keys]


def embed_one(text: str) -> Optional[List[float]]:
    """One vector, or None when embeddings are unavailable."""
    vectors = embed([text])
    return vectors[0] if vectors else None


def cosine(a: List[float], b: List[float]) -> float:
    """Cosine similarity. 0.0 when either side is empty or a zero vector."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0
    for x, y in zip(a, b):
        dot += x * y
        norm_a += x * x
        norm_b += y * y
    if norm_a <= 0.0 or norm_b <= 0.0:
        return 0.0
    return dot / math.sqrt(norm_a * norm_b)
