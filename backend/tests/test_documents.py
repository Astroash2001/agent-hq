"""
PDF and image attachments, and the tool that reads an image.

The PDF fixture is built by hand rather than with reportlab, so the suite gains
no dependency to test a feature whose whole point is not needing one. It is a
real PDF -- pypdf opens it and extracts the text -- which matters, because a
fixture that merely starts with %PDF- would pass a test of the magic-byte check
and prove nothing about extraction.
"""
import base64
import os
import shutil
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import attachments                                   # noqa: E402
from config import settings                          # noqa: E402
from tools.describe_image import _sniff, describe_image   # noqa: E402


# --------------------------------------------------------------- fixtures

def make_pdf(pages) -> bytes:
    """
    The smallest valid PDF carrying real, extractable text.

    Hand-assembled: object table, xref offsets and trailer. Longer than calling
    a library, and it keeps reportlab out of requirements-dev for one fixture.
    """
    objs, kids = [], []
    font_num = 3 + 2 * len(pages)
    for i, text in enumerate(pages):
        content_num = 3 + 2 * i
        page_num = content_num + 1
        stream = f"BT /F1 14 Tf 72 720 Td ({text}) Tj ET".encode("latin-1")
        objs.append((content_num,
                     b"<< /Length " + str(len(stream)).encode() + b" >>stream\n"
                     + stream + b"\nendstream"))
        objs.append((page_num,
                     (f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                      f"/Contents {content_num} 0 R /Resources << /Font << /F1 "
                      f"{font_num} 0 R >> >> >>").encode()))
        kids.append(f"{page_num} 0 R")
    objs.append((1, b"<< /Type /Catalog /Pages 2 0 R >>"))
    objs.append((2, ("<< /Type /Pages /Kids [" + " ".join(kids)
                     + f"] /Count {len(pages)} >>").encode()))
    objs.append((font_num, b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"))
    objs.sort()

    out = bytearray(b"%PDF-1.4\n")
    offsets = {}
    for num, body in objs:
        offsets[num] = len(out)
        out += f"{num} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_at = len(out)
    top = max(offsets) + 1
    out += f"xref\n0 {top}\n".encode() + b"0000000000 65535 f \n"
    for n in range(1, top):
        out += f"{offsets.get(n, 0):010d} 00000 n \n".encode()
    out += (f"trailer\n<< /Size {top} /Root 1 0 R >>\nstartxref\n"
            f"{xref_at}\n%%EOF\n").encode()
    return bytes(out)


# A 1x1 PNG. Smallest thing that is genuinely a PNG by its header.
PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmM"
    "IQAAAABJRU5ErkJggg==")


@pytest.fixture
def staging(monkeypatch):
    """
    A scratch staging directory.

    tempfile rather than pytest's tmp_path, for the reason the other suites
    give: pytest cannot create its tmp root on this machine.
    """
    path = tempfile.mkdtemp(prefix="agenthq_attach_")
    monkeypatch.setattr(settings, "attachments_path", path)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


# --------------------------------------------------------------- PDF staging

def test_a_pdf_is_staged_as_the_text_it_becomes(staging):
    """
    Converted at the door, so nothing downstream learns a new trick: `read()`
    returns text, the mission seeds it like any artifact, `read_file` reads it.
    """
    raw = make_pdf(["Quarterly revenue was 4182 units across 365 stores.",
                    "Page two: margin improved to 12.4 percent."])
    result = attachments.save("report.pdf", raw)

    assert result["kind"] == "pdf"
    assert result["name"] == "report.md", "a PDF is staged under its text name"
    assert result["source_name"] == "report.pdf"
    assert result["pages"] == 2
    assert result["pages_with_text"] == 2

    body = attachments.read("report.md")
    assert "4182" in body
    assert "12.4" in body
    # Page numbers survive, because that is what a citation needs.
    assert "Page 1" in body and "Page 2" in body
    # The binary is not kept: one attachment in, one file staged.
    assert [f["name"] for f in attachments.listing()] == ["report.md"]


def test_a_file_only_named_pdf_is_refused(staging):
    """Checked on the bytes. The extension is chosen by whoever uploads."""
    with pytest.raises(ValueError) as excinfo:
        attachments.save("fake.pdf", b"this is just text pretending")
    assert "does not look like one" in str(excinfo.value)


def test_a_pdf_with_no_text_says_it_is_probably_a_scan(staging):
    """
    An empty artifact would read as "this document says nothing", which is a
    different claim from "this document is a photograph of paper".
    """
    raw = make_pdf([])
    with pytest.raises(ValueError) as excinfo:
        attachments.save("scan.pdf", raw)
    message = str(excinfo.value)
    assert "no text could be extracted" in message
    assert "scan" in message


def test_extract_pdf_text_reports_pages(staging):
    out = attachments.extract_pdf_text(make_pdf(["Alpha", "Beta", "Gamma"]))
    assert out["ok"] is True
    assert out["pages"] == 3
    assert "Alpha" in out["text"] and "Gamma" in out["text"]


def test_a_corrupt_pdf_is_a_reason_not_a_traceback(staging):
    out = attachments.extract_pdf_text(b"%PDF-1.4\nnot really a pdf at all")
    assert out["ok"] is False
    assert out["reason"]


# ------------------------------------------------------------ image staging

def test_an_image_is_staged_as_bytes(staging):
    """
    The exception to "text only", and it earns it by having a reader.
    """
    result = attachments.save("chart.png", PNG_1PX)
    assert result["kind"] == "image"
    assert result["name"] == "chart.png"
    assert attachments.is_image("chart.png") is True
    assert os.path.isfile(attachments.path_of("chart.png"))
    # Byte-for-byte: a vision model needs the pixels, not a decoded approximation.
    with open(attachments.path_of("chart.png"), "rb") as fh:
        assert fh.read() == PNG_1PX


def test_reading_an_image_as_text_returns_nothing_rather_than_mojibake(staging):
    attachments.save("chart.png", PNG_1PX)
    assert attachments.read("chart.png") == ""


def test_an_unreadable_binary_is_still_refused(staging):
    with pytest.raises(ValueError) as excinfo:
        attachments.save("thing.exe", b"MZ\x00\x00binary garbage")
    assert "not a format an agent can read" in str(excinfo.value)


# --------------------------------------------------------- describe_image

@pytest.fixture
def image_dir(monkeypatch):
    root = tempfile.mkdtemp(prefix="agenthq_vision_")
    path = os.path.join(root, "missions")
    os.makedirs(path)
    monkeypatch.setattr(settings, "missions_path", path)
    try:
        yield path
    finally:
        shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def outside_image():
    directory = tempfile.mkdtemp(prefix="agenthq_outside_img_")
    path = os.path.join(directory, "secret.png")
    with open(path, "wb") as fh:
        fh.write(PNG_1PX)
    try:
        yield path
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def _fake_llm(monkeypatch, content="A bar chart. Q1 4182, Q2 5106.",
              cost=0.0021, prompt_tokens=812, completion_tokens=64):
    class _Message:
        pass

    message = _Message()
    message.content = content

    class _Choice:
        pass

    choice = _Choice()
    choice.message = message

    class _Response:
        choices = [choice]

    class _Result:
        response = _Response()
        model = "openai/gpt-4o-mini"

    _Result.prompt_tokens = prompt_tokens
    _Result.completion_tokens = completion_tokens
    _Result.cost_usd = cost

    captured = {}

    async def _acompletion(messages, **kwargs):
        captured["messages"] = messages
        captured["kwargs"] = kwargs
        return _Result()

    import tools.describe_image as mod
    monkeypatch.setattr(mod.llm, "acompletion", _acompletion)
    return captured


def test_sniffing_reads_the_header_not_the_extension():
    assert _sniff(PNG_1PX) == "image/png"
    assert _sniff(b"\xff\xd8\xff\xe0junk") == "image/jpeg"
    assert _sniff(b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP") == "image/webp"
    assert _sniff(b"MZ\x00\x00 an executable") is None


@pytest.mark.asyncio
async def test_describe_image_sends_the_image_and_returns_text(image_dir, monkeypatch):
    captured = _fake_llm(monkeypatch)
    path = os.path.join(image_dir, "chart.png")
    with open(path, "wb") as fh:
        fh.write(PNG_1PX)

    result = await describe_image(filepath=path)
    assert result["success"] is True
    assert result["mime_type"] == "image/png"
    assert "4182" in result["description"]
    # `text` as well, so the tool reads like every other one to an agent.
    assert result["text"] == result["description"]
    # Priced like any other spend: a tool that quietly costs money is one
    # nobody budgets for.
    assert result["cost_usd"] == 0.0021
    assert result["prompt_tokens"] == 812

    content = captured["messages"][0]["content"]
    kinds = [part["type"] for part in content]
    assert "image_url" in kinds and "text" in kinds
    url = next(p["image_url"]["url"] for p in content if p["type"] == "image_url")
    assert url.startswith("data:image/png;base64,")
    assert base64.b64decode(url.split(",", 1)[1]) == PNG_1PX


@pytest.mark.asyncio
async def test_a_custom_prompt_is_what_gets_asked(image_dir, monkeypatch):
    captured = _fake_llm(monkeypatch)
    path = os.path.join(image_dir, "table.png")
    with open(path, "wb") as fh:
        fh.write(PNG_1PX)

    await describe_image(filepath=path, prompt="Transcribe only the header row.")
    asked = next(p["text"] for p in captured["messages"][0]["content"]
                 if p["type"] == "text")
    assert asked == "Transcribe only the header row."


@pytest.mark.asyncio
async def test_describe_image_will_not_read_outside_the_jail(image_dir, outside_image, monkeypatch):
    """
    The most attractive thing in the harness to point at ~/.ssh/id_rsa: it
    reads bytes off disk and posts them to a third party.
    """
    called = {"n": 0}

    async def _boom(*args, **kwargs):
        called["n"] += 1
        raise AssertionError("the model must not be called for a refused path")

    import tools.describe_image as mod
    monkeypatch.setattr(mod.llm, "acompletion", _boom)

    result = await describe_image(filepath=outside_image)
    assert result["success"] is False
    assert result["refused"] is True
    assert called["n"] == 0, "refused before anything left the machine"


@pytest.mark.asyncio
async def test_a_non_image_is_refused_before_the_model_call(image_dir, monkeypatch):
    async def _boom(*args, **kwargs):
        raise AssertionError("the model must not be called for a non-image")

    import tools.describe_image as mod
    monkeypatch.setattr(mod.llm, "acompletion", _boom)

    path = os.path.join(image_dir, "notes.png")     # named .png, is not one
    with open(path, "wb") as fh:
        fh.write(b"just some text in a file")

    result = await describe_image(filepath=path)
    assert result["success"] is False
    assert result["refused"] is True
    assert "header" in result["error"]


@pytest.mark.asyncio
async def test_an_oversized_image_is_refused_before_it_is_encoded_into_a_prompt(
        image_dir, monkeypatch):
    monkeypatch.setattr(settings, "describe_image_max_encoded_chars", 10)

    async def _boom(*args, **kwargs):
        raise AssertionError("the model must not be called for an oversized image")

    import tools.describe_image as mod
    monkeypatch.setattr(mod.llm, "acompletion", _boom)

    path = os.path.join(image_dir, "huge.png")
    with open(path, "wb") as fh:
        fh.write(PNG_1PX)

    result = await describe_image(filepath=path)
    assert result["success"] is False
    assert result["refused"] is True
    assert "encoded" in result["error"]


@pytest.mark.asyncio
async def test_a_model_that_cannot_see_fails_loudly_and_says_what_to_change(
        image_dir, monkeypatch):
    async def _no_vision(*args, **kwargs):
        raise RuntimeError("this model does not support image input")

    import tools.describe_image as mod
    monkeypatch.setattr(mod.llm, "acompletion", _no_vision)

    path = os.path.join(image_dir, "chart.png")
    with open(path, "wb") as fh:
        fh.write(PNG_1PX)

    result = await describe_image(filepath=path)
    assert result["success"] is False
    # "this model cannot see" is a different problem from "the request failed",
    # and only the first is fixed by changing VISION_MODEL.
    assert "VISION_MODEL" in result["error"]


@pytest.mark.asyncio
async def test_an_empty_description_is_a_failure_not_a_blank_artifact(
        image_dir, monkeypatch):
    _fake_llm(monkeypatch, content="")
    path = os.path.join(image_dir, "chart.png")
    with open(path, "wb") as fh:
        fh.write(PNG_1PX)

    result = await describe_image(filepath=path)
    assert result["success"] is False
    assert "returned nothing" in result["error"]
    # The spend is still reported. A call that produced nothing usable still
    # cost money, and hiding that understates the run.
    assert "cost_usd" in result


def test_describe_image_is_registered_and_granted():
    import json
    import glob
    from tool_registry import tool_registry

    catalogue = tool_registry.catalogue()
    assert "describe_image" in catalogue
    assert catalogue["describe_image"]["risk"] == "execute"

    granted = set()
    for path in glob.glob(os.path.join(settings.agents_dir, "*.json")):
        with open(path, encoding="utf-8-sig") as fh:
            manifest = json.load(fh)
        if "describe_image" in (manifest.get("tools") or []):
            granted.add(manifest["id"])
    assert {"researcher", "data_analyst"} <= granted
