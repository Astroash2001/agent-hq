"""
Read an image, by asking a model that can see.

Every other tool in this office turns something into text for the agent to
reason over. An image was the one attachment with no such path: it could be
staged, and then nothing in the harness could do anything with it. An agent
handed a screenshot of a table did what this project keeps finding it does when
a capability is missing -- it wrote what the table probably said.

So this is a tool like any other: it takes a path, returns text, and is bound
to a station. What is unusual is that its implementation is a model call, which
makes it the only tool that spends money of its own. Three consequences, and
they are why the guardrails below look the way they do.

  **It is priced and reported.** The reply carries `cost_usd` and the token
  counts, drawn from the same `llm.acompletion` every other call goes through,
  so a mission that spent four dollars looking at pictures says so in the same
  gauge as everything else.

  **It is capped per call and refuses what it cannot afford.** An image becomes
  base64 in the prompt, which is roughly a third larger than the file. A 10MB
  photograph is millions of tokens before the question is even asked, so the
  size limit is checked against the encoded length rather than the file's.

  **It cannot reach outside the jail.** The path goes through the same
  `file_ops` containment `read_file` uses, because a tool that reads bytes off
  disk and posts them to a third party is the most attractive thing in the
  harness to point at `~/.ssh/id_rsa`. The format allowlist is the second half
  of that: a file is checked for a real image header, not for its extension.
"""
import base64
import logging
import os
from typing import Any, Dict, Optional

import llm
from config import settings
from tools.file_ops import _get_read_allowed_roots, _is_contained, resolve_read_path

logger = logging.getLogger("AgentHQ.DescribeImage")

# Magic bytes, checked instead of the extension. A .png that is really a
# zip is not something to hand to a vision endpoint.
_SIGNATURES = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)

DEFAULT_PROMPT = (
    "Describe this image in full. If it contains a table, transcribe it as a "
    "markdown table with every row. If it contains a chart, state the chart "
    "type, the axes and their units, and read off each series' values. If it "
    "contains text, transcribe it verbatim. Say plainly when something is "
    "illegible rather than guessing at it."
)


def _sniff(raw: bytes) -> Optional[str]:
    """The MIME type these bytes actually are, or None if not an image."""
    for magic, mime in _SIGNATURES:
        if raw.startswith(magic):
            return mime
    # WEBP is "RIFF....WEBP" -- the size sits between the two markers.
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    return None


def _refuse(message: str) -> Dict[str, Any]:
    return {"success": False, "refused": True, "error": message}


async def describe_image(filepath: str, prompt: str = "") -> Dict[str, Any]:
    """
    Describe, transcribe or read an image into text.

    Never raises, for the reason every tool here documents: a throwing tool
    gets retried with different wording until the step's budget is gone.
    """
    path = (filepath or "").strip()
    if not path:
        return _refuse("No filepath given.")

    candidate = resolve_read_path(path)
    if getattr(settings, "file_ops_sandbox_enabled", True):
        if not _is_contained(candidate, _get_read_allowed_roots()):
            return _refuse(
                f"'{path}' resolves outside the workspace and the repository, "
                f"so it cannot be read.")

    if not os.path.isfile(candidate):
        return {"success": False, "error": f"No such image: {path}"}

    try:
        with open(candidate, "rb") as fh:
            raw = fh.read()
    except OSError as exc:
        return {"success": False, "error": f"Could not read {path}: {exc}"}

    if not raw:
        return {"success": False, "error": f"{path} is empty."}

    mime = _sniff(raw)
    if mime is None:
        return _refuse(
            f"'{os.path.basename(path)}' is not a PNG, JPEG, WEBP or GIF. "
            f"Checked by reading the file's header, not its extension.")

    encoded = base64.b64encode(raw).decode("ascii")
    # Against the encoded length, which is what actually enters the prompt.
    if len(encoded) > settings.describe_image_max_encoded_chars:
        return _refuse(
            f"that image is {len(raw) / 1024 / 1024:.1f}MB, which is "
            f"{len(encoded):,} characters once encoded for the model -- over "
            f"the {settings.describe_image_max_encoded_chars:,} limit. Resize "
            f"or crop it and try again.")

    question = (prompt or "").strip() or DEFAULT_PROMPT
    model = settings.vision_model or settings.default_model

    messages = [{
        "role": "user",
        "content": [
            {"type": "text", "text": question},
            {"type": "image_url",
             "image_url": {"url": f"data:{mime};base64,{encoded}"}},
        ],
    }]

    try:
        result = await llm.acompletion(
            messages, model=model, max_tokens=settings.describe_image_max_tokens)
    except Exception as exc:                                   # noqa: BLE001
        # Said as a result rather than raised, and specifically enough to act
        # on: "this model cannot see" is a different problem from "the request
        # failed", and only the first is fixed by changing VISION_MODEL.
        logger.warning("describe_image failed on %s: %s", path, exc)
        return {
            "success": False,
            "error": (f"The vision call failed ({type(exc).__name__}: {exc}). "
                      f"If {model} has no vision support, set VISION_MODEL to "
                      f"one that does."),
        }

    try:
        description = (result.response.choices[0].message.content or "").strip()
    except (AttributeError, IndexError, TypeError):
        description = ""

    if not description:
        return {
            "success": False,
            "model": result.model,
            "cost_usd": result.cost_usd,
            "error": (f"{result.model} returned nothing for this image. It may "
                      f"have no vision support, or the image may be unreadable."),
        }

    return {
        "success": True,
        "filepath": path,
        "mime_type": mime,
        "bytes": len(raw),
        "model": result.model,
        "prompt_tokens": result.prompt_tokens,
        "completion_tokens": result.completion_tokens,
        # Reported like any other spend. A tool that quietly costs money is one
        # nobody budgets for.
        "cost_usd": result.cost_usd,
        "description": description,
        "text": description,
    }
