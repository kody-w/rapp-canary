"""Synthetic protocol tests: no Brainstem runtime, credentials, agents, or HTTP."""

from copy import deepcopy
import base64
import json
import socket

import pytest

from provider_plugins.base import ProviderError
from provider_plugins.responses import ResponsesPlugin


MODEL = {
    "id": "gpt-6-astra", "model_picker_enabled": True, "policy": {"state": "enabled"},
    "supported_endpoints": ["/responses", "ws:/responses"],
    "capabilities": {
        "type": "chat",
        "supports": {
            "tool_calls": True, "streaming": True, "parallel_tool_calls": True,
            "reasoning_effort": ["low", "medium", "high", "xhigh", "max"],
        },
        "limits": {"max_output_tokens": 128000},
    },
}
TOOLS = [
    {"type": "function", "function": {
        "name": "Echo", "description": "Echo synthetic text.",
        "parameters": {
            "type": "object", "properties": {"value": {"type": "string"}}, "required": [],
        },
    }},
    {"type": "function", "function": {
        "name": "Search", "parameters": {"type": "object", "properties": {}}, "strict": True,
    }},
]
OPAQUE = "opaque-reasoning-do-not-render"
SUMMARY = "private-reasoning-summary-do-not-render"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        pytest.fail("Responses unit tests attempted network access")
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)


@pytest.fixture
def plugin():
    return ResponsesPlugin()


def request(messages=None, **options):
    return {
        "model": MODEL["id"],
        "messages": deepcopy(messages if messages is not None else [{"role": "user", "content": "hello"}]),
        **deepcopy(options),
    }


def chat_call(call_id="call_1", name="Echo", arguments='{"value":"hi"}'):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}


def function_item(call_id="call_1", name="Echo", arguments='{"value":"hi"}'):
    return {
        "id": "fc_" + call_id, "type": "function_call", "status": "completed",
        "call_id": call_id, "name": name, "arguments": arguments,
    }


def text_item(text="hello", item_id="msg_0", kind="output_text"):
    return {
        "id": item_id, "type": "message", "status": "completed", "role": "assistant",
        "content": [{"type": kind, "refusal" if kind == "refusal" else "text": text}],
    }


def reasoning(label="one"):
    return {
        "id": "rs_" + label, "type": "reasoning", "encrypted_content": OPAQUE + label,
        "summary": [{"type": "summary_text", "text": SUMMARY}],
    }


def response(output=None, **options):
    return {
        "id": "resp_1", "object": "response", "model": MODEL["id"], "created_at": 123,
        "status": "completed", "output": deepcopy(output if output is not None else [text_item()]),
        **deepcopy(options),
    }


@pytest.mark.parametrize("actual", [None, "", "gpt-4o", "claude-haiku-4.5"])
def test_response_must_prove_the_selected_model_identity(plugin, actual):
    with pytest.raises(ProviderError):
        plugin.translate_response(response(model=actual), MODEL, {})


@pytest.mark.parametrize("actual", ["gpt-6-astra-2026-05-01", "gpt-6-astra-2026-05-01-preview"])
def test_response_accepts_a_more_specific_snapshot_of_the_requested_model(plugin, actual):
    # Copilot may echo back the concrete dated/variant snapshot it actually
    # served for a requested alias (e.g. gpt-5.5 -> gpt-5.5-2026-04-23).
    # That must still be accepted as the same model, not rejected.
    result = plugin.translate_response(response(model=actual), MODEL, {})
    assert result["model"] == actual


def test_response_accepts_a_shorter_canonical_alias_of_the_requested_model(plugin):
    # The inverse case: requesting a variant alias (e.g. gpt-5.6-sol-fast)
    # that Copilot serves and echoes back as its shorter canonical family
    # name (gpt-5.6-sol) must also be accepted.
    model = {**MODEL, "id": "gpt-6-astra-fast"}
    result = plugin.translate_response(response(model="gpt-6-astra"), model, {})
    assert result["model"] == "gpt-6-astra"


@pytest.mark.parametrize("actual", ["gpt-6-astray", "gpt-6-astra-x-other"])
def test_response_still_rejects_a_merely_similar_model_id(plugin, actual):
    # Guard the relaxed check against false positives: a similar-looking but
    # genuinely different model id must still be refused.
    with pytest.raises(ProviderError, match="different model"):
        plugin.translate_response(response(model=actual), MODEL, {})


def test_stream_cannot_label_another_model_as_astra(plugin):
    events = [
        "event: response.created",
        'data: {"type":"response.created","response":{"id":"resp_other","model":"gpt-4o","status":"in_progress"}}',
        "",
    ]
    with pytest.raises(ProviderError, match="different model"):
        list(plugin.translate_stream(events, MODEL, {}))


def test_stream_accepts_a_more_specific_snapshot_of_the_requested_model(plugin):
    events = [
        "event: response.created",
        'data: {"type":"response.created","response":{"id":"resp_1","model":"gpt-6-astra-2026-05-01","status":"in_progress"}}',
        "",
        "event: response.completed",
        'data: {"type":"response.completed","response":' + json.dumps(response(model="gpt-6-astra-2026-05-01")) + '}',
        "",
    ]
    list(plugin.translate_stream(events, MODEL, {}))


def test_copilot_wrapped_ids_can_rotate_but_final_ids_are_authoritative(plugin):
    def opaque(label):
        return base64.b64encode(label.encode() * 128).decode()

    final_item = text_item(item_id=opaque("g"))
    final = response([final_item], id=opaque("h"))
    events = [
        created(id=opaque("a")),
        {"type": "response.in_progress", "response": {
            "id": opaque("b"), "model": MODEL["id"], "created_at": 123, "status": "in_progress",
        }},
        {"type": "response.output_item.added", "output_index": 0, "item": {
            **text_item("", item_id=opaque("c")), "status": "in_progress",
        }},
        delta("hello", item_id=opaque("d")),
        {"type": "response.output_item.done", "output_index": 0,
         "item": text_item(item_id=opaque("f"))},
        terminal(final),
    ]
    result = list(plugin.translate_stream(frames(*events), MODEL, {}))[-1].completion
    assert result["id"] == opaque("h")
    assert result["model"] == MODEL["id"]
    assert result["choices"][0]["message"]["content"] == "hello"


def created(**options):
    return {
        "type": "response.created",
        "response": {
            "id": "resp_1", "model": MODEL["id"], "created_at": 123, "status": "in_progress",
            **options,
        },
    }


def delta(text="hello", index=0, content_index=0, kind="output_text", **options):
    return {
        "type": f"response.{kind}.delta", "delta": text,
        "output_index": index, "content_index": content_index, **options,
    }


def terminal(payload=None):
    return {"type": "response.completed", "response": payload if payload is not None else response()}


def frames(*events, multiline=False):
    lines = []
    for event in events:
        lines.extend([": harmless comment", "id: ignored-event-id", "retry: 1000", "extension: ignored"])
        lines.append("event: " + event["type"])
        data = json.dumps(event, ensure_ascii=False, indent=2 if multiline else None)
        lines.extend("data: " + line for line in data.splitlines())
        lines.append("")
    return lines


def stream_result(plugin, events, context=None):
    translated = list(plugin.translate_stream(frames(*events), MODEL, context if context is not None else {}))
    finals = [event.completion for event in translated if event.completion is not None]
    assert len(finals) == 1
    assert translated[-1].completion is finals[0]
    chunks = [event.chunk for event in translated if event.chunk is not None]
    assert all(chunk["object"] == "chat.completion.chunk" for chunk in chunks)
    assert all(chunk["id"] == finals[0]["id"] and chunk["model"] == finals[0]["model"] for chunk in chunks)
    assert chunks[-1]["choices"][0]["finish_reason"] == finals[0]["choices"][0]["finish_reason"]
    text = "".join(chunk["choices"][0]["delta"].get("content", "") for chunk in chunks)
    calls = {}
    for chunk in chunks:
        for call in chunk["choices"][0]["delta"].get("tool_calls", []):
            slot = calls.setdefault(call["index"], {
                "id": "", "type": "function", "function": {"name": "", "arguments": ""},
            })
            if call.get("id"):
                assert not slot["id"] or slot["id"] == call["id"]
                slot["id"] = call["id"]
            assert call.get("type", "function") == "function"
            for key in ("name", "arguments"):
                slot["function"][key] += call.get("function", {}).get(key, "")
    assembled = {"role": "assistant", "content": text or None}
    if calls:
        assembled["tool_calls"] = [calls[index] for index in sorted(calls)]
    assert assembled == finals[0]["choices"][0]["message"]
    return translated, finals[0]


def tool_result(call_id="call_1", content="tool output", name="Echo"):
    return {"role": "tool", "tool_call_id": call_id, "content": content, "name": name}


def test_spec_and_metadata_based_eligibility(plugin):
    assert plugin.spec.id == "responses"
    assert plugin.spec.api_version == 1
    assert plugin.spec.version == "1.0.0"
    assert plugin.spec.capabilities == frozenset({"catalog", "completion", "streaming", "tool-calls"})
    for model_id in ("gpt-6-astra", "gpt-5.5", "gpt-5.6-sol", "unrelated-future-model"):
        model = deepcopy(MODEL)
        model["id"] = model_id
        assert plugin.supports_model(model) is True
    model["supported_endpoints"].append("/chat/completions")
    model["id"] = "gpt-5.4"
    assert plugin.supports_model(model) is False


@pytest.mark.parametrize("change", [
    {"id": ""}, {"id": None}, {"id": "bad id"}, {"id": "bad\nid"},
    {"supported_endpoints": "/responses"},
    {"supported_endpoints": ["ws:/responses"]},
    {"supported_endpoints": ["/responses", "/chat/completions"]},
    {"supported_endpoints": ["/responses", None]},
    {"capabilities": {"type": "embeddings"}},
    {"capabilities": {"type": "utility", "supports": {"streaming": True, "tool_calls": True}}},
    {"capabilities": {"type": "chat"}},
    {"capabilities": {"type": "chat", "supports": {"streaming": False, "tool_calls": True}}},
    {"capabilities": {"type": "chat", "supports": {"streaming": True, "tool_calls": False}}},
    {"capabilities": None}, {"policy": {"state": "disabled"}}, {"model_picker_enabled": False},
])
def test_rejects_ineligible_model_metadata(plugin, change):
    model = {**deepcopy(MODEL), **change}
    assert plugin.supports_model(model) is False
    with pytest.raises(ProviderError):
        plugin.prepare_request(request(), model, {})


@pytest.mark.parametrize("model", [None, [], "gpt-6-astra", {}])
def test_malformed_catalog_entries_are_not_claimed(plugin, model):
    assert plugin.supports_model(model) is False
    with pytest.raises(ProviderError):
        plugin.translate_response(response(), model, {})
    with pytest.raises(ProviderError):
        list(plugin.translate_stream(frames(terminal()), model, {}))


def test_ordinary_text_messages_streaming_first_and_nonmutation(plugin):
    body = request([
        {"role": "system", "content": "system"},
        {"role": "developer", "content": "developer"},
        {"role": "user", "content": [{"type": "text", "text": "hello "}, {"type": "text", "text": "world"}]},
        {"role": "assistant", "content": "previous answer"},
        {"role": "user", "content": "follow-up"},
    ], stream=False)
    before, model = deepcopy(body), deepcopy(MODEL)
    context = {"unrelated": "retained"}
    prepared = plugin.prepare_request(body, model, context)
    assert prepared.endpoint == "/responses" and prepared.stream is True
    assert prepared.body == {
        "model": MODEL["id"],
        "input": [
            {"role": "system", "content": "system"}, {"role": "developer", "content": "developer"},
            {"role": "user", "content": "hello world"},
            {"role": "assistant", "content": "previous answer"}, {"role": "user", "content": "follow-up"},
        ],
        "stream": True, "store": False, "include": ["reasoning.encrypted_content"],
    }
    assert body == before and model == MODEL and context["unrelated"] == "retained"
    prepared.body["input"][0]["content"] = "changed"
    assert body == before


def test_optional_and_strict_tools_keep_original_schema(plugin):
    body = request(tools=TOOLS, tool_choice="auto")
    before = deepcopy(body)
    prepared = plugin.prepare_request(body, MODEL, {})
    assert prepared.body["tools"][0] == {"type": "function", "strict": False, **TOOLS[0]["function"]}
    assert prepared.body["tools"][0]["parameters"]["required"] == []
    assert "additionalProperties" not in prepared.body["tools"][0]["parameters"]
    assert prepared.body["tools"][1]["strict"] is True
    assert prepared.body["tool_choice"] == "auto"
    prepared.body["tools"][0]["parameters"]["required"].append("value")
    assert body == before


@pytest.mark.parametrize("choice,expected", [
    ("auto", "auto"), ("none", "none"), ("required", "required"),
    ({"type": "function", "function": {"name": "Echo"}}, {"type": "function", "name": "Echo"}),
])
def test_explicit_tool_choices(plugin, choice, expected):
    prepared = plugin.prepare_request(request(tools=TOOLS, tool_choice=choice), MODEL, {})
    assert prepared.body["tool_choice"] == expected


def test_zero_parameter_function(plugin):
    prepared = plugin.prepare_request(
        request(tools=[{"type": "function", "function": {"name": "NoArguments"}}]), MODEL, {},
    )
    assert prepared.body["tools"] == [{
        "type": "function", "name": "NoArguments", "strict": False,
        "parameters": {"type": "object", "properties": {}},
    }]


def test_multiple_calls_and_out_of_order_tool_results(plugin):
    calls = [chat_call(), chat_call("call_2", "Search", "{}")]
    body = request([
        {"role": "user", "content": "do both"},
        {"role": "assistant", "content": "checking", "tool_calls": calls},
        tool_result("call_2", "second", "Search"), tool_result(content="first"),
    ], tools=TOOLS, parallel_tool_calls=True)
    before = deepcopy(body)
    items = plugin.prepare_request(body, MODEL, {}).body["input"]
    assert items == [
        {"role": "user", "content": "do both"}, {"role": "assistant", "content": "checking"},
        {"type": "function_call", "call_id": "call_1", "name": "Echo", "arguments": '{"value":"hi"}'},
        {"type": "function_call", "call_id": "call_2", "name": "Search", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "call_2", "output": "second"},
        {"type": "function_call_output", "call_id": "call_1", "output": "first"},
    ]
    assert body == before


def test_final_tool_less_synthesis_and_none_choice(plugin):
    messages = [
        {"role": "user", "content": "finish"}, {"role": "assistant", "content": None, "tool_calls": [chat_call()]},
        tool_result(),
    ]
    for options in ({}, {"tools": []}, {"tool_choice": "none"}):
        context = {}
        prepared = plugin.prepare_request(request(messages, **options), MODEL, context)
        assert "tools" not in prepared.body
        assert prepared.body.get("tool_choice") == options.get("tool_choice")
        assert prepared.body["input"][-1]["type"] == "function_call_output"
        with pytest.raises(ProviderError, match="disallowed"):
            plugin.translate_response(response([function_item("call_new")]), MODEL, context)


@pytest.mark.parametrize("field", ["max_tokens", "max_completion_tokens"])
def test_supported_request_options_are_mapped_not_forwarded(plugin, field):
    prepared = plugin.prepare_request(request(
        **{field: 321}, reasoning_effort="high", stream_options={"include_usage": True}, n=1,
    ), MODEL, {})
    assert prepared.body["max_output_tokens"] == 321
    assert prepared.body["reasoning"] == {"effort": "high"}
    assert not {"messages", "stream_options", "n", field, "reasoning_effort"} & prepared.body.keys()
    default = plugin.prepare_request(request(), MODEL, {}).body
    assert "reasoning" not in default and "reasoning_effort" not in default


@pytest.mark.parametrize("options", [
    {"reasoning_effort": "none"}, {"reasoning_effort": 2}, {"stream": "yes"},
    {"n": 2}, {"n": True}, {"temperature": 0.5}, {"stop": ["stop"]},
    {"max_tokens": 0}, {"max_tokens": True}, {"max_tokens": -1}, {"max_tokens": 128001},
    {"max_tokens": 100, "max_completion_tokens": 100},
    {"stream_options": {"unsupported": True}}, {"stream_options": {"include_usage": 1}},
    {"tools": None}, {"tools": [{}]}, {"tools": [{"type": "web_search"}]},
    {"tools": [TOOLS[0], TOOLS[0]]},
    {"tools": [{"type": "function", "function": {"name": "Echo", "strict": None}}]},
    {"tools": [{"type": "function", "function": {"name": "Echo", "parameters": []}}]},
    {"tool_choice": "required"}, {"tool_choice": "bogus"}, {"tool_choice": None},
    {"tool_choice": {"type": "function", "function": {"name": "Missing"}}},
])
def test_unsupported_request_options_fail_without_context_mutation(plugin, options):
    context = {"unrelated": "value"}
    with pytest.raises(ProviderError):
        plugin.prepare_request(request(**options), MODEL, context)
    assert context == {"unrelated": "value"}


def test_parallel_calls_require_advertised_support(plugin):
    model = deepcopy(MODEL)
    model["capabilities"]["supports"]["parallel_tool_calls"] = False
    with pytest.raises(ProviderError):
        plugin.prepare_request(request(tools=TOOLS, parallel_tool_calls=True), model, {})
    assert plugin.prepare_request(
        request(tools=TOOLS, parallel_tool_calls=False), model, {},
    ).body["parallel_tool_calls"] is False


@pytest.mark.parametrize("messages", [
    [], None, "hello", [{}],
    [{"role": "user", "content": None}],
    [{"role": "user", "content": {"text": "wrong shape"}}],
    [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "unused"}}]}],
    [{"role": "user", "content": [{"type": "text", "text": "hello", "ignored": "not allowed"}]}],
    [{"role": "function", "content": "legacy function"}],
    [{"role": "user", "content": "hello", "name": "unsupported alias"}],
    [{"role": "assistant", "content": None}],
    [{"role": "assistant", "content": "hello", "audio": {}}],
    [{"role": "assistant", "tool_calls": [chat_call()]}],
    [tool_result()],
    [{"role": "assistant", "tool_calls": [chat_call()]}, tool_result(name="Search")],
    [{"role": "assistant", "tool_calls": [chat_call()]}, tool_result(), tool_result()],
    [{"role": "assistant", "tool_calls": [chat_call(), chat_call()]}, tool_result()],
    [{"role": "assistant", "tool_calls": [chat_call()]}, {"role": "user", "content": "no result"}],
    [{"role": "assistant", "tool_calls": [chat_call("")]}, tool_result("")],
    [{"role": "assistant", "tool_calls": [chat_call("bad id")]}, tool_result("bad id")],
    [{"role": "assistant", "tool_calls": [chat_call(arguments="invalid")]}, tool_result()],
    [{"role": "assistant", "tool_calls": [chat_call(arguments="[]")]}, tool_result()],
    [{"role": "assistant", "tool_calls": [chat_call(arguments='{"x":NaN}')]}, tool_result()],
    [{"role": "assistant", "tool_calls": [chat_call(arguments='{"x":1,"x":2}')]}, tool_result()],
])
def test_malformed_and_unsupported_message_history(plugin, messages):
    body = {"model": MODEL["id"], "messages": messages}
    before = deepcopy(body)
    with pytest.raises(ProviderError):
        plugin.prepare_request(body, MODEL, {})
    assert body == before


def test_text_completion_usage_metadata_and_nonmutation(plugin):
    payload = response([text_item("Hello "), text_item("world", "msg_1")], usage={
        "input_tokens": 20, "output_tokens": 10, "total_tokens": 30,
        "input_tokens_details": {"cached_tokens": 5}, "output_tokens_details": {"reasoning_tokens": 7},
    })
    before = deepcopy(payload)
    completion = plugin.translate_response(payload, MODEL, {})
    assert completion == {
        "id": "resp_1", "object": "chat.completion", "created": 123, "model": MODEL["id"],
        "choices": [{
            "index": 0, "message": {"role": "assistant", "content": "Hello world"}, "finish_reason": "stop",
        }],
        "usage": {
            "prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30,
            "prompt_tokens_details": {"cached_tokens": 5}, "completion_tokens_details": {"reasoning_tokens": 7},
        },
    }
    completion["usage"]["prompt_tokens_details"]["cached_tokens"] = 0
    assert payload == before


def test_multiple_functions_and_interleaved_text_are_one_choice(plugin):
    output = [
        text_item("First. "), reasoning(), function_item(),
        text_item("Second.", "msg_1"), function_item("call_2", "Search", "{}"),
    ]
    context = {}
    plugin.prepare_request(request(tools=TOOLS), MODEL, context)
    completion = plugin.translate_response(response(output), MODEL, context)
    assert len(completion["choices"]) == 1
    choice = completion["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    assert choice["message"] == {
        "role": "assistant", "content": "First. Second.",
        "tool_calls": [chat_call(), chat_call("call_2", "Search", "{}")],
    }
    assert OPAQUE not in json.dumps(completion) and SUMMARY not in json.dumps(completion)


@pytest.mark.parametrize("streaming", [False, True])
def test_opaque_reasoning_round_trip_and_receipt_copies(plugin, streaming):
    body, context = request(tools=TOOLS), {}
    plugin.prepare_request(body, MODEL, context)
    output = [reasoning(), text_item("Checking."), function_item()]
    payload = response(output)
    if streaming:
        _, completion = stream_result(plugin, [terminal(payload)], context)
    else:
        completion = plugin.translate_response(payload, MODEL, context)
    assistant = deepcopy(completion["choices"][0]["message"])
    payload["output"][0]["encrypted_content"] = "changed externally"
    completion["choices"][0]["message"]["content"] = "changed externally"
    follow_up = request([*body["messages"], assistant, tool_result()])
    prepared = plugin.prepare_request(follow_up, MODEL, context)
    assert prepared.body["input"] == [
        *body["messages"], *output,
        {"type": "function_call_output", "call_id": "call_1", "output": "tool output"},
    ]
    prepared.body["input"][1]["encrypted_content"] = "mutated request"
    assert plugin.prepare_request(follow_up, MODEL, context).body["input"][1] == output[0]
    assert "previous_response_id" not in prepared.body


def test_receipts_require_exact_generated_assistant_and_prior_tool_history(plugin):
    context, original = {}, request(tools=TOOLS)
    plugin.prepare_request(original, MODEL, context)
    first = plugin.translate_response(response([reasoning("one"), function_item()]), MODEL, context)
    history = [*original["messages"], first["choices"][0]["message"], tool_result(content="actual first result")]
    plugin.prepare_request(request(history, tools=TOOLS), MODEL, context)
    second = plugin.translate_response(response([
        reasoning("two"), function_item("call_2"),
    ], id="resp_2"), MODEL, context)
    complete_history = [*history, second["choices"][0]["message"], tool_result("call_2")]
    prepared = plugin.prepare_request(request(complete_history), MODEL, context)
    assert [item["id"] for item in prepared.body["input"] if item.get("type") == "reasoning"] == ["rs_one", "rs_two"]
    changed = deepcopy(complete_history)
    changed[2]["content"] = "a different earlier tool result"
    prepared = plugin.prepare_request(request(changed), MODEL, context)
    assert [item["id"] for item in prepared.body["input"] if item.get("type") == "reasoning"] == ["rs_one"]
    changed[1]["tool_calls"][0]["function"]["arguments"] = '{"value":"unrelated"}'
    prepared = plugin.prepare_request(request(changed), MODEL, context)
    assert not any(item.get("type") == "reasoning" for item in prepared.body["input"])


def test_unrelated_prompts_and_independent_contexts_never_reuse_receipts(plugin):
    context, body = {}, request(tools=TOOLS)
    plugin.prepare_request(body, MODEL, context)
    completion = plugin.translate_response(response([reasoning(), function_item()]), MODEL, context)
    history = [*body["messages"], completion["choices"][0]["message"], tool_result()]
    other_context = {}
    independent = plugin.prepare_request(request(history), MODEL, other_context)
    assert OPAQUE not in json.dumps(independent.body)
    history[0]["content"] = "unrelated prompt"
    unrelated = plugin.prepare_request(request(history), MODEL, context)
    assert OPAQUE not in json.dumps(unrelated.body)
    other_model = {**deepcopy(MODEL), "id": "different-responses-model"}
    with pytest.raises(ProviderError, match="context"):
        plugin.prepare_request({**request(), "model": other_model["id"]}, other_model, context)


@pytest.mark.parametrize("streaming", [False, True])
def test_completed_refusal_is_visible_without_reasoning(plugin, streaming):
    payload = response([reasoning(), text_item("I cannot help with that.", "msg_0", "refusal")])
    if streaming:
        events, completion = stream_result(plugin, [
            created(), delta("I cannot ", index=1, kind="refusal"),
            delta("help with that.", index=1, kind="refusal"), terminal(payload),
        ])
        assert OPAQUE not in repr(events) and SUMMARY not in repr(events)
    else:
        completion = plugin.translate_response(payload, MODEL, {})
    assert completion["choices"][0]["message"]["content"] == "I cannot help with that."
    assert completion["choices"][0]["finish_reason"] == "stop"
    assert OPAQUE not in repr(completion) and SUMMARY not in repr(completion)


def test_refusal_input_is_text_only_and_preserved(plugin):
    prepared = plugin.prepare_request(request([
        {"role": "assistant", "content": None, "refusal": "I cannot help."},
        {"role": "user", "content": "A different question"},
    ]), MODEL, {})
    assert prepared.body["input"][0] == {"role": "assistant", "content": "I cannot help."}


@pytest.mark.parametrize("payload", [
    None, [], {}, response(status="incomplete"), response(status="failed"),
    response(status="in_progress"), response(status="cancelled"),
    response(error={"message": OPAQUE}), response(incomplete_details={"reason": OPAQUE}),
    response(output=[]), {**response(), "output": None}, response(output="wrong"),
    response([reasoning()]), response([text_item("")]), response(id=""),
    response([{"type": "web_search_call", "status": "completed"}]),
    response([{"type": "message", "role": "assistant", "content": []}]),
    response([{"type": "message", "role": "user", "content": [{"type": "output_text", "text": "wrong role"}]}]),
    response([{"type": "message", "role": "assistant", "content": [{"type": "audio", "data": OPAQUE}]}]),
    response([{"type": "message", "role": "assistant", "content": [{"type": [], "text": "wrong type"}]}]),
    response([{**text_item(), "status": "incomplete"}]),
    response([text_item(), text_item()]),
    response([function_item(), function_item()]),
    response([function_item("bad id")]),
    response([function_item(arguments="not JSON")]),
    response([function_item(arguments="[]")]),
    response([function_item(name="bad.name")]),
    response(usage={"input_tokens": True, "output_tokens": 1}),
    response(usage={"input_tokens": 1, "output_tokens": 2, "total_tokens": 9}),
    response(usage={"input_tokens": 1, "output_tokens": 2, "input_tokens_details": {"cached_tokens": 2}}),
])
def test_malformed_failed_and_incomplete_responses_do_not_create_receipts(plugin, payload):
    context = {}
    plugin.prepare_request(request(tools=TOOLS), MODEL, context)
    before = deepcopy(context)
    with pytest.raises(ProviderError) as error:
        plugin.translate_response(payload, MODEL, context)
    assert OPAQUE not in str(error.value) and SUMMARY not in str(error.value)
    assert context == before


@pytest.mark.parametrize("choice", ["none", {"type": "function", "function": {"name": "Search"}}])
def test_output_cannot_override_tool_choice(plugin, choice):
    context = {}
    plugin.prepare_request(request(tools=TOOLS, tool_choice=choice), MODEL, context)
    with pytest.raises(ProviderError):
        plugin.translate_response(response([function_item()]), MODEL, context)


@pytest.mark.parametrize("choice", ["required", {"type": "function", "function": {"name": "Echo"}}])
def test_completed_refusal_takes_precedence_over_forced_tool_choice(plugin, choice):
    context = {}
    plugin.prepare_request(request(tools=TOOLS, tool_choice=choice), MODEL, context)
    completion = plugin.translate_response(response([
        text_item("I cannot perform that action.", kind="refusal"),
    ]), MODEL, context)
    assert completion["choices"][0]["message"]["content"] == "I cannot perform that action."
    assert completion["choices"][0]["finish_reason"] == "stop"


def test_output_cannot_reuse_prior_call_id_or_exceed_parallel_choice(plugin):
    context = {}
    plugin.prepare_request(request([
        {"role": "assistant", "tool_calls": [chat_call()]}, tool_result(),
    ], tools=TOOLS), MODEL, context)
    with pytest.raises(ProviderError, match="reused"):
        plugin.translate_response(response([function_item()]), MODEL, context)
    plugin.prepare_request(request(tools=TOOLS, parallel_tool_calls=False), MODEL, context)
    with pytest.raises(ProviderError, match="disallowed"):
        plugin.translate_response(response([function_item(), function_item("call_2")]), MODEL, context)


@pytest.mark.parametrize("multiline", [False, True])
@pytest.mark.parametrize("ending", ["", "\n", "\r\n"])
def test_fragmented_text_and_proper_sse_framing(plugin, multiline, ending):
    events = [
        created(),
        {"type": "response.reasoning_summary_text.delta", "delta": SUMMARY},
        delta("hé", item_id="msg_0"), delta("llo 🧠", item_id="msg_0"),
        {"type": "response.output_text.done", "output_index": 0, "content_index": 0, "text": "héllo 🧠"},
        terminal(response([text_item("héllo 🧠")])),
    ]
    lines = frames(*events, multiline=multiline) + ["data: [DONE]", "", ": closing comment", ""]
    lines[0] = "\ufeff" + lines[0]
    translated = list(plugin.translate_stream([line + ending for line in lines], MODEL, {}))
    assert translated[-1].completion["choices"][0]["message"]["content"] == "héllo 🧠"
    assert sum(event.completion is not None for event in translated) == 1
    assert "".join(
        event.chunk["choices"][0]["delta"].get("content", "")
        for event in translated if event.chunk is not None
    ) == "héllo 🧠"
    assert OPAQUE not in repr(translated) and SUMMARY not in repr(translated)


def test_final_only_text_and_tools_produce_valid_chat_chunks(plugin):
    for output in (
        [text_item()],
        [text_item("I cannot perform that action.", kind="refusal")],
        [text_item(""), function_item()],
        [reasoning(), function_item(), function_item("call_2", "Search", "{}")],
    ):
        context = {}
        plugin.prepare_request(request(tools=TOOLS), MODEL, context)
        events, completion = stream_result(plugin, [terminal(response(output))], context)
        calls = completion["choices"][0]["message"].get("tool_calls", [])
        emitted = [
            call for event in events if event.chunk is not None
            for call in event.chunk["choices"][0]["delta"].get("tool_calls", [])
        ]
        assert emitted == [{"index": index, **call} for index, call in enumerate(calls)]
        assert OPAQUE not in repr(events)


def test_empty_text_deltas_with_tool_calls_normalize_to_none(plugin):
    events, completion = stream_result(plugin, [
        created(), delta(""), terminal(response([text_item(""), function_item()])),
    ])
    assert completion["choices"][0]["message"]["content"] is None
    assert not any(
        "content" in event.chunk["choices"][0]["delta"]
        for event in events if event.chunk is not None
    )


def test_deferred_deltas_and_mixed_refusal_parts_match_final_completion(plugin):
    item = text_item("A visible answer. ")
    item["content"].append({"type": "refusal", "refusal": "I cannot perform the other action."})
    events, completion = stream_result(plugin, [
        delta("A visible "), delta("answer. "),
        delta("I cannot perform ", content_index=1, kind="refusal"),
        delta("the other action.", content_index=1, kind="refusal"),
        terminal(response([item, function_item()])),
    ])
    assert completion["choices"][0]["message"]["content"] == (
        "A visible answer. I cannot perform the other action."
    )
    assert sum(event.completion is not None for event in events) == 1


def test_tool_argument_fragments_are_validated_but_emitted_only_after_eof(plugin):
    context = {}
    plugin.prepare_request(request(tools=TOOLS), MODEL, context)
    output = [text_item("Checking"), reasoning(), function_item()]
    source = [
        created(), delta("Checking"),
        {"type": "response.output_item.added", "output_index": 2, "item": {
            **function_item(), "status": "in_progress", "arguments": "",
        }},
        {"type": "response.function_call_arguments.delta", "output_index": 2, "item_id": "fc_call_1", "delta": '{"value":'},
        {"type": "response.function_call_arguments.delta", "output_index": 2, "delta": '"hi"}'},
        {"type": "response.function_call_arguments.done", "output_index": 2, "arguments": '{"value":"hi"}'},
        {"type": "response.output_item.done", "output_index": 2, "item": function_item()},
        terminal(response(output)),
    ]
    exhausted = False
    def lines():
        nonlocal exhausted
        yield from frames(*source)
        exhausted = True
    events = []
    for event in plugin.translate_stream(lines(), MODEL, context):
        if event.chunk and event.chunk["choices"][0]["delta"].get("tool_calls"):
            assert exhausted
        events.append(event)
    assert events[-1].completion["choices"][0]["message"]["tool_calls"] == [chat_call()]
    assert OPAQUE not in repr(events)


def test_streamed_parallel_calls_and_interleaved_text(plugin):
    output = [text_item("One"), function_item(), text_item("Two", "msg_2"), function_item("call_2", "Search", "{}")]
    source = [
        created(), delta("One", item_id="msg_0"),
        {"type": "response.function_call_arguments.delta", "output_index": 1, "delta": '{"value":"hi"}'},
        delta("Two", index=2, item_id="msg_2"),
        {"type": "response.function_call_arguments.delta", "output_index": 3, "delta": "{}"},
        terminal(response(output)),
    ]
    events, completion = stream_result(plugin, source)
    assert completion["choices"][0]["message"]["content"] == "OneTwo"
    assert completion["choices"][0]["message"]["tool_calls"] == [chat_call(), chat_call("call_2", "Search", "{}")]
    assert len([event for event in events if event.chunk and event.chunk["choices"][0]["delta"].get("tool_calls")]) == 1


def test_content_part_and_item_done_events_agree(plugin):
    item = text_item()
    part = item["content"][0]
    stream_result(plugin, [
        created(),
        {"type": "response.output_item.added", "output_index": 0, "item": {
            **item, "status": "in_progress", "content": [],
        }},
        {"type": "response.content_part.added", "output_index": 0, "content_index": 0, "part": {
            "type": "output_text", "text": "",
        }},
        delta(),
        {"type": "response.output_text.done", "output_index": 0, "content_index": 0, "text": "hello"},
        {"type": "response.content_part.done", "output_index": 0, "content_index": 0, "part": part},
        {"type": "response.output_item.done", "output_index": 0, "item": item},
        terminal(),
    ])


@pytest.mark.parametrize("events", [
    [], [created()], [created(), delta()],
    [created(), {"type": "error", "message": OPAQUE}],
    [{"type": "response.failed", "response": {"status": "failed", "error": {"message": OPAQUE}}}],
    [{"type": "response.incomplete", "response": {"status": "incomplete"}}],
    [created(), delta("short"), terminal()],
    [created(), delta("hello"), terminal(response([text_item("hello extra")]))],
    [created(), delta("hello", index=1), terminal()],
    [created(), delta("hello", kind="refusal"), terminal()],
    [created(), delta("hello", item_id="wrong"), terminal()],
    [created(), delta("hello", output_index=-1), terminal()],
    [created(), delta("hello", output_index=True), terminal()],
    [created(), delta("hello", content_index=None), terminal()],
    [created(), delta("hello", delta={}), terminal()],
    [created(), delta(), terminal(response(id="different"))],
    [created(), terminal(response(model="different"))],
    [created(), terminal(), delta("late")],
    [created(), terminal(), {"type": "response.failed", "response": {"error": {"message": OPAQUE}}}],
    [terminal(), terminal()],
    [created(), {"type": "response.audio.delta", "delta": OPAQUE}, terminal()],
    [created(), {"type": "response.output_item.added", "output_index": 0, "item": {"type": []}}, terminal()],
    [created(), {"type": "response.content_part.added", "output_index": 0, "content_index": 0,
                 "part": {"type": [], "text": "wrong type"}}, terminal()],
    [created(), delta("hello", response_id="different-response"), terminal()],
    [created(), delta("hello"), {"type": "response.output_text.done", "output_index": 0, "content_index": 0, "text": "different"}, terminal()],
    [created(), {"type": "response.output_text.done", "output_index": 0, "content_index": 0, "text": "hello"}, delta(), terminal()],
    [created(), {"type": "response.output_item.done", "output_index": 0, "item": text_item()}, delta(), terminal()],
    [created(), {"type": "response.output_item.done", "output_index": 0, "item": text_item("changed")}, terminal()],
    [created(), {"type": "response.output_item.added", "output_index": 0, "item": {
        **text_item("changed"), "status": "in_progress",
    }}, terminal()],
    [created(), {"type": "response.output_item.added", "output_index": 0, "item": text_item("he")}, terminal()],
    [created(), {"type": "response.output_item.added", "output_index": 0, "item": {
        **text_item(), "status": "failed",
    }}, terminal()],
    [created(), {"type": "response.function_call_arguments.delta", "output_index": 0, "delta": "{}"}, terminal(response([function_item()]))],
    [created(), {"type": "response.function_call_arguments.done", "output_index": 0, "arguments": "invalid"}, terminal(response([function_item()]))],
    [created(), {"type": "response.function_call_arguments.done", "output_index": 0,
                 "name": "Search", "arguments": '{"value":"hi"}'}, terminal(response([function_item()]))],
    [created(), {"type": "response.output_item.added", "output_index": 0, "item": function_item()},
     {"type": "response.output_item.added", "output_index": 1, "item": {**function_item(), "id": "fc_other"}},
     terminal(response([function_item(), function_item("call_2")]))],
    [created(), {"type": "response.output_item.added", "output_index": 0, "item": function_item()},
     terminal(response([function_item("call_different")]))],
    [created(), delta("Two", index=1), delta("One", index=0), terminal(response([text_item("One"), text_item("Two", "msg_1")]))],
    [{**created(), "sequence_number": 1}, {**delta(), "sequence_number": 1}, terminal()],
])
def test_failed_inconsistent_and_truncated_streams_never_complete_or_record(plugin, events):
    context = {}
    plugin.prepare_request(request(tools=TOOLS), MODEL, context)
    before, observed = deepcopy(context), []
    with pytest.raises(ProviderError) as error:
        for event in plugin.translate_stream(frames(*events), MODEL, context):
            observed.append(event)
    assert OPAQUE not in str(error.value) and SUMMARY not in str(error.value)
    assert all(event.completion is None for event in observed)
    assert all(event.chunk["choices"][0]["finish_reason"] is None for event in observed)
    assert all(not event.chunk["choices"][0]["delta"].get("tool_calls") for event in observed)
    assert context == before


@pytest.mark.parametrize("lines", [
    ["data: [DONE]", ""],
    ["data: not JSON", ""],
    ["data: []", ""],
    ["data: null", ""],
    ['data: {"type":"response.created","type":"response.completed"}', ""],
    ['data: {"type":"response.created","number":NaN}', ""],
    ["event: response.failed", 'data: {"type":"response.completed"}', ""],
    [b"data: bytes are not decoded", ""],
    ["data: incomplete"],
    frames(terminal())[:-1],
    frames(terminal()) + ["data: [DONE]", "", "data: [DONE]", ""],
    frames(terminal()) + ["data: [DONE]", "", *frames(delta("late"))],
    frames(terminal()) + ["event: response.failed", "data: [DONE]", ""],
])
def test_malformed_sse_and_terminators_fail_explicitly(plugin, lines):
    with pytest.raises(ProviderError):
        list(plugin.translate_stream(lines, MODEL, {}))


def test_stream_usage_and_receipts_survive_a_mixed_sync_stream_round(plugin):
    context = {}
    body = request(tools=TOOLS)
    plugin.prepare_request(body, MODEL, context)
    first = plugin.translate_response(response([reasoning(), function_item()]), MODEL, context)
    follow_up = request([*body["messages"], first["choices"][0]["message"], tool_result()])
    prepared = plugin.prepare_request(follow_up, MODEL, context)
    assert prepared.body["input"][1] == reasoning()
    events, completion = stream_result(plugin, [
        created(id="resp_2"), delta("done"),
        terminal(response([text_item("done")], id="resp_2", usage={"input_tokens": 5, "output_tokens": 2})),
    ], context)
    assert completion["usage"] == {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}
    assert events[-2].chunk["usage"] == completion["usage"]
    assert OPAQUE not in repr(events) and SUMMARY not in repr(events)
