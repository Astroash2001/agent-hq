"""
Record/replay transport for `llm.acompletion`.

An eval suite that gives a different answer each run measures nothing. Models
are non-deterministic even at temperature 0, providers change under you, and a
suite that needs a live key cannot run in CI. So a scored run is a *replayed*
run: the model exchanges are recorded once, committed, and served from disk
afterwards.

The harness must not know this layer exists. Nothing here edits `llm.py` -- it
monkeypatches the `llm.acompletion` symbol, which is the single call site every
consumer resolves through (`base_agent`, `planner` and `critic` all do
`import llm` then `llm.acompletion(...)`, so replacing the module attribute
reaches all three).

A cassette miss in replay mode is a hard error naming the key it wanted. It is
never a live call: a silent fall-through to the network is exactly how a
deterministic suite turns into a flaky one that nobody trusts and everybody
re-runs.
"""
import hashlib
import json
import logging
import os
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger("AgentHQ.Evals.Replay")

CASSETTE_ROOT = os.path.join(os.path.dirname(__file__), "cassettes")

# Written beside a task's cassettes by `--record`, read back by `--replay`.
# Underscored so it sorts away from the keys and reads as not-a-cassette.
RECORDING_MANIFEST = "_recording.json"

# `config.date_context()` prepends "Today's date is <weekday, dd Month yyyy>."
# to every agent system prompt and to the planner's. That is deliberate and
# correct -- a model asked about the news has no idea what day it is -- but it
# means the request bytes change every midnight, so a cassette recorded
# yesterday would miss today and the whole suite would go red overnight for no
# reason at all. The date is normalised out of the key rather than frozen in the
# harness, because the hash is this layer's concern and the harness is not.
_DATE_LINE = re.compile(r"Today's date is [^.\n]*\.")

# `memory.render_hints` writes ", took 41.2s" into every recalled episode it
# pastes into an agent's prompt. That number is wall clock, so a step that took
# 41.2s while being recorded and 0.9s while being replayed asks the provider a
# different question and misses its own cassette -- which is what stopped a
# freshly recorded task from replaying at all. Same reasoning as the date:
# neutralise it in the key rather than take the timing out of the prompt, since
# the hint is genuinely useful to the agent and the hash is this layer's
# concern.
_RECALL_DURATION = re.compile(r", took \d+(?:\.\d+)?s")

# The whole recalled-episode block, which `memory.render_hints` pastes into an
# agent's system prompt between these two fixed lines.
#
# Normalising the timing inside it was not enough. Two steps that run in
# parallel each record an episode when they finish, so whether the second one
# sees the first one's episode depends on which finished first -- and a replay,
# where every model call returns instantly, orders them differently from the
# recording. The block then differs, the key differs, and a mission with any
# fan-out is unreplayable. Observed on `battery_team_review`, whose two research
# steps run side by side.
#
# Same reasoning as the date: the hint is genuinely useful to the agent and
# stays in the prompt; the hash is this layer's concern. It does mean a cassette
# recorded with one set of hints is served for a request with another, which is
# the same trade already accepted for a prompt that says what day it is.
_RECALL_BLOCK = re.compile(
    r"Relevant things you have done before in this office:.*?"
    r"Reuse what worked\. Do not repeat what did not\.",
    re.S)


class CassetteMiss(RuntimeError):
    """Replay wanted an exchange that was never recorded."""


# ------------------------------------------------------------------ hashing

def _canonical(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Messages with the volatile parts neutralised."""
    out = []
    for message in messages or []:
        item = dict(message)
        content = item.get("content")
        if isinstance(content, str):
            content = _DATE_LINE.sub("Today's date is <DATE>.", content)
            content = _RECALL_BLOCK.sub("<RECALLED EPISODES>", content)
            item["content"] = _RECALL_DURATION.sub(", took <SECONDS>", content)
        out.append(item)
    return out


def cassette_key(
    model: Optional[str],
    messages: List[Dict[str, Any]],
    tools: Optional[List[Dict[str, Any]]] = None,
    temperature: Optional[float] = None,
) -> str:
    """
    A stable id for one request.

    Everything that could change the reply goes in: the model, the whole message
    history, the tool schemas offered, and the temperature. Sorted keys and no
    whitespace, so two structurally identical requests hash identically whatever
    order the dicts were built in.

    `model` is hashed exactly as the caller passed it, which is emphatically not
    the same as the model that answers. `base_agent` calls
    `acompletion(messages, tools=...)` with no model at all and lets the default
    apply, so every agent turn hashes `""` here and the key does not distinguish
    models. A sweep recorded on flash-lite replays clean under flash.

    Resolving the default into the key is the obvious fix and is wrong to do on
    its own: it invalidates every cassette already recorded, and a cassette
    stores only the response, so there is nothing to re-key from. That would
    trade a labelling bug for the loss of the whole recorded suite.

    `_check_model` below closes the hole instead, by comparing the model the
    cassette was answered by against the one this run resolves to and refusing
    the mismatch out loud. Resolve it here as well when the suite is next
    re-recorded from scratch, and the two mechanisms agree.
    """
    payload = {
        "model": model or "",
        "messages": _canonical(messages),
        "tools": tools or [],
        "temperature": temperature,
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:24]


# ------------------------------------------------- serialising a response

class _Function:
    def __init__(self, name: str, arguments: str):
        self.name = name
        self.arguments = arguments


class _ToolCall:
    def __init__(self, payload: Dict[str, Any]):
        self.id = payload.get("id", "call_replay")
        self.type = "function"
        fn = payload.get("function") or {}
        self.function = _Function(fn.get("name", ""), fn.get("arguments", "{}"))


class _Message:
    def __init__(self, payload: Dict[str, Any]):
        self.content = payload.get("content")
        raw = payload.get("tool_calls") or []
        self.tool_calls = [_ToolCall(t) for t in raw] or None


class _Choice:
    def __init__(self, payload: Dict[str, Any]):
        self.finish_reason = payload.get("finish_reason", "stop")
        self.message = _Message(payload.get("message") or {})


class _Usage:
    def __init__(self, payload: Dict[str, Any]):
        self.prompt_tokens = payload.get("prompt_tokens", 0)
        self.completion_tokens = payload.get("completion_tokens", 0)


class ReplayResponse:
    """
    The parts of a provider response the harness actually reads.

    `base_agent` reaches for `choices[0].message.content`,
    `.message.tool_calls[].function.{name,arguments}` and
    `choices[0].finish_reason`; `planner` and `critic` read the content only.
    Recording the whole litellm object would pickle a moving target, so the
    cassette stores this shape and nothing else.
    """

    def __init__(self, payload: Dict[str, Any]):
        self.choices = [_Choice(c) for c in (payload.get("choices") or [{}])]
        self.usage = _Usage(payload.get("usage") or {})


def _dump_response(response: Any) -> Dict[str, Any]:
    """Reduce a live provider response to the recordable shape."""
    choices = []
    for choice in getattr(response, "choices", []) or []:
        message = getattr(choice, "message", None)
        tool_calls = []
        for call in (getattr(message, "tool_calls", None) or []):
            fn = getattr(call, "function", None)
            arguments = getattr(fn, "arguments", "{}")
            if not isinstance(arguments, str):
                arguments = json.dumps(arguments)
            tool_calls.append({
                "id": getattr(call, "id", "call_0"),
                "function": {"name": getattr(fn, "name", ""), "arguments": arguments},
            })
        choices.append({
            "finish_reason": getattr(choice, "finish_reason", "stop"),
            "message": {
                "content": getattr(message, "content", None),
                "tool_calls": tool_calls,
            },
        })

    usage = getattr(response, "usage", None)
    return {
        "choices": choices,
        "usage": {
            "prompt_tokens": getattr(usage, "prompt_tokens", 0) or 0,
            "completion_tokens": getattr(usage, "completion_tokens", 0) or 0,
        },
    }


# -------------------------------------------------------------- the transport

class Cassettes:
    """
    One task's recorded exchanges, on disk under `cassettes/<task_id>/`.

    One file per exchange rather than one file per task: a diff then shows which
    single turn changed when a prompt is edited, instead of one unreadable blob
    rewriting itself every time.
    """

    def __init__(self, task_id: str, root: str = CASSETTE_ROOT):
        self.task_id = task_id
        self.dir = os.path.join(root, task_id)
        self.hits = 0
        self.misses: List[str] = []
        self.recorded = 0
        # Models that answered calls which named none -- i.e. whatever the
        # default resolved to at record time. Observed while serving, so a
        # replay can report the model that actually produced the answers
        # instead of the one this machine's environment happens to name.
        self.served_default: set = set()

    def path_for(self, key: str) -> str:
        return os.path.join(self.dir, f"{key}.json")

    def exists(self) -> bool:
        return os.path.isdir(self.dir) and bool(os.listdir(self.dir))

    def count(self) -> int:
        if not os.path.isdir(self.dir):
            return 0
        return len([f for f in os.listdir(self.dir)
                    if f.endswith(".json") and f != RECORDING_MANIFEST])

    def load(self, key: str) -> Optional[Dict[str, Any]]:
        path = self.path_for(key)
        if not os.path.isfile(path):
            return None
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)

    def save(self, key: str, entry: Dict[str, Any]) -> None:
        os.makedirs(self.dir, exist_ok=True)
        with open(self.path_for(key), "w", encoding="utf-8") as fh:
            json.dump(entry, fh, indent=1, ensure_ascii=False, sort_keys=True)
        self.recorded += 1


def write_recording_manifest(task_id: str, root: str = CASSETTE_ROOT) -> None:
    """
    Note the model names this sweep recorded under, beside its cassettes.

    `cassette_key` hashes the model exactly as the caller passed it, so a call
    that names one -- the critic, the synthesis pass and the judge all name
    `settings.fallback_model` -- keys on that name. Replay therefore has to ask
    for the same name to find the recording at all, and a machine whose
    FALLBACK_MODEL differs misses every one of those keys, silently degrading a
    scored suite into an unjudged one.

    Two lines of provenance on disk cost nothing and remove the environment
    from replay entirely, which is what an offline suite is for.
    """
    from config import settings
    os.makedirs(os.path.join(root, task_id), exist_ok=True)
    path = os.path.join(root, task_id, RECORDING_MANIFEST)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({
            "default_model": settings.default_model,
            "fallback_model": settings.fallback_model,
        }, fh, indent=1, sort_keys=True)


def recording_manifest(task_id: str, root: str = CASSETTE_ROOT) -> Dict[str, str]:
    """The model names `task_id` was recorded under, or {} if it predates them."""
    path = os.path.join(root, task_id, RECORDING_MANIFEST)
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, str) and v}


class CassetteModelMismatch(RuntimeError):
    """A cassette was recorded by a different model than this run asks for."""


def _wrong_model(entry: Dict[str, Any], requested: Optional[str]) -> bool:
    """Whether this cassette was answered by a different model than this run
    resolves to. Shared by the replay guard and the record-mode refresh."""
    from config import settings
    recorded = (entry.get("model") or "").strip()
    effective = (requested or settings.default_model or "").strip()
    return bool(recorded and effective and recorded != effective)


def _check_model(entry: Dict[str, Any], requested: Optional[str], key: str,
                 task_id: str) -> None:
    """
    Refuse to serve one model's answers as another's.

    The cassette key does not include the resolved model (see `cassette_key`),
    so without this a suite recorded on flash-lite replays clean under flash and
    the scorecard prints the model that was *asked for* beside answers that
    model never produced. The model column is the scorecard's central claim
    about what was measured; serving it wrong is worse than not serving it.

    Compared against the model that ANSWERED, which is what a cassette stores.
    When a recording fell back, that is the fallback -- and a replay under the
    primary is then correctly refused, because the primary is not what produced
    these answers either.
    """
    if requested is None:
        # An unnamed call, and there is nothing here to check. `cassette_key`
        # hashes the model exactly as the caller passed it, so a key built from
        # None can only have been recorded by a call that also passed None --
        # the match is already proof that the two calls are the same call.
        #
        # What `settings.default_model` names is a provider this run never
        # reaches. Comparing against it made the documented offline `--replay`
        # fail on all fourteen tasks for anyone whose DEFAULT_MODEL was not the
        # recording model, which is the common case: the env default follows
        # whichever lane is cheapest this month, the cassettes do not move.
        #
        # The labelling hole this guard exists to close stays closed, because
        # the answer is not that no model is claimed -- it is that the run
        # reports the model recorded here, gathered below.
        return
    if not _wrong_model(entry, requested):
        return
    from config import settings
    recorded = (entry.get("model") or "").strip()
    effective = (requested or settings.default_model or "").strip()
    raise CassetteModelMismatch(
        f"Cassette {key} in {task_id} was answered by {recorded}, but this run "
        f"resolves to {effective}. Replay with --model {recorded}, or re-record "
        f"with:  python -m evals.runner --suite {task_id} --record")


def make_transport(cassettes: Cassettes, mode: str, real_acompletion):
    """
    Build the `llm.acompletion` stand-in for one task.

    `record` calls through and writes what came back. `replay` serves from disk
    and raises on a miss. The returned callable keeps the real signature,
    because everything upstream of it is untouched harness code.
    """
    import llm

    async def _transport(messages, *, model=None, tools=None, temperature=None,
                         max_tokens=None, timeout=None, on_retry=None):
        key = cassette_key(model, messages, tools, temperature)
        entry = cassettes.load(key)

        if entry is not None and mode == "record" and _wrong_model(entry, model):
            # Re-record rather than reuse. `record` serves a cached entry when
            # one exists, so a sweep run over a directory left by a DIFFERENT
            # model silently keeps that model's answers and produces a scorecard
            # that is a blend of two -- while naming one. Found with 120
            # gemini-flash-lite calls sitting next to 12 OpenAI ones, about to
            # be swept over.
            logger.info("re-recording %s: cassette was answered by %s",
                        key, entry.get("model"))
            entry = None

        if entry is not None:
            if mode != "record":
                _check_model(entry, model, key, cassettes.task_id)
                if model is None and entry.get("model"):
                    cassettes.served_default.add(entry["model"])
            cassettes.hits += 1
            return llm.LLMResult(
                response=ReplayResponse(entry["response"]),
                model=entry.get("model", model or ""),
                prompt_tokens=entry.get("prompt_tokens", 0),
                completion_tokens=entry.get("completion_tokens", 0),
                cost_usd=entry.get("cost_usd", 0.0),
                attempts=entry.get("attempts", 1),
                fell_back=entry.get("fell_back", False),
            )

        cassettes.misses.append(key)
        if mode != "record":
            # Relative, because this message is quoted verbatim into the
            # committed scorecard when a judge call misses -- and an absolute
            # path bakes one machine's home directory into the repo.
            where = os.path.relpath(cassettes.dir, os.path.dirname(
                os.path.dirname(os.path.abspath(__file__))))
            raise CassetteMiss(
                f"No cassette for key {key} in {where}. "
                f"Re-record this task with:  python -m evals.runner "
                f"--suite {cassettes.task_id} --record"
            )

        result = await real_acompletion(
            messages, model=model, tools=tools, temperature=temperature,
            max_tokens=max_tokens, timeout=timeout, on_retry=on_retry)
        cassettes.save(key, {
            "model": result.model,
            "prompt_tokens": result.prompt_tokens,
            "completion_tokens": result.completion_tokens,
            "cost_usd": result.cost_usd,
            "attempts": result.attempts,
            "fell_back": result.fell_back,
            "response": _dump_response(result.response),
        })
        return result

    return _transport


# ------------------------------------------------------- tools that reach out
#
# Recording the model exchanges is not enough to make a run reproducible, and
# assuming it was is what this set exists to fix. An agent's first turn calls
# `web_search`; the results land verbatim in the SECOND turn's prompt, so when
# the web moves, that prompt changes, its key changes, and a suite that replayed
# cleanly an hour ago misses. Diagnosed on `adversarial_trivial`: turn one calls
# web_search, turn two missed with a stable new key -- stable because the search
# had settled on new results, not because anything in the repo had changed.
#
# Which tools may be served from disk, and which have to really run.
#
# The rule is inverted from the obvious one, and it took three recording sweeps
# to get here. "Record the tools that reach outside" sounds right and is not
# specific enough: `list_files` returns a directory listing of this repository,
# and `read_file` will happily read `TODO.md`. Both were observed sitting in a
# replayed prompt -- `.git` byte sizes in one, the whole of TODO.md in the other
# -- and both change whenever anyone commits, so the turn that consumed them
# missed its own cassette for reasons that had nothing to do with the harness.
#
# So: a tool that OBSERVES is recorded, because nothing can promise its answer
# twice. A tool whose point is its SIDE EFFECT really runs, because that effect
# is the mission's output -- serving `write_file` a recorded "ok" leaves the
# workspace empty and scores the run as having produced nothing.
SIDE_EFFECT_TOOLS = ("write_file", "record_decision")


def is_external(tool_name: str) -> bool:
    """
    Whether this tool's result should be served from a cassette.

    True for everything except the writers, including any `mcp__*` server --
    somebody else's server promises nothing about repeating an answer.
    """
    return tool_name not in SIDE_EFFECT_TOOLS


def tool_key(tool_name: str, arguments: Dict[str, Any]) -> str:
    """A stable id for one tool call. Same canonicalisation as `cassette_key`."""
    blob = json.dumps({"tool": tool_name, "arguments": arguments or {}},
                      sort_keys=True, separators=(",", ":"), default=str)
    return "tool_" + hashlib.sha256(blob.encode("utf-8")).hexdigest()[:20]


def make_tool_transport(cassettes: "Cassettes", mode: str, real_execute):
    """
    Wrap `tool_registry.execute` so external tools replay from disk.

    Same contract as `make_transport`: record calls through and writes, replay
    serves and treats a miss as fatal. Anything `is_external` says no to is
    passed straight through and really runs.
    """
    async def _execute(tool_name, arguments, agent_id=None, task_id=None):
        if not is_external(tool_name):
            return await real_execute(tool_name, arguments,
                                      agent_id=agent_id, task_id=task_id)

        key = tool_key(tool_name, arguments)
        entry = cassettes.load(key)
        if entry is not None:
            cassettes.hits += 1
            # Stored as a JSON *string*, and it has to be. `Cassettes.save`
            # writes with sort_keys=True, so a result stored as a nested object
            # comes back alphabetised -- and `base_agent` puts
            # `json.dumps(tool_result)` straight into the next turn's prompt.
            # The replayed prompt then differs from the recorded one by key
            # order alone, and that turn misses its own cassette. Round-tripping
            # the exact bytes keeps insertion order, which json.loads preserves.
            return json.loads(entry["result_json"])

        cassettes.misses.append(key)
        if mode != "record":
            where = os.path.relpath(cassettes.dir, os.path.dirname(
                os.path.dirname(os.path.abspath(__file__))))
            raise CassetteMiss(
                f"No cassette for {tool_name} call {key} in {where}. "
                f"Re-record this task with:  python -m evals.runner "
                f"--suite {cassettes.task_id} --record")

        result = await real_execute(tool_name, arguments,
                                    agent_id=agent_id, task_id=task_id)
        cassettes.save(key, {"tool": tool_name,
                             "result_json": json.dumps(result)})
        return result

    return _execute


class installed:
    """
    Context manager that swaps `llm.acompletion` and `tool_registry.execute` for
    the duration of one task.

    Restores the originals on the way out, including when the task raises, so
    one failing eval cannot leave the transport patched for every task after it.
    """

    def __init__(self, cassettes: Cassettes, mode: str):
        self.cassettes = cassettes
        self.mode = mode
        self._original = None
        self._original_tool = None

    def __enter__(self):
        import llm
        from tool_registry import tool_registry
        self._original = llm.acompletion
        llm.acompletion = make_transport(self.cassettes, self.mode, self._original)
        self._original_tool = tool_registry.execute
        tool_registry.execute = make_tool_transport(
            self.cassettes, self.mode, self._original_tool)
        return self.cassettes

    def __exit__(self, *exc):
        import llm
        from tool_registry import tool_registry
        llm.acompletion = self._original
        tool_registry.execute = self._original_tool
        return False
