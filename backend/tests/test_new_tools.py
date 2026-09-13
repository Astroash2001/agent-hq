"""
The four tools added to widen what the office can do, and the guardrails on them.

Each tool is tested on its refusals before its successes. A tool that works is
worth little if the thing that stops it reaching somewhere it should not is
untested -- and for `fetch_url` in particular the refusal IS the feature: it is
the first outbound request in this harness whose destination is chosen by a
model rather than by this code.
"""
import json
import os
import shutil
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from config import settings                            # noqa: E402
from tool_registry import tool_registry                # noqa: E402
from tools.calculate import calculate                  # noqa: E402
from tools.fetch_url import _check_url, _is_public_address, _to_text, fetch_url   # noqa: E402
from tools.file_ops import bind_workspace              # noqa: E402
from tools.query_data import query_data                # noqa: E402
from tools.search_workspace import search_workspace    # noqa: E402
from workspace import MissionWorkspace                 # noqa: E402


# --------------------------------------------------------------- fetch_url

@pytest.mark.parametrize("url", [
    "file:///etc/passwd",
    "ftp://example.com/x",
    "gopher://example.com",
    "javascript:alert(1)",
])
def test_only_http_urls_are_fetchable(url):
    """
    `file://` would read the disk through the one tool whose whole contract is
    "this is the network" -- a way round file_ops containment rather than a
    fetch. The others are request-forgery primitives.
    """
    refusal = _check_url(url)
    assert refusal.get("refused") is True


@pytest.mark.parametrize("host", [
    "localhost",
    "127.0.0.1",
    "169.254.169.254",     # the cloud metadata service
    "10.0.0.1",
    "192.168.1.1",
    "0.0.0.0",
])
def test_private_addresses_are_refused(host):
    """
    The reason this tool needed a guardrail class the repo did not have.
    169.254.169.254 hands out cloud credentials; 127.0.0.1:8080 is this very
    server, which an agent could otherwise drive through its own tool.
    """
    assert _is_public_address(host) is False
    refusal = _check_url(f"http://{host}/whatever")
    assert refusal.get("refused") is True
    assert "public" in refusal["error"]


def test_a_name_that_does_not_resolve_is_not_treated_as_public():
    """
    "I could not tell" must not mean "it is fine" in the one check standing
    between a model-chosen string and this machine's private network.
    """
    assert _is_public_address("no-such-host.invalid") is False


def test_a_public_address_passes_the_policy():
    # Guards against a check so strict it refuses everything, which would pass
    # every test above while making the tool useless.
    assert _is_public_address("93.184.216.34") is True


def test_html_becomes_readable_text():
    html = ("<html><head><style>body{color:red}</style>"
            "<script>var x=1;</script></head>"
            "<body><h1>Title</h1><p>First&nbsp;line.</p><p>Second &amp; last.</p></body></html>")
    text = _to_text(html, "text/html")
    assert "Title" in text and "First line." in text and "Second & last." in text
    # The two things that must not survive: a stylesheet and a script arriving
    # as prose, which is what a naive tag-strip produces.
    assert "color:red" not in text
    assert "var x" not in text


class _FakeResponse:
    def __init__(self, text: str, status_code: int = 200, content_type: str = "text/html"):
        self.text = text
        self.status_code = status_code
        self.headers = {"content-type": content_type}


@pytest.mark.asyncio
async def test_fetch_url_offset_and_pagination(monkeypatch):
    sample_text = "<html><body>" + ("Alpha Beta Gamma Delta. " * 30) + "</body></html>"

    async def _mock_get(*args, **kwargs):
        return _FakeResponse(sample_text)

    monkeypatch.setattr("httpx.AsyncClient.get", _mock_get)

    # 1. First slice (offset 0, max_chars 50)
    res1 = await fetch_url("https://93.184.216.34/test", max_chars=50, offset=0)
    assert res1["success"] is True
    assert res1["truncated"] is True
    assert res1["offset"] == 0
    assert res1["next_offset"] == 50
    assert res1["total_chars"] > 50
    assert "offset=50" in res1["hint"]
    assert "[...content truncated at 50/" in res1["text"]

    # 2. Second slice (offset 50, max_chars 50)
    res2 = await fetch_url("https://93.184.216.34/test", max_chars=50, offset=50)
    assert res2["success"] is True
    assert res2["offset"] == 50
    assert res2["next_offset"] == 100

    # 3. Final slice reaching the end
    res3 = await fetch_url("https://93.184.216.34/test", max_chars=2000, offset=0)
    assert res3["success"] is True
    assert res3["truncated"] is False
    assert "next_offset" not in res3


@pytest.mark.asyncio
async def test_fetch_url_selector_extraction(monkeypatch):
    html = """
    <html><body>
        <div class="store">Store Alpha - Mumbai</div>
        <div class="store">Store Beta - Delhi</div>
        <footer class="footer">Ignored footer text</footer>
    </body></html>
    """
    async def _mock_get(*args, **kwargs):
        return _FakeResponse(html)

    monkeypatch.setattr("httpx.AsyncClient.get", _mock_get)

    res = await fetch_url("https://93.184.216.34/stores", selector="div.store")
    assert res["success"] is True
    assert "Store Alpha - Mumbai" in res["text"]
    assert "Store Beta - Delhi" in res["text"]
    assert "Ignored footer text" not in res["text"]


@pytest.mark.asyncio
async def test_fetch_url_save_as_workspace(monkeypatch):
    html = "<html><body><h1>Huge Catalog</h1><p>Store directory with 500 entries</p></body></html>"

    async def _mock_get(*args, **kwargs):
        return _FakeResponse(html)

    monkeypatch.setattr("httpx.AsyncClient.get", _mock_get)

    temp_dir = tempfile.mkdtemp()
    try:
        ws = MissionWorkspace("test_mission_fetch")
        ws.dir = temp_dir
        bind_workspace(ws, "agent1", "Agent", "step1")

        res = await fetch_url("https://93.184.216.34/directory", save_as="raw_catalog.html")
        assert res["success"] is True
        assert res["saved_to"] == "raw_catalog.html"
        assert res["bytes_saved"] == len(html)
        assert res["truncated"] is False
        assert "Huge Catalog" in res["preview"]

        saved_path = os.path.join(temp_dir, "raw_catalog.html")
        assert os.path.exists(saved_path)
        with open(saved_path, "r", encoding="utf-8") as f:
            assert f.read() == html
    finally:
        bind_workspace(None)
        shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.mark.asyncio
async def test_fetch_url_selector_with_save_as(monkeypatch):
    html = """
    <html><body>
        <div class="store-record">Store 1: Bangalore</div>
        <div class="store-record">Store 2: Chennai</div>
        <div class="nav">Home | About</div>
    </body></html>
    """
    async def _mock_get(*args, **kwargs):
        return _FakeResponse(html)

    monkeypatch.setattr("httpx.AsyncClient.get", _mock_get)

    temp_dir = tempfile.mkdtemp()
    try:
        ws = MissionWorkspace("test_mission_fetch2")
        ws.dir = temp_dir
        bind_workspace(ws, "agent1", "Agent", "step1")

        res = await fetch_url("https://93.184.216.34/stores", selector="div.store-record", save_as="stores.txt")
        assert res["success"] is True
        assert res["saved_to"] == "stores.txt"

        saved_path = os.path.join(temp_dir, "stores.txt")
        assert os.path.exists(saved_path)
        with open(saved_path, "r", encoding="utf-8") as f:
            content = f.read()
            assert "Store 1: Bangalore" in content
            assert "Store 2: Chennai" in content
            assert "Home | About" not in content
    finally:
        bind_workspace(None)
        shutil.rmtree(temp_dir, ignore_errors=True)


# --------------------------------------------------------------- calculate

@pytest.mark.asyncio
async def test_calculate_is_exact_where_a_model_would_guess():
    result = await calculate(expression="(4200 - 3150) / 3150 * 100")
    assert result["success"] is True
    assert round(result["result"], 6) == round(1050 / 3150 * 100, 6)


@pytest.mark.asyncio
async def test_calculate_returns_a_figure_that_can_be_grounded():
    """
    `synthesis.ungrounded` matches figures on their digits, so handing back
    0.30000000000000004 where 0.3 is meant puts an ungroundable number into the
    report this tool exists to keep honest.
    """
    result = await calculate(expression="0.1 + 0.2")
    assert result["result"] == 0.3


@pytest.mark.asyncio
@pytest.mark.parametrize("expression", [
    "__import__('os').system('echo hi')",
    "open('/etc/passwd').read()",
    "(lambda: 1)()",
    "[x for x in range(10)]",
    "pi * 2",
    "sqrt(16)",
    "().__class__.__bases__",
])
async def test_calculate_refuses_anything_that_is_not_literal_arithmetic(expression):
    """
    The allowlist is over AST node types, so every one of these is refused for
    the same structural reason rather than by recognising its spelling.
    """
    result = await calculate(expression=expression)
    assert result["success"] is False
    assert result.get("refused") is True


@pytest.mark.asyncio
async def test_calculate_refuses_an_expression_that_would_never_return():
    """9**9**9 is literal arithmetic that allocates until the process dies."""
    result = await calculate(expression="9**9**9")
    assert result["success"] is False
    assert result.get("refused") is True


@pytest.mark.asyncio
async def test_calculate_reports_division_by_zero_rather_than_raising():
    result = await calculate(expression="1/0")
    assert result["success"] is False
    assert "zero" in result["error"].lower()


# --------------------------------------------------------------- query_data

@pytest.fixture
def data_dir(monkeypatch):
    """
    A mission directory, nested one level inside its own scratch root.

    tempfile rather than pytest's tmp_path, for the reason the other suites
    give: pytest cannot create its tmp root on this machine.

    The nesting is not decoration. `file_ops._get_read_allowed_roots` allows
    `missions_path` AND its parent, so pointing `missions_path` straight at a
    mkdtemp directory quietly allows the whole system temp directory -- and a
    containment test written that way passes against a tool with no containment
    at all. Found exactly that way. The scratch root absorbs the parent grant,
    leaving somewhere genuinely outside to test against.
    """
    root = tempfile.mkdtemp(prefix="agenthq_querydata_")
    path = os.path.join(root, "missions")
    os.makedirs(path)
    monkeypatch.setattr(settings, "missions_path", path)
    try:
        yield path
    finally:
        shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def outside_file():
    """A readable file that is genuinely outside every allowed root."""
    directory = tempfile.mkdtemp(prefix="agenthq_outside_")
    path = os.path.join(directory, "secret.csv")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("a,b" + chr(10) + "1,2" + chr(10))
    try:
        yield path
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def _write_csv(directory):
    path = os.path.join(directory, "sales.csv")
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write("Region,Units Sold,Revenue\n")
        fh.write("North,120,4800\n")
        fh.write("South,80,3200\n")
        fh.write("North,60,2400\n")
    return path


@pytest.mark.asyncio
async def test_query_data_aggregates_exactly(data_dir):
    """The thing data_analyst had no way to do: ask a file a question."""
    path = _write_csv(data_dir)
    result = await query_data(
        filepath=path,
        query="SELECT region, SUM(revenue) AS total FROM data GROUP BY region ORDER BY total DESC",
    )
    assert result["success"] is True
    assert result["rows"] == [["North", 7200], ["South", 3200]]


@pytest.mark.asyncio
async def test_headers_become_addressable_column_names(data_dir):
    path = _write_csv(data_dir)
    result = await query_data(filepath=path, query="SELECT units_sold FROM data LIMIT 1")
    assert result["success"] is True, result.get("error")
    assert result["rows"] == [[120]]


@pytest.mark.asyncio
@pytest.mark.parametrize("query", [
    "DROP TABLE data",
    "INSERT INTO data VALUES ('x', 1, 2)",
    "UPDATE data SET revenue = 0",
    "DELETE FROM data",
    "ATTACH DATABASE '/tmp/evil.db' AS evil",
    "CREATE TABLE other (a)",
    # Lower case and a trailing comment: the two cheapest ways past a keyword
    # blocklist, and the reason this is enforced by SQLite's authorizer instead.
    "drop table data -- please",
])
async def test_query_data_is_read_only(data_dir, query):
    path = _write_csv(data_dir)
    result = await query_data(filepath=path, query=query)
    assert result["success"] is False
    assert result.get("refused") is True or "SQL error" in result.get("error", "")


@pytest.mark.asyncio
async def test_query_data_will_not_read_outside_the_jail(data_dir, outside_file):
    result = await query_data(filepath=outside_file, query="SELECT * FROM data")
    assert result["success"] is False
    assert result.get("refused") is True


@pytest.mark.asyncio
async def test_numbers_sort_as_numbers(data_dir):
    """
    The silent one. `csv.reader` hands back strings, and a text column sorts
    lexically -- '80' above '120' -- so the top row of a ranked table is the
    wrong row, with no error anywhere to say so.
    """
    path = _write_csv(data_dir)
    result = await query_data(
        filepath=path,
        query="SELECT region, revenue FROM data ORDER BY revenue DESC LIMIT 1",
    )
    assert result["success"] is True
    assert result["rows"] == [["North", 4800]]


@pytest.mark.asyncio
async def test_an_identifier_with_a_leading_zero_stays_text(data_dir):
    """'01234' is a postcode far more often than it is 1234."""
    path = os.path.join(data_dir, "codes.csv")
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write("postcode,count\n01234,5\n")
    result = await query_data(filepath=path, query="SELECT postcode, count FROM data")
    assert result["rows"] == [["01234", 5]]


@pytest.mark.asyncio
async def test_query_data_reads_json_rows(data_dir):
    path = os.path.join(data_dir, "rows.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump([{"city": "Pune", "pop": 7}, {"city": "Delhi", "pop": 32}], fh)
    result = await query_data(filepath=path, query="SELECT SUM(pop) FROM data")
    assert result["success"] is True
    assert result["rows"] == [[39]]


@pytest.mark.asyncio
async def test_a_bad_query_says_what_the_columns_are(data_dir):
    """An error that does not name the schema costs a whole turn to recover."""
    path = _write_csv(data_dir)
    result = await query_data(filepath=path, query="SELECT nope FROM data")
    assert result["success"] is False
    assert "region" in result["error"]


# ----------------------------------------------------------- search_workspace

@pytest.mark.asyncio
async def test_search_finds_the_line_without_reading_every_file(data_dir):
    for name, body in (
        ("a.md", "# Report\nThe figure is 4182 units.\n"),
        ("b.md", "# Notes\nNothing relevant here.\n"),
        ("c.md", "# Brief\nAlso mentions 4182 somewhere.\n"),
    ):
        with open(os.path.join(data_dir, name), "w", encoding="utf-8") as fh:
            fh.write(body)

    result = await search_workspace(pattern="4182", directory=data_dir)
    assert result["success"] is True
    assert result["match_count"] == 2
    assert {m["file"] for m in result["matches"]} == {"a.md", "c.md"}
    assert all(m["line"] == 2 for m in result["matches"])


@pytest.mark.asyncio
async def test_no_match_says_so_rather_than_returning_nothing(data_dir):
    """
    "No file contains this" is a finding an agent should act on. Returned as a
    bare empty list it is something to narrate around.
    """
    with open(os.path.join(data_dir, "a.md"), "w", encoding="utf-8") as fh:
        fh.write("nothing here\n")
    result = await search_workspace(pattern="missing-string", directory=data_dir)
    assert result["success"] is True
    assert result["match_count"] == 0
    assert "No match" in result["summary"]


@pytest.mark.asyncio
async def test_search_will_not_walk_outside_the_jail():
    result = await search_workspace(pattern="x", directory=tempfile.gettempdir())
    assert result["success"] is False
    assert result.get("refused") is True


@pytest.mark.asyncio
async def test_a_broken_regex_is_refused_not_raised(data_dir):
    result = await search_workspace(pattern="([unclosed", directory=data_dir, regex=True)
    assert result["success"] is False
    assert result.get("refused") is True


# ------------------------------------------------------- registry and grants

@pytest.mark.asyncio
async def test_a_tool_outside_the_grant_is_refused():
    """
    Until this check existed a grant was enforced only by omission: an agent is
    shown schemas for its own tools, so in practice it calls only those. That
    is a habit of the model, not a property of the system -- and it has to be a
    property before an agent's tool list is chosen at runtime.
    """
    result = await tool_registry.execute(
        tool_name="terminal",
        arguments={"command": "ls"},
        agent_id="researcher",
        granted=["web_search", "read_file"],
    )
    assert result["success"] is False
    assert result.get("refused") is True
    # The refusal names what IS available, so the model corrects rather than
    # retrying the same call with a synonym until the budget is gone.
    assert "web_search" in result["error"]


@pytest.mark.asyncio
async def test_a_granted_tool_runs():
    result = await tool_registry.execute(
        tool_name="calculate",
        arguments={"expression": "2+2"},
        agent_id="data_analyst",
        granted=["calculate"],
    )
    assert result["success"] is True
    assert result["result"] == 4


@pytest.mark.asyncio
async def test_an_internal_caller_passing_no_grant_is_not_blocked():
    """
    `None` means a caller that is not acting on a model's instruction -- the
    meeting machinery, a test. It is not an opt-out: base_agent always passes
    its list.
    """
    result = await tool_registry.execute(
        tool_name="calculate", arguments={"expression": "1+1"}, agent_id="x")
    assert result["success"] is True


def test_the_catalogue_is_the_authority_on_what_exists():
    """
    Anything choosing tools reads this rather than a hardcoded list, so a tool
    that does not exist cannot be granted, and one registered later is offered
    without those callers changing.
    """
    catalogue = tool_registry.catalogue()
    for name in ("web_search", "fetch_url", "calculate", "query_data",
                 "search_workspace", "terminal", "read_file", "write_file"):
        assert name in catalogue, name
        assert catalogue[name]["description"]
        assert catalogue[name]["risk"] in ("read", "write", "execute")
    fetch_schema = tool_registry.get_tool_schema("fetch_url")
    assert fetch_schema is not None
    fetch_params = fetch_schema["function"]["parameters"]["properties"]
    assert "offset" in fetch_params
    assert "save_as" in fetch_params
    assert "selector" in fetch_params


def test_risk_is_recorded_so_the_dangerous_tools_are_knowable():
    assert tool_registry.risk_of("terminal") == "execute"
    assert tool_registry.risk_of("fetch_url") == "execute"
    assert tool_registry.risk_of("calculate") == "read"
    assert tool_registry.risk_of("write_file") == "write"
    # An unknown tool is assumed dangerous rather than safe.
    assert tool_registry.risk_of("not_a_registered_tool") == "execute"


def test_every_granted_tool_actually_exists():
    """
    An agent manifest naming a tool the registry does not have is a step that
    will fail at the point the model tries to use it. Cheap to catch here.
    """
    import glob
    catalogue = tool_registry.catalogue()
    for path in glob.glob(os.path.join(settings.agents_dir, "*.json")):
        with open(path, encoding="utf-8-sig") as fh:
            manifest = json.load(fh)
        for tool in manifest.get("tools") or []:
            assert tool in catalogue, f"{manifest['id']} was granted unknown tool {tool}"


# ------------------------------------------------- read_file pagination

@pytest.fixture
def paging_dir(monkeypatch):
    """A mission directory nested inside its own scratch root, as above."""
    root = tempfile.mkdtemp(prefix="agenthq_readfile_")
    path = os.path.join(root, "missions")
    os.makedirs(path)
    monkeypatch.setattr(settings, "missions_path", path)
    try:
        yield path
    finally:
        shutil.rmtree(root, ignore_errors=True)


@pytest.mark.asyncio
async def test_a_small_file_reads_whole_and_says_it_is_whole(paging_dir):
    from tools.file_ops import read_file
    path = os.path.join(paging_dir, "small.txt")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("hello world")

    result = await read_file(path)
    assert result["success"] is True
    assert result["content"] == "hello world"
    assert result["truncated"] is False
    assert result["total_chars"] == 11
    assert result["chars_returned"] == 11
    assert "next_offset" not in result


@pytest.mark.asyncio
async def test_a_large_file_admits_the_cut(paging_dir):
    """
    The bug this exists for: `content[:8000]` with nothing in the reply saying
    a cut had happened, so a 45,000-character page arrived as 8,000 characters
    that looked complete. That is how an agent extracts 65 of 365 records and
    reports finishing.
    """
    from tools.file_ops import read_file
    path = os.path.join(paging_dir, "big.txt")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("X" * 45000)

    result = await read_file(path)
    assert result["success"] is True
    assert result["truncated"] is True
    assert result["total_chars"] == 45000
    assert result["chars_returned"] == settings.read_file_max_chars
    assert result["next_offset"] == settings.read_file_max_chars
    # The hint has to say what to DO, not merely that something was cut. This
    # is a .txt, so the alternative named is a script rather than SQL.
    assert "offset=" in result["hint"]
    assert "terminal" in result["hint"]


@pytest.mark.asyncio
async def test_paging_reassembles_the_whole_file(paging_dir):
    from tools.file_ops import read_file
    body = "".join(chr(97 + (i % 26)) for i in range(20000))
    path = os.path.join(paging_dir, "seq.txt")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(body)

    collected, offset, guard = "", 0, 0
    while True:
        guard += 1
        assert guard < 50, "pagination did not terminate"
        page = await read_file(path, offset=offset)
        # `content` carries a truncation banner after the slice, so a caller
        # reassembling the file takes the first `chars_returned` characters --
        # the metadata describes the real slice precisely so that this works.
        collected += page["content"][:page["chars_returned"]]
        if not page["truncated"]:
            break
        offset = page["next_offset"]

    assert collected == body, "paged content does not reassemble to the original"


@pytest.mark.asyncio
async def test_reading_past_the_end_says_so_rather_than_returning_nothing(paging_dir):
    """
    An agent that reads "" decides for itself what it means, and what it
    decides is usually that the file is empty. The same lesson as fetch_url.
    """
    from tools.file_ops import read_file
    path = os.path.join(paging_dir, "short.txt")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("only a little")

    result = await read_file(path, offset=9999)
    assert result["success"] is True
    assert result["end_of_content"] is True
    assert result["chars_returned"] == 0
    assert "past the end" in result["hint"]
    assert str(result["total_chars"]) in result["hint"]


@pytest.mark.asyncio
async def test_max_chars_is_honoured_and_capped(paging_dir):
    from tools.file_ops import read_file
    path = os.path.join(paging_dir, "big.txt")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("Y" * 30000)

    small = await read_file(path, max_chars=100)
    assert small["chars_returned"] == 100
    assert small["next_offset"] == 100

    # Above the configured ceiling, the ceiling wins -- the cap is a token
    # budget, so a caller must not be able to talk its way past it.
    huge = await read_file(path, max_chars=999999)
    assert huge["chars_returned"] == settings.read_file_max_chars


@pytest.mark.asyncio
async def test_read_file_still_refuses_outside_the_jail(paging_dir, outside_file):
    """Pagination must not have opened a way round containment."""
    from tools.file_ops import read_file
    result = await read_file(outside_file)
    assert result["success"] is False
    assert result.get("refused") is True


@pytest.mark.asyncio
async def test_a_truncated_csv_is_told_to_query_not_to_page(paging_dir):
    """
    Pagination alone does not make a large file readable, it only makes the
    failure loud. An agent has `max_agent_steps` turns for the whole step, and
    every chunk it reads stays in its history for the rest of them -- so paging
    a big file spends the budget and ends with the whole file in the prompt
    anyway. The agent then reports what it managed to read, which is the
    65-of-365 failure arriving by a different route.

    So the hint carries the arithmetic and the file's shape, which is what the
    agent needs to write one SQL query instead of six reads.
    """
    from tools.file_ops import read_file
    path = os.path.join(paging_dir, "stores.csv")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("store_id,name,city,pincode\n")
        for i in range(365):
            fh.write(f"{i},Store {i},City{i % 40},4{i:05d}\n")

    result = await read_file(path)
    hint = result["hint"]

    assert result["truncated"] is True
    # The numbers, done rather than left to the model.
    assert f"{result['total_chars']:,}" in hint
    assert str(settings.max_agent_steps) in hint
    # The shape, so the next call can be the right one.
    assert "store_id" in hint and "pincode" in hint
    assert "rows" in hint
    # The redirect, stated as a redirect.
    assert "query_data" in hint
    assert "Do not page" in hint
    # And it is in the content as well: a hint in a sibling field is one an
    # agent can skim past, and skimming past it is the whole failure.
    assert "[TRUNCATED]" in result["content"]


@pytest.mark.asyncio
async def test_a_truncated_plain_file_is_pointed_at_a_script(paging_dir):
    """No SQL over a .txt, so the alternative named is the one that exists."""
    from tools.file_ops import read_file
    path = os.path.join(paging_dir, "notes.txt")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("lorem ipsum " * 4000)

    hint = (await read_file(path))["hint"]
    assert "terminal" in hint
    assert "offset=" in hint
    assert "query_data" not in hint


@pytest.mark.asyncio
async def test_the_paged_content_still_reassembles_despite_the_banner(paging_dir):
    """
    The truncation banner is appended to `content`, so paging must strip it or
    a reassembled file would be salted with warnings. `chars_returned` and
    `next_offset` describe the real slice, which is what makes that possible.
    """
    from tools.file_ops import read_file
    body = "".join(chr(97 + (i % 26)) for i in range(20000))
    path = os.path.join(paging_dir, "seq.txt")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(body)

    collected, offset, guard = "", 0, 0
    while True:
        guard += 1
        assert guard < 50
        page = await read_file(path, offset=offset)
        # The real slice is the first `chars_returned` characters; the rest is
        # the banner. A caller that trusts the metadata reassembles exactly.
        collected += page["content"][:page["chars_returned"]]
        if not page["truncated"]:
            break
        offset = page["next_offset"]

    assert collected == body
