"""
Does the SQL a report shows match SQL that actually ran?

A report that pairs every figure with "the SQL that produced it" reads as
audited, and that is exactly what makes a made-up query dangerous. Observed: a
report step with no `query_data` tool printed a `ROUND(goals * 90.0 / minutes,
4)` query and its result as though it had been run. The figure happened to be
right; nothing in the report could have told a reader it was never executed.

This is the SQL counterpart of the conclusion's `ungrounded_figures`, and it
follows the same rule: reported, never rejected. A query shown in a slightly
different layout from the one that ran is still flagged, and a reader can clear
that in seconds -- that trade is better than a check that lets rewording through.

Deterministic and free: regex extraction and a whitespace/case normalisation,
no model call.
"""
import re
from typing import Iterable, List, Optional, Set, Tuple

# ```sql ... ``` fences, the form the reports use for a query.
_FENCE = re.compile(r"```\s*sql\s*\n(.*?)```", re.I | re.S)
# `SELECT ...` in inline code, the form summary tables use.
# A real statement, not a word: SELECT needs a FROM, WITH needs `AS (`. The loose
# version flagged "`with`" and bare fragments like "`SELECT DISTINCT squad_no`"
# in a summary table, which are not claims that a query ran.
_INLINE = re.compile(r"`\s*((?:SELECT\b[^`]*\bFROM\b|WITH\b[^`]*\bAS\s*\()[^`]*)`", re.I)

# Cap on how much of one flagged query is kept in the manifest.
_SHOWN_CHARS = 200


def normalise(sql: str) -> str:
    """
    A comparison key for one SQL statement.

    Case, whitespace, a trailing semicolon and the spacing around punctuation
    are layout, not meaning, so they are dropped. Nothing smarter than that: a
    renamed alias or a reordered clause is a different query as far as this
    check is concerned.
    """
    text = (sql or "").strip().rstrip(";").strip().lower()
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\s*([(),=<>*+/-])\s*", r"\1", text)
    return text


def _select_parts(key: str) -> Optional[Tuple[frozenset, str]]:
    """
    A normalised simple SELECT as (its output expressions, everything from FROM).

    Aliases are dropped from the expressions. None for anything this cannot
    split safely -- a WITH, a subquery in the select list -- which then has to
    match exactly.
    """
    # `normalise` drops the space after a closing parenthesis, so "sum(x)from"
    # is how FROM can arrive; matched as a word rather than by its spaces.
    match = re.match(r"^select (.+?) ?(?<![a-z0-9_])from(?![a-z0-9_]) ?(.+)$", key)
    if not match or "select" in match.group(1):
        return None
    items, depth, current = [], 0, ""
    for ch in match.group(1):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            items.append(current)
            current = ""
        else:
            current += ch
    items.append(current)
    # " as n", or ")as n" once normalise has eaten the space after a bracket.
    cleaned = frozenset(re.sub(r"(?:\s|(?<=\)))as [a-z0-9_]+$", "", item.strip())
                        for item in items)
    return cleaned, match.group(2)


def _covered(key: str, ran: Set[str], ran_parts: List[Tuple[frozenset, str]]) -> bool:
    """
    True when `key` ran, or asks for a subset of what one query that ran returned.

    A summary table that lists `SELECT SUM(goals) FROM data` beside a figure
    produced by `SELECT COUNT(*), SUM(goals), SUM(minutes) FROM data` is citing
    the query that ran, one column at a time. Same FROM, WHERE and ORDER, and
    expressions that query actually returned -- anything else still counts as
    a query nobody ran.
    """
    if key in ran:
        return True
    parts = _select_parts(key)
    if parts is None:
        return False
    items, rest = parts
    return any(rest == ran_rest and items <= ran_items for ran_items, ran_rest in ran_parts)


def extract_sql(text: str) -> List[str]:
    """
    Every query a document presents as SQL: fenced blocks, and inline code in
    table rows.

    Inline code in prose is not checked. "Bob would have verified it with
    `SELECT squad_no FROM data LIMIT 2`" is a suggestion, not a claim that the
    query ran -- and a recorded meeting decision is mostly sentences like that.
    A table row pairing a figure with its SQL is the claim, and so is a fence.
    """
    found: List[str] = []
    body = text or ""
    for block in _FENCE.findall(body):
        # A fence can hold several statements; each is its own claim.
        for statement in block.split(";"):
            if re.match(r"\s*(select|with)\b", statement, re.I):
                found.append(statement.strip())
    for line in _FENCE.sub("", body).splitlines():
        if not line.lstrip().startswith("|"):
            continue
        for inline in _INLINE.findall(line):
            found.append(inline.strip())
    return found


def unexecuted_sql(artifacts: Iterable[Tuple[str, str]],
                   executed: Iterable[str]) -> List[str]:
    """
    Queries shown in `artifacts` that match nothing in `executed`.

    Returned as "file: query" strings, de-duplicated, in the order found, so
    the manifest can list them and a reader can find each one.
    """
    ran = {normalise(q) for q in executed}
    ran_parts = [p for p in (_select_parts(k) for k in ran) if p is not None]
    flagged: List[str] = []
    seen = set()
    for name, body in artifacts:
        for query in extract_sql(body):
            key = normalise(query)
            if not key or (name, key) in seen or _covered(key, ran, ran_parts):
                continue
            seen.add((name, key))
            shown = re.sub(r"\s+", " ", query)
            if len(shown) > _SHOWN_CHARS:
                shown = shown[:_SHOWN_CHARS] + "..."
            flagged.append(f"{name}: {shown}")
    return flagged
