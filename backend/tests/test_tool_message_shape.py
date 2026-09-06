"""
The shape of the tool call/result pair in the message history.

The OpenAI wire format requires a `tool` message to be preceded by an assistant
message carrying the matching `tool_calls` entry. Send one without, and the
whole request is rejected -- not just that message:

    Invalid parameter: messages with role 'tool' must be a response to a
    preceeding message with 'tool_calls'.

Gemini accepted a bare `tool` message, so the search-cap refusal path shipped
one for as long as this project only ever pointed at Gemini. It surfaced the
first time a mission ran against OpenAI, and it killed the step.

The ids matter for the same reason. `call_{step}` collides the moment a model
returns two tool calls in one turn: both assistant messages claim the same id
and both results answer it.
"""
import json

from base_agent import _append_tool_exchange, _call_id


def _pairs(messages):
    """Every (assistant, tool) adjacency, as the provider will read them."""
    return list(zip(messages, messages[1:]))


def test_a_tool_result_is_always_preceded_by_its_call():
    messages = []
    _append_tool_exchange(messages, _call_id(1, 0), "web_search",
                          {"query": "ssd"}, {"success": True, "result": "x"})

    assert [m["role"] for m in messages] == ["assistant", "tool"]
    assistant, tool = messages
    assert assistant["tool_calls"][0]["id"] == tool["tool_call_id"]
    assert assistant["tool_calls"][0]["function"]["name"] == "web_search"
    assert json.loads(assistant["tool_calls"][0]["function"]["arguments"]) == {
        "query": "ssd"}
    assert json.loads(tool["content"]) == {"success": True, "result": "x"}


def test_every_tool_message_has_a_call_before_it():
    """The invariant the provider actually enforces, over a whole history."""
    messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
    for step in (1, 2):
        for index in (0, 1):
            _append_tool_exchange(messages, _call_id(step, index), "read_file",
                                  {"path": f"{step}.md"}, {"success": True})

    for before, after in _pairs(messages):
        if after["role"] == "tool":
            assert before["role"] == "assistant", (
                f"a tool message follows a {before['role']} message")
            assert before.get("tool_calls"), "the preceding assistant has no tool_calls"
            assert before["tool_calls"][0]["id"] == after["tool_call_id"]


def test_two_calls_in_one_turn_do_not_share_an_id():
    """
    `call_{step}` was the id, so a turn with two tool calls produced two
    assistant messages claiming the same one and two results answering it.
    """
    assert _call_id(3, 0) != _call_id(3, 1)

    messages = []
    _append_tool_exchange(messages, _call_id(3, 0), "web_search", {}, {"ok": 1})
    _append_tool_exchange(messages, _call_id(3, 1), "read_file", {}, {"ok": 2})

    ids = [m["tool_call_id"] for m in messages if m["role"] == "tool"]
    assert len(set(ids)) == 2, f"ids collided: {ids}"


def test_ids_are_unique_across_steps_too():
    seen = {_call_id(step, i) for step in range(1, 6) for i in range(3)}
    assert len(seen) == 15


def test_a_refusal_payload_travels_the_same_way_as_a_result():
    """
    The search-cap refusal is the path that shipped the bare `tool` message.
    It carries a different payload, not a different shape.
    """
    messages = []
    refusal = {"success": False, "error": "Search limit reached (5 searches per task)."}
    _append_tool_exchange(messages, _call_id(2, 0), "web_search",
                          {"query": "x"}, refusal)

    assert [m["role"] for m in messages] == ["assistant", "tool"]
    assert json.loads(messages[1]["content"])["success"] is False
    assert messages[0]["tool_calls"][0]["id"] == messages[1]["tool_call_id"]
