"""
What an artifact has to be before the next step is allowed to read it.

Two patches in the orchestrator exist because this layer did not.
`_best_artifact` was written after an agent's closing chat message got filed as
the deliverable, and `_run_step` appends an explicit "Deliverable: write your
full output to <name>" instruction because a researcher once filed a 39-byte
stub as the research the rest of the mission was built on. Both make it more
likely that a good artifact is produced. Neither ever *checked* one.

So this does. A spec is inferred from the promised filename and the step's own
description, and `check` says whether what arrived satisfies it -- before the
step is marked complete and before anything downstream consumes it.

Everything here is a pure function: no model call, no file access, no clock. The
expensive judgement lives in `critic.py`; this layer is the cheap one that runs
on every artifact, and it has to stay fast enough that running it is never a
decision.
"""
import csv
import io
import json
import re
from dataclasses import dataclass, field
from typing import List, Optional

# The stub that started all of this. A real GPT-5.5 run filed exactly this
# string as `solar_efficiency_research.md` while the 2.4KB report the agent had
# actually written went to a loose path in backend/ -- and the next step
# consumed the sentence. base_agent still produces it as a last resort, so it
# is the first thing any artifact is tested against.
STUB_PATTERNS: List[str] = [
    r"Task completed after \d+ execution steps",
    r"Task halted by emergency stop",
    r"Task completed successfully\.?\s*$",
    r"lorem ipsum",
]

# Something that looks like a source: a bare URL, a markdown link, or a
# numbered reference marker. Deliberately loose -- the question is whether the
# agent grounded its claims at all, not whether it used a citation style.
_CITATION_RE = re.compile(r"https?://\S+|\[\d+\]|\[[^\]]+\]\(https?://", re.I)
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+\S", re.M)

# Filenames that promise a finished piece of writing rather than working notes.
_REPORT_HINTS = ("report", "briefing", "summary", "recommendation", "brief")
_NOTES_HINTS = ("notes", "scratch", "draft", "log")

_PLACEHOLDERS = {
    "", "null", "none", "n/a", "na", "tbd", "unknown", "undefined",
    "not available", "not fetched", "not provided", "-"
}


@dataclass
class ArtifactSpec:
    """The bar one artifact has to clear."""
    min_chars: int = 200
    required_sections: List[str] = field(default_factory=list)
    must_not_match: List[str] = field(default_factory=lambda: list(STUB_PATTERNS))
    require_citations: bool = False
    # A finished document has structure; a set of notes need not. Kept separate
    # from `required_sections`, which names specific headings -- this only asks
    # that the artifact has any heading at all.
    require_heading: bool = False
    # Format and key validations
    filename: str = ""
    expected_format: str = "text"   # text | json | csv | markdown
    expected_keys: List[str] = field(default_factory=list)
    optional_keys: List[str] = field(default_factory=list)


@dataclass
class CheckResult:
    """The verdict, and what specifically was wrong."""
    ok: bool
    failures: List[str] = field(default_factory=list)
    severity: str = "none"          # none | minor | major

    def as_guidance(self) -> str:
        """The failures as an instruction the agent can act on. An agent told
        only that its work was rejected will produce the same thing again."""
        if not self.failures:
            return ""
        lines = ["Your previous attempt was rejected before it could be used. "
                 "Fix all of the following and write the file again:"]
        lines += [f"- {f}" for f in self.failures]
        return "\n".join(lines)


def infer_spec(
    filename: str,
    description: str = "",
    *,
    citations_expected: bool = False
) -> ArtifactSpec:
    """
    The bar for this particular artifact.

    Deliberately permissive by default. A spec that rejects reasonable work is
    worse than no spec at all -- it spends a repair attempt, and then the step
    completes anyway with the same artifact, having paid for nothing.

    `citations_expected` is passed in rather than guessed at: whether sources
    were even obtainable depends on the agent's tool grants and on whether a
    search key is configured, and neither is knowable from a filename. A
    contract that demands citations from an agent with no way to fetch one is a
    contract that fails every run offline.
    """
    name = (filename or "").lower()
    text = (description or "").lower()
    spec = ArtifactSpec(filename=filename or "")

    if name.endswith(".json"):
        spec.expected_format = "json"
        spec.min_chars = 10
    elif name.endswith(".csv"):
        spec.expected_format = "csv"
        spec.min_chars = 20
    elif name.endswith(".md") or name.endswith(".markdown"):
        spec.expected_format = "markdown"

    if any(hint in name for hint in _REPORT_HINTS):
        # The mission's deliverable. Longer, and structured.
        spec.min_chars = 500
        spec.require_heading = True
    elif any(hint in name for hint in _NOTES_HINTS):
        # Working notes feed a later step; they are allowed to be terse.
        spec.min_chars = 120

    # Citations are a demand on PROSE. A CSV or a JSON document is data: a
    # four-column providers.csv (provider,monthly_usd,disk_supported,
    # cold_start_seconds) has no column a URL could honestly go in, so the
    # only way to satisfy "include the URLs you used" is to corrupt the schema
    # the instruction specified. Observed: exactly that step rejected twice for
    # "No sources are cited", a mission marked `unverified` on the strength of
    # it -- while its companion markdown note, which CAN carry sources, was
    # never the file being judged. The step's description mentioning a web
    # search says the work involved one; it says nothing about the format.
    data_file = spec.expected_format in ("csv", "json")
    if citations_expected and not data_file and (
            "research" in name or "research" in text
            or "source" in text or "cite" in text):
        spec.require_citations = True

    # Extract requested schema keys from description (e.g. record: {city, store_name, address, phone...})
    m = re.search(r"\{([^}]+)\}", description or "")
    if m:
        raw_items = [x.strip() for x in m.group(1).split(",")]
        for item in raw_items:
            clean = re.sub(r"\(.*?\)", "", item).strip().lower().replace(" ", "_")
            if not clean:
                continue
            item_lower = item.lower()
            if any(opt in item_lower for opt in ["optional", "if present", "if available", "if known"]):
                spec.optional_keys.append(clean)
            else:
                spec.expected_keys.append(clean)

    return spec


# Endings that finish a document: punctuation, a closing fence or quote, a
# table row, a list marker's own line. Anything else at the very end is a
# sentence the writer did not get to the end of.
_FINISHED_END = re.compile(r"""[.!?:;)\]"'`*_|>-]\s*$|```\s*$""", re.S)


def _looks_cut_off(body: str) -> Optional[str]:
    """
    Whether this artifact stops in the middle of something, and what.

    A step that exhausts its output ceiling files whatever it had: a report
    ending "…the Mand" reads as finished to every check that counts characters.
    Two tells, both cheap and both free of false alarms on ordinary prose: an
    unclosed code fence, and a final line that simply stops.
    """
    text = (body or "").rstrip()
    if not text:
        return None
    if text.count("```") % 2 == 1:
        return "code block"
    last_line = text.splitlines()[-1].strip()
    # A heading or a lone marker is a fine place to stop only if nothing follows
    # it; a heading as the very last line means the section under it is missing.
    if last_line.startswith("#"):
        return "section, with its heading written and nothing under it"
    if _FINISHED_END.search(last_line):
        return None
    # A running sentence with no terminator is the cut-off case. A short or
    # few-word final line -- a label, a squad number, a table cell, a bold
    # status -- legitimately ends bare, so both tests have to hold.
    words = len(last_line.split())
    if words < 6 or len(last_line) <= 40:
        return None
    # A line that already contains a full stop is a finished thought whose
    # final clause happens to end bare -- "**Verified.** All 4 steps passed
    # review". A long line with no sentence punctuation at all is the writer
    # being cut off.
    if re.search(r"[.!?]", last_line):
        return None
    return "sentence"


def check(artifact: Optional[str], spec: ArtifactSpec) -> CheckResult:
    """
    Measure one artifact against its spec.

    `ok` is false if anything failed; `severity` says how badly, so a caller
    that wants to spend its repair budget only on the serious cases can. A stub
    or an empty artifact is major -- the work is simply not there. A thin or
    unstructured one is minor: it exists, it is just weaker than promised.
    """
    body = (artifact or "").strip()
    failures: List[str] = []
    major = False

    if not body:
        return CheckResult(ok=False, severity="major",
                           failures=["The artifact is empty. Nothing was written."])

    for pattern in spec.must_not_match:
        if re.search(pattern, body, re.I):
            major = True
            failures.append(
                f"The artifact is a placeholder, not the work: it matches "
                f"'{pattern}'. Write the actual content to the file."
            )

    if len(body) < spec.min_chars:
        # Well under the bar means nothing was really attempted; just under it
        # means the work is thin. Worth separating, because only the first is
        # worth a whole extra dispatch.
        if len(body) * 2 < spec.min_chars:
            major = True
        failures.append(
            f"The artifact is {len(body)} characters; this deliverable needs at "
            f"least {spec.min_chars}. Expand it with real detail, not filler."
        )

    cut = _looks_cut_off(body)
    if cut:
        # The step ran out of room mid-sentence and filed what it had. Observed:
        # a costs file ending at "Mand", which took its source with it and made
        # the whole mission unverified. Major, because the missing part is the
        # part nobody has: only a re-dispatch can produce it.
        major = True
        failures.append(
            f"The artifact stops mid-{cut}: it was cut off before it was "
            f"finished. Write it again, shorter if need be, so it ends on a "
            f"complete sentence and covers everything asked for."
        )

    if spec.require_heading and not _HEADING_RE.search(body):
        failures.append(
            "The artifact has no Markdown heading. Give it a title and section "
            "headings so the next step can navigate it."
        )

    for section in spec.required_sections:
        if section.lower() not in body.lower():
            failures.append(
                f"The required section '{section}' is missing. Add it as a "
                f"Markdown heading."
            )

    if spec.require_citations and not _CITATION_RE.search(body):
        failures.append(
            "No sources are cited. Include the URLs you used, so a reader can "
            "check the claims."
        )

    # ------------------------------------------------------------------- Structured data checks

    # 1. JSON Data Integrity & Completeness
    if spec.expected_format == "json" or (spec.filename and spec.filename.lower().endswith(".json")):
        cleaned = body
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```[a-zA-Z0-9_-]*\n?", "", cleaned)
            cleaned = re.sub(r"\n?```$", "", cleaned).strip()
        try:
            parsed = json.loads(cleaned)
        except Exception:
            parsed = None

        if parsed is None:
            major = True
            failures.append(
                "The file is not valid JSON. Format the deliverable as valid JSON syntax."
            )
        elif isinstance(parsed, list):
            if len(parsed) == 0:
                major = True
                failures.append(
                    "The JSON file contains an empty list []. Extract and include the requested records."
                )
            else:
                # Check for dropped/placeholder fields in list of dicts
                dict_records = [r for r in parsed if isinstance(r, dict)]
                if dict_records:
                    # Check required keys from spec or key substantive fields
                    keys_to_check = set(spec.expected_keys)
                    for r in dict_records:
                        keys_to_check.update(r.keys())

                    substantive = {"address", "store_name", "city", "name", "content", "summary", "text", "description", "details"}
                    for k in keys_to_check:
                        if k in spec.optional_keys:
                            continue
                        null_count = sum(
                            1 for r in dict_records
                            if r.get(k) is None or str(r.get(k)).strip().lower() in _PLACEHOLDERS
                        )
                        ratio = null_count / len(dict_records)
                        # Flag if over 50% of records have null/placeholder for a substantive or expected key
                        if (k in spec.expected_keys or k in substantive) and ratio > 0.50:
                            major = True
                            failures.append(
                                f"Incomplete data extraction: {null_count}/{len(dict_records)} records have "
                                f"null or placeholder values for field '{k}' ({ratio:.0%}). Extract real values "
                                f"from source files; do not emit placeholder nulls."
                            )

    # 2. CSV Data Integrity & Completeness
    elif spec.expected_format == "csv" or (spec.filename and spec.filename.lower().endswith(".csv")):
        try:
            reader = list(csv.reader(io.StringIO(body)))
            if len(reader) <= 1:
                major = True
                failures.append(
                    "The CSV file contains no data rows (only header or empty). Populate real data rows."
                )
            else:
                headers = [h.strip().lower() for h in reader[0]]
                data_rows = reader[1:]
                substantive_csv = {"address", "store_name", "city", "name", "content", "summary", "text", "description"}
                for idx, col_name in enumerate(headers):
                    if col_name in spec.optional_keys:
                        continue
                    na_count = sum(
                        1 for row in data_rows
                        if idx >= len(row) or row[idx].strip().lower() in _PLACEHOLDERS
                    )
                    ratio = na_count / len(data_rows)
                    if (col_name in spec.expected_keys or col_name in substantive_csv) and ratio > 0.60:
                        major = True
                        failures.append(
                            f"CSV column '{col_name}' is predominantly empty or N/A ({na_count}/{len(data_rows)} rows, "
                            f"{ratio:.0%}). Extract and populate real data from source files."
                        )
        except Exception:
            pass

    # 3. Markdown Completeness (Check for pervasive N/A placeholders)
    elif spec.expected_format == "markdown" or (spec.filename and spec.filename.lower().endswith(".md")):
        # Look for repetitive placeholder occurrences like: ' — N/A — ', 'Address: N/A', '| N/A |', 'Data unavailable'
        na_matches = re.findall(
            r"(?:—\s*N/A\b|:\s*N/A\b|\|\s*N/A\s*\||\|\s*null\s*\||Address:\s*N/A|Data unavailable\b|unavailable\s*—)",
            body, re.I
        )
        if len(na_matches) >= 3:
            major = True
            failures.append(
                f"The deliverable contains {len(na_matches)} placeholder 'N/A' or 'data unavailable' entries for substantive fields. "
                f"Extract complete real data from source files rather than emitting placeholder text."
            )

    if not failures:
        return CheckResult(ok=True, severity="none")
    return CheckResult(ok=False, failures=failures,
                       severity="major" if major else "minor")
