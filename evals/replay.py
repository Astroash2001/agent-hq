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
import os
import re
from typing import Any, Dict, List, Optional

CASSETTE_ROOT = os.path.join(os.path.dirname(__file__), "cassettes")

# `config.date_context()` prepends "Today's date is <weekday, dd Month yyyy>."
# to every agent system prompt and to the planner's. That is deliberate and
# correct -- a model asked about the news has no idea what day it is -- but it
# means the request bytes change every midnight, so a cassette recorded
# yesterday would miss today and the whole suite would go red overnight for no
# reason at all. The date is normalised out of the key rather than frozen in the
# harness, because the hash is this layer's concern and the harness is not.
_DATE_LINE = re.compile(r"Today's date is [^.\n]*\.")


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
            item["content"] = _DATE_LINE.sub("Today's date is <DATE>.", content)
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

    def path_for(self, key: str) -> str:
        return os.path.join(self.dir, f"{key}.json")

    def exists(self) -> bool:
        return os.path.isdir(self.dir) and bool(os.listdir(self.dir))

    def count(self) -> int:
        if not os.path.isdir(self.dir):
            return 0
        return len([f for f in os.listdir(self.dir) if f.endswith(".json")])

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

        if entry is not None:
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
            raise CassetteMiss(
                f"No cassette for key {key} in {cassettes.dir}. "
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


class installed:
    """
    Context manager that swaps `llm.acompletion` for the duration of one task.

    Restores the original on the way out, including when the task raises, so one
    failing eval cannot leave the transport patched for every task after it.
    """

    def __init__(self, cassettes: Cassettes, mode: str):
        self.cassettes = cassettes
        self.mode = mode
        self._original = None

    def __enter__(self):
        import llm
        self._original = llm.acompletion
        llm.acompletion = make_transport(self.cassettes, self.mode, self._original)
        return self.cassettes

    def __exit__(self, *exc):
        import llm
        llm.acompletion = self._original
        return False
