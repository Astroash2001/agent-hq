"""
Query a CSV or JSON file with SQL, in memory.

`data_analyst` once held `read_file`, `list_files` and `terminal` -- which is to say
it had no data tool at all, and analysed a spreadsheet by reading it into its
own context window and doing the arithmetic in its head. That fails in two
ways at once: a file bigger than the context is silently truncated to whatever
fit, and the sums over what did fit are the same unverifiable mental arithmetic
`calculate.py` exists to replace.

sqlite3 is in the standard library, so this adds no dependency. The file is
loaded into an in-memory database, queried, and thrown away; nothing is written
back, and the source file is opened read-only.

Two guardrails, one of them a shape this repo did not have:

  - **Path containment**, reused rather than reinvented. `file_ops._is_contained`
    already resolves symlinks and refuses traversal, and it is the checked,
    tested implementation -- a second one here would be a second thing to keep
    correct.
  - **Read-only SQL**, which is new. The connection is opened with an authorizer
    that refuses every operation except reading, so INSERT, UPDATE, DROP,
    ATTACH and PRAGMA are rejected by SQLite itself rather than by a regex over
    the query text. A keyword blocklist is the obvious approach and it does not
    hold: comments, case, whitespace and string literals all defeat it. The
    authorizer sees parsed operations, not spellings.
"""
import csv
import json
import logging
import os
import re
import sqlite3
from typing import Any, Dict, List, Optional

from config import settings  # type: ignore
from tools.file_ops import (  # type: ignore
    _get_read_allowed_roots, _is_contained, refusal_for, resolve_read_path)

logger = logging.getLogger("AgentHQ.QueryData")

TABLE = "data"


def _refuse(message: str) -> Dict[str, Any]:
    return {"success": False, "refused": True, "error": message}


def _authorizer(action: int, *_args) -> int:
    """
    SQLite's own gate on what a statement may do.

    Called for every operation the parser produces. Only reads are permitted;
    everything else -- writes, schema changes, ATTACH, PRAGMA, loading
    extensions -- returns DENY, so the refusal comes from the engine and covers
    spellings no blocklist would catch.

    SQLITE_READ, SQLITE_SELECT and SQLITE_FUNCTION are the three an ordinary
    SELECT needs. SQLITE_FUNCTION covers count(), sum(), avg() and friends.
    """
    if action in (sqlite3.SQLITE_READ, sqlite3.SQLITE_SELECT, sqlite3.SQLITE_FUNCTION):
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


_INT_RE = re.compile(r"^[+-]?\d+$")
_FLOAT_RE = re.compile(r"^[+-]?(\d+\.\d*|\.\d+)([eE][+-]?\d+)?$|^[+-]?\d+[eE][+-]?\d+$")


def _coerce(value: str) -> Any:
    """
    A CSV cell as the number it is, or the text it is.

    `csv.reader` returns every cell as a string, and SQLite will happily store
    them that way. Aggregates still come out right, because SQLite coerces text
    for SUM -- which is what makes this worth doing deliberately, since the
    thing that does NOT come out right is silent: `ORDER BY revenue` over text
    sorts lexically, so '80' sorts above '120' and the top-selling region in
    the report is the wrong one. No error, no warning, a confident wrong answer
    in an artifact. Exactly the failure this whole tool exists to remove.

    A value with a leading zero stays text. '01234' is a postcode, an account
    number or a part code far more often than it is 1234, and dropping the zero
    corrupts an identifier while looking like a tidy-up.
    """
    text = value.strip() if isinstance(value, str) else value
    if text is None or text == "":
        return None
    if not isinstance(text, str):
        return text
    body = text[1:] if text[:1] in "+-" else text
    if len(body) > 1 and body[0] == "0" and "." not in body:
        return text
    if _INT_RE.match(text):
        try:
            return int(text)
        except ValueError:
            return text
    if _FLOAT_RE.match(text):
        try:
            return float(text)
        except ValueError:
            return text
    return text


def _rows_from_csv(path: str, max_rows: int) -> tuple:
    with open(path, "r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.reader(fh)
        try:
            header = next(reader)
        except StopIteration:
            return [], []
        rows = []
        overflowed = False
        for index, row in enumerate(reader):
            # One past the cap, so "there are more" is a fact rather than an
            # assumption. Stopping AT the cap cannot tell a file of exactly
            # max_rows from one of ten million.
            if index >= max_rows:
                overflowed = True
                break
            rows.append([_coerce(cell) for cell in row])
    return header, rows, overflowed


def _rows_from_json(path: str, max_rows: int) -> tuple:
    with open(path, "r", encoding="utf-8") as fh:
        payload = json.load(fh)

    # A bare list of objects, or an object wrapping one under any key -- both
    # shapes turn up in the wild and neither is worth making the agent reshape
    # by hand before it can ask a question.
    if isinstance(payload, dict):
        for value in payload.values():
            if isinstance(value, list):
                payload = value
                break
    if not isinstance(payload, list):
        raise ValueError("JSON must be a list of objects, or an object containing one.")

    overflowed = len(payload) > max_rows
    records = [r for r in payload[:max_rows] if isinstance(r, dict)]
    if not records:
        raise ValueError("No object rows found in the JSON.")

    header: List[str] = []
    for record in records:
        for key in record:
            if key not in header:
                header.append(key)
    rows = [[_scalar(record.get(key)) for key in header] for record in records]
    return header, rows, overflowed


def _scalar(value: Any) -> Any:
    """Nested structures become their JSON text; SQLite has no column for them."""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return value


def _safe_column(name: str, index: int) -> str:
    """A column name SQL can address, without letting the file name the schema."""
    cleaned = "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in str(name)).strip("_")
    if not cleaned or cleaned[0].isdigit():
        cleaned = f"col_{index}" if not cleaned else f"c_{cleaned}"
    return cleaned.lower()


async def query_data(
    filepath: str,
    query: str,
    max_rows: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Run a read-only SQL query against a CSV or JSON file.

    The file becomes a table called `data`. Never raises.

    Every query that reached the engine -- ran, was refused, or failed as SQL
    -- is written down on the bound mission, so a report's SQL can be checked
    against what was really attempted (`provenance.unexecuted_sql`). A refusal
    or an SQL error quoted in a report is a real call being reported honestly:
    observed, an extraction showing "`SELECT name ...` -- failed, the column is
    `player`" was flagged as SQL nobody ran. A figure attached to a failed query
    is caught by the figure checks, not by this one. A missing file is not
    recorded: no query was attempted.
    """
    result = await _query_data(filepath, query, max_rows)
    reached_engine = (result.get("success") or result.get("refused")
                      or str(result.get("error", "")).startswith("SQL error"))
    if reached_engine:
        from tools.file_ops import active_workspace
        workspace = active_workspace()
        if workspace is not None and hasattr(workspace, "record_query"):
            workspace.record_query((query or "").strip())
    return result


async def _query_data(
    filepath: str,
    query: str,
    max_rows: Optional[int] = None,
) -> Dict[str, Any]:
    path = (filepath or "").strip()
    sql = (query or "").strip()

    if not path:
        return _refuse("No filepath given.")
    if not sql:
        return _refuse("No query given.")

    resolved = resolve_read_path(path)
    # Refused before the existence check, so a denied path does not answer
    # "is there a file here" either.
    denied = refusal_for(resolved, path)
    if denied:
        return denied
    if not os.path.isfile(resolved):
        return {"success": False, "error": f"No such file: {path}"}

    path = resolved
    if getattr(settings, "file_ops_sandbox_enabled", True):
        if not _is_contained(path, _get_read_allowed_roots()):
            return _refuse(
                f"'{path}' is outside the mission workspace and the repository, "
                f"so it cannot be read.")

    extension = os.path.splitext(path)[1].lower()
    requested_rows = max_rows
    max_rows = max_rows or settings.query_data_max_rows
    max_rows = min(max_rows, settings.query_data_max_rows)

    try:
        if extension in (".csv", ".tsv", ".txt"):
            header, rows, overflowed = _rows_from_csv(
                path, settings.query_data_max_source_rows)
        elif extension == ".json":
            header, rows, overflowed = _rows_from_json(
                path, settings.query_data_max_source_rows)
        else:
            return _refuse(
                f"Cannot query '{extension or 'a file with no extension'}'. "
                f"Supported: .csv, .tsv, .txt, .json.")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {"success": False, "error": f"Could not read {os.path.basename(path)}: {exc}"}

    if not header:
        return {"success": False, "error": f"{os.path.basename(path)} has no columns."}

    # Prose queried as a table returns tidy nonsense: a letter opening "Dear
    # team," becomes the columns `dear_team` and `col_1`, and every SELECT over
    # it succeeds. `.txt` is accepted because data exports use it, and that is
    # what lets a letter in.
    #
    # Width, not column count, is the test. "Dear team," has a trailing comma
    # and so splits into two fields, which passes any check on the header
    # alone. What separates data from prose is that data is RECTANGULAR: every
    # row has the header's width. Prose does not, line by line.
    if extension != ".json" and rows:
        sample = rows[:50]
        matching = sum(1 for r in sample if len(r) == len(header))
        if matching * 2 < len(sample):
            return _refuse(
                f"{os.path.basename(path)} does not look like delimited data: "
                f"only {matching} of its first {len(sample)} rows have the "
                f"{len(header)} fields its first line implies. Querying it "
                f"would return prose as a table. Use read_file for text, or "
                f"point this at a real .csv or .tsv.")

    # The whole value of this tool is that its answer is exact. Loading part of
    # the file and answering anyway is worse than not answering: a COUNT over
    # the first 50,000 rows of a 60,000-row file comes back clean, plausible
    # and wrong, and nothing in the reply says which. That is the failure this
    # tool was recommended over paging to avoid -- it had it too.
    #
    # Refused rather than flagged. A flag beside a number is something an agent
    # can carry into its report without repeating; a refusal it cannot.
    if overflowed:
        return _refuse(
            f"{os.path.basename(path)} has more than "
            f"{settings.query_data_max_source_rows:,} rows, which is this "
            f"tool's load limit. Any answer over a partly-loaded file would be "
            f"wrong without saying so, so nothing was run. Say in your "
            f"deliverable that the file is over the limit and was not "
            f"analysed; the operator can raise query_data_max_source_rows if "
            f"the whole file genuinely has to be in memory at once.")

    columns = [_safe_column(name, i) for i, name in enumerate(header)]
    # Duplicate names after cleaning would make the CREATE fail on a file the
    # agent cannot edit, so they are numbered instead.
    seen: Dict[str, int] = {}
    for i, name in enumerate(columns):
        if name in seen:
            seen[name] += 1
            columns[i] = f"{name}_{seen[name]}"
        else:
            seen[name] = 0

    connection = sqlite3.connect(":memory:")
    try:
        placeholders = ", ".join("?" for _ in columns)
        quoted = ", ".join(f'"{c}"' for c in columns)
        connection.execute(f'CREATE TABLE {TABLE} ({quoted})')
        connection.executemany(
            f'INSERT INTO {TABLE} VALUES ({placeholders})',
            [row[:len(columns)] + [None] * (len(columns) - len(row)) for row in rows],
        )
        connection.commit()

        # Set only now. The authorizer denies INSERT, so loading the data has to
        # finish before the gate closes -- the query is the untrusted part, not
        # the file's own contents.
        connection.set_authorizer(_authorizer)

        cursor = connection.execute(sql)
        fetched = cursor.fetchmany(max_rows + 1)
        truncated = len(fetched) > max_rows
        fetched = fetched[:max_rows]
        result_columns = [d[0] for d in (cursor.description or [])]

    except sqlite3.DatabaseError as exc:
        message = str(exc)
        if "not authorized" in message.lower():
            return _refuse(
                "This tool is read-only: only SELECT queries are permitted. "
                "Writes, schema changes, ATTACH and PRAGMA are refused.")
        return {"success": False,
                "error": f"SQL error: {message}. The table is called '{TABLE}' "
                         f"with columns: {', '.join(columns)}."}
    finally:
        connection.close()

    result = {
        "success": True,
        "file": os.path.basename(path),
        "table": TABLE,
        "columns": result_columns,
        "source_columns": columns,
        "row_count": len(fetched),
        "truncated": truncated,
        "rows": [list(r) for r in fetched],
    }
    if truncated:
        # Said outright, with what to do. Without it an agent asked for
        # max_rows=30000 ten times in a row, waiting for a cap that never lifts.
        asked_more = bool(requested_rows and requested_rows > settings.query_data_max_rows)
        result["hint"] = (
            f"Only the first {max_rows} rows are returned"
            + (f" -- max_rows is capped at {settings.query_data_max_rows}, so asking "
               f"for {requested_rows} returns the same rows" if asked_more else "")
            + ". Do not repeat this query. Aggregate instead (COUNT, SUM, AVG, "
              "GROUP BY), filter with WHERE, or take the rows you need with "
              "ORDER BY ... LIMIT. A deliverable should report the aggregate, "
              "not a full row dump.")
    return result
