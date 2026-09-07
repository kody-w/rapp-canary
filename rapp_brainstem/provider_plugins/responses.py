"""Text/function-call Responses transport; no network, runtime imports, or I/O.

Requests always use streaming, store=False, and encrypted reasoning inclusion.
Only the supplied request-local context is mutated. Receipts contain independent
copies of successful output and match the complete preceding Chat history plus
the generated assistant message, never just a shared prompt or response id.
"""

from collections.abc import Iterable, Iterator
import base64
import binascii
from copy import deepcopy
from dataclasses import dataclass, field
import json
import re

from .base import (
    PluginSpec, PreparedRequest, ProviderError, ProviderPlugin, ProviderStreamEvent,
)


_STATE = "responses.v1"
_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}")
_REQUEST_FIELDS = {
    "model", "messages", "tools", "tool_choice", "stream", "stream_options",
    "parallel_tool_calls", "max_tokens", "max_completion_tokens", "n", "reasoning_effort",
}
_OUTPUT_TYPES = {"message", "function_call", "reasoning"}
_VISIBLE_TYPES = {"output_text": "text", "refusal": "refusal"}


def _object(value, allowed=None, required=()):
    if (
        not isinstance(value, dict)
        or not set(required) <= value.keys()
        or (allowed is not None and not value.keys() <= allowed)
    ):
        raise ProviderError("Responses received a malformed or unsupported object")
    return value


def _is_identifier(value):
    return (
        isinstance(value, str) and bool(value)
        and not any(character.isspace() or ord(character) < 32 or ord(character) == 127
                    for character in value)
    )


def _identifier(value):
    if not _is_identifier(value):
        raise ProviderError("Responses identifiers must be nonempty strings without whitespace")
    return value


_SNAPSHOT_SUFFIX = re.compile(r"^\d{4}-\d{2}-\d{2}(-[a-z0-9]+)*$")
_ALIAS_SUFFIX = re.compile(r"^[a-z0-9]+$")


def _matches_requested_model(actual, requested):
    """The Responses API may echo back a more specific dated snapshot for a
    requested alias (gpt-5.5 -> gpt-5.5-2026-04-23, gpt-5.4-mini ->
    gpt-5.4-mini-2026-03-17), or the shorter canonical family name for a
    requested variant alias (gpt-5.6-sol-fast -> gpt-5.6-sol). Accept only
    those two specific, observed shapes; anything else — including a merely
    similar-looking but unrelated id — is a genuine mismatch and must still
    be refused.
    """
    if actual == requested:
        return True
    if actual.startswith(requested + "-"):
        return bool(_SNAPSHOT_SUFFIX.match(actual[len(requested) + 1:]))
    if requested.startswith(actual + "-"):
        return bool(_ALIAS_SUFFIX.match(requested[len(actual) + 1:]))
    return False


def _same_identifier(previous, current):
    if previous == current:
        return True
    # Copilot may re-encrypt opaque IDs for each snapshot. Never substitute a
    # different clear-text ID; final response IDs remain the continuity tokens.
    for value in (previous, current):
        if not isinstance(value, str) or len(value) < 128:
            return False
        try:
            decoded = base64.b64decode(value, validate=True)
        except (binascii.Error, ValueError):
            return False
        if len(decoded) < 64:
            return False
    return True


def _same_item_field(key, previous, current):
    if key in ("id", "call_id", "encrypted_content"):
        return _same_identifier(previous, current)
    return previous == current


def _name(value):
    if not isinstance(value, str) or not _NAME.fullmatch(value):
        raise ProviderError("Responses received an invalid function name")
    return value


def _integer(value):
    if type(value) is not int or value < 0:
        raise ProviderError("Responses counters and indices must be nonnegative integers")
    return value


def _string(value):
    if not isinstance(value, str):
        raise ProviderError("Responses text and arguments must be strings")
    return value


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ProviderError("Responses received JSON with duplicate keys")
        result[key] = value
    return result


def _invalid_constant(value):
    raise ProviderError("Responses received non-finite JSON")


def _json(value):
    try:
        return json.loads(
            value, object_pairs_hook=_unique_object, parse_constant=_invalid_constant,
        )
    except (json.JSONDecodeError, RecursionError):
        raise ProviderError("Responses received malformed JSON") from None


def _arguments(value):
    _string(value)
    if not isinstance(_json(value), dict):
        raise ProviderError("Responses function arguments must encode a JSON object")
    return value


def _chat_text(value, assistant=False):
    if value is None and assistant:
        return ""
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        raise ProviderError("Responses supports text-only Chat messages")
    text = []
    for part in value:
        _object(part)
        if part.get("type") == "text":
            _object(part, {"type", "text"}, {"text"})
            text.append(_string(part["text"]))
        elif assistant and part.get("type") == "refusal":
            _object(part, {"type", "refusal"}, {"refusal"})
            text.append(_string(part["refusal"]))
        else:
            raise ProviderError("Responses supports text-only Chat content parts")
    return "".join(text)


def _messages(messages):
    if not isinstance(messages, list) or not messages:
        raise ProviderError("Responses requires a nonempty Chat messages array")
    normalized, groups, seen, pending = [], [], set(), {}
    for message in messages:
        _object(message, required={"role"})
        role = message["role"]
        if role == "tool":
            _object(message, {"role", "tool_call_id", "name", "content"}, {"content"})
            call_id = _identifier(message.get("tool_call_id"))
            if call_id not in pending:
                raise ProviderError("Responses received an orphaned or duplicate tool result")
            name = pending.pop(call_id)
            if "name" in message and message["name"] != name:
                raise ProviderError("Responses tool result name does not match its function call")
            content = _chat_text(message["content"])
            normalized.append({"role": "tool", "tool_call_id": call_id, "content": content})
            groups.append([{"type": "function_call_output", "call_id": call_id, "output": content}])
            continue
        if pending:
            raise ProviderError("Responses requires all tool results before the next message")
        if role not in ("system", "developer", "user", "assistant"):
            raise ProviderError("Responses received an unsupported Chat message role")
        allowed = {"role", "content"}
        if role == "assistant":
            allowed |= {"tool_calls", "refusal"}
        _object(message, allowed)
        text = _chat_text(message.get("content"), assistant=role == "assistant")
        if message.get("refusal") is not None:
            refusal = _string(message["refusal"])
            if refusal != text:
                text += refusal
        calls = message.get("tool_calls", [])
        if not isinstance(calls, list):
            raise ProviderError("Responses requires a Chat tool_calls array")
        canonical = {"role": role, "content": text or (None if calls else "")}
        items = [{"role": role, "content": text}] if text or not calls else []
        converted_calls = []
        for call in calls:
            _object(call, {"id", "type", "function"}, {"id", "type", "function"})
            if call["type"] != "function":
                raise ProviderError("Responses supports only function tools")
            call_id = _identifier(call["id"])
            if call_id in seen:
                raise ProviderError("Responses function call ids must be unique")
            function = _object(call["function"], {"name", "arguments"}, {"name", "arguments"})
            name, arguments = _name(function["name"]), _arguments(function["arguments"])
            seen.add(call_id)
            pending[call_id] = name
            converted_calls.append({
                "id": call_id, "type": "function",
                "function": {"name": name, "arguments": arguments},
            })
            items.append({
                "type": "function_call", "call_id": call_id, "name": name, "arguments": arguments,
            })
        if converted_calls:
            canonical["tool_calls"] = converted_calls
        if role == "assistant" and not text and not converted_calls:
            raise ProviderError("Responses received an empty assistant message")
        normalized.append(canonical)
        groups.append(items)
    if pending:
        raise ProviderError("Responses requires a result for every function call")
    return normalized, groups, seen


def _tools(value):
    if not isinstance(value, list):
        raise ProviderError("Responses requires a Chat tools array")
    tools, names = [], set()
    for tool in value:
        _object(tool, {"type", "function"}, {"type", "function"})
        if tool["type"] != "function":
            raise ProviderError("Responses supports only function tools")
        function = _object(
            tool["function"], {"name", "description", "parameters", "strict"}, {"name"},
        )
        name = _name(function["name"])
        if name in names:
            raise ProviderError("Responses tool names must be unique")
        names.add(name)
        parameters = function.get("parameters", {"type": "object", "properties": {}})
        _object(parameters)
        if parameters.get("type", "object") != "object":
            raise ProviderError("Responses function parameters must describe an object")
        strict = function.get("strict", False)
        if type(strict) is not bool:
            raise ProviderError("Responses function strict must be a boolean")
        converted = {
            "type": "function", "name": name,
            "parameters": deepcopy(parameters), "strict": strict,
        }
        if "description" in function:
            converted["description"] = _string(function["description"])
        tools.append(converted)
    return tools, names


def _tool_choice(choice, names):
    if isinstance(choice, str):
        if choice not in ("auto", "none", "required") or (choice == "required" and not names):
            raise ProviderError("Responses received an unsupported tool choice")
        return choice
    _object(choice, {"type", "function"}, {"type", "function"})
    function = _object(choice["function"], {"name"}, {"name"})
    name = _name(function["name"])
    if choice["type"] != "function" or name not in names:
        raise ProviderError("Responses forced function must be a declared tool")
    return {"type": "function", "name": name}


def _state(context, model):
    _object(context)
    state = context.get(_STATE)
    if state is None:
        return {"model": model["id"], "receipts": [], "pending": None}
    _object(state, {"model", "receipts", "pending"}, {"model", "receipts", "pending"})
    if state["model"] != model["id"] or not isinstance(state["receipts"], list):
        raise ProviderError("Responses request context is incompatible with this model")
    return state


def _remember(context, model, payload, completion):
    state = _state(context, model)
    pending = state["pending"]
    if pending is None:
        return
    receipt = {
        "history": deepcopy(pending["messages"]),
        "assistant": deepcopy(completion["choices"][0]["message"]),
        "output": deepcopy(payload["output"]),
    }
    context[_STATE] = {
        "model": model["id"], "receipts": [*state["receipts"], receipt], "pending": None,
    }


def _usage(value):
    _object(value, required={"input_tokens", "output_tokens"})
    prompt = _integer(value["input_tokens"])
    output = _integer(value["output_tokens"])
    total = _integer(value.get("total_tokens", prompt + output))
    if total != prompt + output:
        raise ProviderError("Responses usage totals are inconsistent")
    result = {"prompt_tokens": prompt, "completion_tokens": output, "total_tokens": total}
    for source, target, limit in (
        ("input_tokens_details", "prompt_tokens_details", prompt),
        ("output_tokens_details", "completion_tokens_details", output),
    ):
        if value.get(source) is not None:
            details = _object(value[source])
            if any(_integer(count) > limit for count in details.values()):
                raise ProviderError("Responses usage details exceed their token total")
            result[target] = deepcopy(details)
    return result


def _completed(payload, model, context):
    _object(payload)
    actual_model = _identifier(payload.get("model"))
    if not _matches_requested_model(actual_model, model["id"]):
        raise ProviderError("Responses returned a different model from the selected model")
    if payload.get("error") is not None:
        raise ProviderError("Responses provider reported a failed response")
    if payload.get("status") != "completed" or payload.get("incomplete_details") is not None:
        raise ProviderError("Responses provider did not complete the response")
    if payload.get("object", "response") != "response":
        raise ProviderError("Responses provider returned an invalid response object")
    response_id = _identifier(payload.get("id"))
    output = payload.get("output")
    if not isinstance(output, list) or not output:
        raise ProviderError("Responses provider returned no output")
    text, calls, item_ids, call_ids, parts = [], [], set(), set(), {}
    refused = False
    for index, item in enumerate(output):
        _object(item)
        if item.get("status", "completed") != "completed":
            raise ProviderError("Responses provider returned an unfinished output item")
        if "id" in item:
            item_id = _identifier(item["id"])
            if item_id in item_ids:
                raise ProviderError("Responses output item ids must be unique")
            item_ids.add(item_id)
        kind = item.get("type")
        if kind == "message":
            if item.get("role") != "assistant" or not isinstance(item.get("content"), list):
                raise ProviderError("Responses provider returned a malformed assistant message")
            if not item["content"]:
                raise ProviderError("Responses provider returned an empty message item")
            for content_index, part in enumerate(item["content"]):
                _object(part)
                part_type = part.get("type")
                if not isinstance(part_type, str) or part_type not in _VISIBLE_TYPES:
                    raise ProviderError("Responses provider returned unsupported message content")
                value = _string(part.get(_VISIBLE_TYPES[part_type]))
                text.append(value)
                refused = refused or (part_type == "refusal" and bool(value))
                parts[(index, content_index)] = (part_type, value)
        elif kind == "function_call":
            call_id = _identifier(item.get("call_id"))
            if call_id in call_ids:
                raise ProviderError("Responses function call ids must be unique")
            call_ids.add(call_id)
            calls.append({
                "id": call_id, "type": "function",
                "function": {
                    "name": _name(item.get("name")), "arguments": _arguments(item.get("arguments")),
                },
            })
        elif kind == "reasoning":
            if "encrypted_content" in item and item["encrypted_content"] is not None:
                _string(item["encrypted_content"])
            if "summary" in item and not isinstance(item["summary"], list):
                raise ProviderError("Responses provider returned a malformed reasoning item")
        else:
            raise ProviderError("Responses provider returned an unsupported output item")
    content = "".join(text)
    if not content and not calls:
        raise ProviderError("Responses provider returned no visible text or function calls")
    pending = _state(context, model)["pending"]
    if pending is not None:
        if call_ids & pending["call_ids"]:
            raise ProviderError("Responses provider reused a previous function call id")
        choice = pending["tool_choice"]
        if calls and (
            choice == "none"
            or any(call["function"]["name"] not in pending["tool_names"] for call in calls)
            or (pending["parallel"] is False and len(calls) > 1)
        ):
            raise ProviderError("Responses provider returned calls disallowed by the request")
        if (choice == "required" or isinstance(choice, dict)) and not calls and not refused:
            raise ProviderError("Responses provider did not honor the required tool choice")
        if isinstance(choice, dict) and any(
            call["function"]["name"] != choice["name"] for call in calls
        ):
            raise ProviderError("Responses provider did not honor the forced function")
    message = {"role": "assistant", "content": content or None}
    if calls:
        message["tool_calls"] = calls
    completion = {
        "id": response_id, "object": "chat.completion",
        "created": _integer(payload.get("created_at", 0)),
        "model": actual_model,
        "choices": [{
            "index": 0, "message": message, "finish_reason": "tool_calls" if calls else "stop",
        }],
    }
    if payload.get("usage") is not None:
        completion["usage"] = _usage(payload["usage"])
    return completion, parts


def _sse(lines):
    """Consume physical SSE lines, as returned by requests.iter_lines()."""
    data, event_name, first = [], None, True
    for line in lines:
        _string(line)
        if first:
            line = line.removeprefix("\ufeff")
            first = False
        line = line.removesuffix("\n").removesuffix("\r")
        if "\r" in line or "\n" in line:
            raise ProviderError("Responses streaming requires individual SSE lines")
        if not line:
            if data:
                yield event_name, "\n".join(data)
            data, event_name = [], None
        elif not line.startswith(":"):
            field_name, _, value = line.partition(":")
            value = value.removeprefix(" ")
            if field_name == "data":
                data.append(value)
            elif field_name == "event":
                event_name = value
            # SSE id/retry/unknown fields are framing metadata, not output.
    if data or event_name is not None:
        raise ProviderError("Responses stream ended inside an SSE event")


@dataclass
class _Fragments:
    kind: str
    value: str = ""
    seen: bool = False
    final: str | None = None
    sources: set[str] = field(default_factory=set)

    def add(self, value):
        if self.final is not None:
            raise ProviderError("Responses emitted a delta after its content was complete")
        self.value += _string(value)
        self.seen = True

    def finish(self, value, source):
        _string(value)
        if source in self.sources:
            raise ProviderError("Responses repeated a content completion event")
        self.check(value)
        self.final = value
        self.sources.add(source)

    def check(self, value):
        if (self.seen and self.value != value) or (self.final is not None and self.final != value):
            raise ProviderError("Responses final content disagrees with its streamed content")


class _Stream:
    def __init__(self, model):
        self.requested_model = model["id"]
        self.metadata = {"model": model["id"], "created": 0}
        self.explicit_metadata = {}
        self.parts, self.arguments = {}, {}
        self.item_ids, self.added, self.done = {}, {}, {}
        self.call_ids, self.argument_names = {}, {}
        self.deltas = []
        self.last_position = (-1, -1)
        self.sequence = -1
        self.payload = None

    def metadata_from(self, response):
        _object(response)
        for source, target in (("id", "id"), ("model", "model"), ("created_at", "created")):
            if source in response:
                value = _integer(response[source]) if source == "created_at" else _identifier(response[source])
                if source == "model" and not _matches_requested_model(value, self.requested_model):
                    raise ProviderError("Responses returned a different model from the selected model")
                if source in self.explicit_metadata and not (
                    _same_identifier(self.explicit_metadata[source], value) if source == "id"
                    else self.explicit_metadata[source] == value
                ):
                    raise ProviderError("Responses changed response metadata during streaming")
                self.explicit_metadata[source] = value
                self.metadata[target] = value

    def position(self, event):
        index = _integer(event.get("output_index"))
        if "item_id" in event:
            item_id = _identifier(event["item_id"])
            if index in self.item_ids and not _same_identifier(self.item_ids[index], item_id):
                raise ProviderError("Responses changed an output item id during streaming")
            if any(other != index and value == item_id for other, value in self.item_ids.items()):
                raise ProviderError("Responses stream contains duplicate output item ids")
            self.item_ids[index] = item_id
        return index

    def part(self, event, kind):
        index = self.position(event)
        key = (index, _integer(event.get("content_index")))
        slot = self.parts.setdefault(key, _Fragments(kind))
        if slot.kind != kind:
            raise ProviderError("Responses changed the kind of streamed content")
        return key, slot

    def item(self, event, finished):
        index = self.position(event)
        item = _object(event.get("item"))
        kind = item.get("type")
        if not isinstance(kind, str) or kind not in _OUTPUT_TYPES:
            raise ProviderError("Responses streamed an unsupported output item")
        statuses = ("completed",) if finished else ("in_progress", "completed")
        if item.get("status", "completed") not in statuses:
            raise ProviderError("Responses streamed an invalid output item state")
        if "id" in item:
            self.position({"output_index": index, "item_id": item["id"]})
        if index in self.done or (not finished and index in self.added):
            raise ProviderError("Responses repeated or reopened an output item")
        if kind == "function_call":
            call_id = _identifier(item.get("call_id"))
            _name(item.get("name"))
            if any(other != index and value == call_id for other, value in self.call_ids.items()):
                raise ProviderError("Responses stream contains duplicate function call ids")
            if index in self.call_ids and not _same_identifier(self.call_ids[index], call_id):
                raise ProviderError("Responses changed a function call id during streaming")
            self.call_ids[index] = call_id
        elif kind == "message":
            if item.get("role") != "assistant" or not isinstance(item.get("content"), list):
                raise ProviderError("Responses streamed a malformed message item")
            for part in item["content"]:
                _object(part)
                part_type = part.get("type")
                if not isinstance(part_type, str) or part_type not in _VISIBLE_TYPES:
                    raise ProviderError("Responses streamed unsupported message content")
                _string(part.get(_VISIBLE_TYPES[part_type]))
        (self.done if finished else self.added)[index] = item

    def accept(self, event):
        _object(event)
        kind = event.get("type")
        if not isinstance(kind, str):
            raise ProviderError("Responses SSE event is missing its type")
        if self.payload is not None:
            raise ProviderError("Responses emitted data after response.completed")
        if "response_id" in event:
            self.metadata_from({"id": event["response_id"]})
        if "sequence_number" in event:
            sequence = _integer(event["sequence_number"])
            if sequence <= self.sequence:
                raise ProviderError("Responses stream sequence numbers are not increasing")
            self.sequence = sequence
        if kind in ("error", "response.error", "response.failed", "response.incomplete", "response.cancelled"):
            raise ProviderError("Responses stream failed or did not complete")
        if kind == "response.completed":
            self.payload = _object(event.get("response"))
            self.metadata_from(self.payload)
        elif kind in ("response.created", "response.in_progress", "response.queued"):
            response = _object(event.get("response"))
            if response.get("error") is not None or response.get("status") not in ("queued", "in_progress"):
                raise ProviderError("Responses streamed an invalid response state")
            self.metadata_from(response)
        elif kind in ("response.output_item.added", "response.output_item.done"):
            self.item(event, kind.endswith(".done"))
        elif kind in (
            "response.output_text.delta", "response.output_text.done",
            "response.refusal.delta", "response.refusal.done",
        ):
            part_type = "refusal" if ".refusal." in kind else "output_text"
            key, slot = self.part(event, part_type)
            if key[0] in self.done:
                raise ProviderError("Responses emitted content after an output item was complete")
            if kind.endswith(".delta"):
                delta = _string(event.get("delta"))
                slot.add(delta)
                if delta and key < self.last_position:
                    raise ProviderError("Responses streamed visible content out of order")
                if delta:
                    self.last_position = key
                self.deltas.append(delta)
                return delta
            slot.finish(event.get(_VISIBLE_TYPES[part_type]), kind)
        elif kind in ("response.content_part.added", "response.content_part.done"):
            part = _object(event.get("part"))
            part_type = part.get("type")
            if not isinstance(part_type, str) or part_type not in _VISIBLE_TYPES:
                raise ProviderError("Responses streamed unsupported message content")
            key, slot = self.part(event, part_type)
            if key[0] in self.done:
                raise ProviderError("Responses emitted content after an output item was complete")
            value = _string(part.get(_VISIBLE_TYPES[part_type]))
            if kind.endswith(".done"):
                slot.finish(value, kind)
            else:
                if slot.seen or slot.sources:
                    raise ProviderError("Responses reopened a content part")
                slot.sources.add(kind)
                if value:
                    slot.finish(value, kind + ".initial")
        elif kind in (
            "response.function_call_arguments.delta", "response.function_call_arguments.done",
        ):
            index = self.position(event)
            if index in self.done:
                raise ProviderError("Responses emitted arguments after a function call was complete")
            slot = self.arguments.setdefault(index, _Fragments("function_call"))
            if kind.endswith(".delta"):
                slot.add(event.get("delta"))
            else:
                slot.finish(_arguments(event.get("arguments")), kind)
            if "name" in event:
                name = _name(event["name"])
                if (
                    (index in self.added and self.added[index].get("name") != name)
                    or (index in self.argument_names and self.argument_names[index] != name)
                ):
                    raise ProviderError("Responses changed a streamed function name")
                self.argument_names[index] = name
        elif kind == "response.output_text.annotation.added" or kind.startswith((
            "response.reasoning_", "response.reasoning.",
        )):
            pass  # Reasoning is opaque continuity state, never user-visible output.
        else:
            raise ProviderError("Responses streamed an unsupported event type")
        return None

    def verify(self, completion, final_parts):
        output = self.payload["output"]
        for index, item_id in self.item_ids.items():
            if index >= len(output) or not _same_identifier(item_id, output[index].get("id")):
                raise ProviderError("Responses final output disagrees with streamed item ids")
        for key, slot in self.parts.items():
            if key not in final_parts or final_parts[key][0] != slot.kind:
                raise ProviderError("Responses final output is missing streamed content")
            slot.check(final_parts[key][1])
        for index, slot in self.arguments.items():
            if index >= len(output) or output[index].get("type") != "function_call":
                raise ProviderError("Responses final output is missing a streamed function call")
            slot.check(output[index]["arguments"])
            if index in self.argument_names and output[index]["name"] != self.argument_names[index]:
                raise ProviderError("Responses final function name disagrees with the stream")
        for index, snapshot in self.done.items():
            if index >= len(output) or any(
                not _same_item_field(key, value, output[index].get(key)) for key, value in snapshot.items()
            ):
                raise ProviderError("Responses changed a completed output item")
        for index, snapshot in self.added.items():
            if index >= len(output) or any(
                not _same_item_field(key, snapshot[key], output[index].get(key))
                for key in ("type", "id", "role", "call_id", "name") if key in snapshot
            ):
                raise ProviderError("Responses changed an output item during streaming")
            if snapshot.get("status") == "completed" and any(
                not _same_item_field(key, value, output[index].get(key)) for key, value in snapshot.items()
            ):
                raise ProviderError("Responses changed an already completed output item")
            arguments = snapshot.get("arguments")
            if arguments is not None and not _string(output[index].get("arguments")).startswith(_string(arguments)):
                raise ProviderError("Responses changed the initial function arguments")
            if snapshot["type"] == "message":
                for content_index, part in enumerate(snapshot["content"]):
                    final = final_parts.get((index, content_index))
                    if (
                        final is None or final[0] != part["type"]
                        or not final[1].startswith(part[_VISIBLE_TYPES[part["type"]]])
                    ):
                        raise ProviderError("Responses changed initial message content")
        content = completion["choices"][0]["message"]["content"] or ""
        if self.deltas and "".join(self.deltas) != content:
            raise ProviderError("Responses final text disagrees with its streamed text")

    def chunk(self, delta, reason=None, usage=None):
        chunk = {
            **self.metadata, "object": "chat.completion.chunk",
            "choices": [{"index": 0, "delta": delta, "finish_reason": reason}],
        }
        if usage is not None:
            chunk["usage"] = deepcopy(usage)
        return ProviderStreamEvent(chunk=chunk)


class ResponsesPlugin(ProviderPlugin):
    spec = PluginSpec(
        id="responses", api_version=1,
        capabilities=frozenset({"catalog", "completion", "streaming", "tool-calls"}),
        version="1.0.0",
    )

    def supports_model(self, model: dict) -> bool:
        if not isinstance(model, dict) or not _is_identifier(model.get("id")):
            return False
        endpoints = model.get("supported_endpoints")
        capabilities = model.get("capabilities")
        if (
            not isinstance(endpoints, list)
            or not all(isinstance(endpoint, str) for endpoint in endpoints)
            or "/responses" not in endpoints or "/chat/completions" in endpoints
            or not isinstance(capabilities, dict) or capabilities.get("type") != "chat"
        ):
            return False
        supports = capabilities.get("supports")
        if not isinstance(supports, dict) or any(
            supports.get(feature) is not True for feature in ("streaming", "tool_calls")
        ):
            return False
        policy = model.get("policy")
        return (
            ("model_picker_enabled" not in model or model["model_picker_enabled"] is True)
            and (policy is None or (isinstance(policy, dict) and policy.get("state") == "enabled"))
        )

    def prepare_request(self, body: dict, model: dict, context: dict) -> PreparedRequest:
        if not self.supports_model(model):
            raise ProviderError("Responses cannot serve this model's advertised capabilities")
        _object(body, _REQUEST_FIELDS, {"model", "messages"})
        if body["model"] != model["id"]:
            raise ProviderError("Responses request model does not match the selected model")
        if "stream" in body and type(body["stream"]) is not bool:
            raise ProviderError("Responses Chat stream must be a boolean")
        if "n" in body and (type(body["n"]) is not int or body["n"] != 1):
            raise ProviderError("Responses supports exactly one completion")
        if "stream_options" in body:
            options = _object(body["stream_options"], {"include_usage"})
            if "include_usage" in options and type(options["include_usage"]) is not bool:
                raise ProviderError("Responses include_usage must be a boolean")
        messages, groups, call_ids = _messages(body["messages"])
        tools, names = _tools(body.get("tools", []))
        choice = _tool_choice(body.get("tool_choice", "auto"), names)
        state = _state(context, model)
        for receipt in state["receipts"]:
            index = len(receipt["history"])
            if (
                index < len(messages) and messages[:index] == receipt["history"]
                and messages[index] == receipt["assistant"]
            ):
                groups[index] = deepcopy(receipt["output"])
        request = {
            "model": model["id"], "input": [item for group in groups for item in group],
            "stream": True, "store": False, "include": ["reasoning.encrypted_content"],
        }
        if tools:
            request["tools"] = tools
        if "tool_choice" in body:
            request["tool_choice"] = choice
        supports = model["capabilities"]["supports"]
        if "parallel_tool_calls" in body:
            parallel = body["parallel_tool_calls"]
            if type(parallel) is not bool or (parallel and supports.get("parallel_tool_calls") is not True):
                raise ProviderError("Responses model does not support the requested parallel calls")
            request["parallel_tool_calls"] = parallel
        limits = [key for key in ("max_tokens", "max_completion_tokens") if key in body]
        if len(limits) > 1:
            raise ProviderError("Responses received conflicting output token limits")
        if limits:
            limit = _integer(body[limits[0]])
            if not limit:
                raise ProviderError("Responses output token limit must be positive")
            advertised = _object(model["capabilities"].get("limits", {}))
            if "max_output_tokens" in advertised and limit > _integer(advertised["max_output_tokens"]):
                raise ProviderError("Responses output token limit exceeds the model's capabilities")
            request["max_output_tokens"] = limit
        if "reasoning_effort" in body:
            efforts = supports.get("reasoning_effort")
            effort = body["reasoning_effort"]
            if not isinstance(effort, str) or not isinstance(efforts, list) or effort not in efforts:
                raise ProviderError("Responses model does not support the requested reasoning effort")
            request["reasoning"] = {"effort": effort}
        context[_STATE] = {
            **state,
            "pending": {
                "messages": deepcopy(messages), "call_ids": call_ids, "tool_names": names,
                "tool_choice": deepcopy(choice), "parallel": body.get("parallel_tool_calls"),
            },
        }
        return PreparedRequest(endpoint="/responses", body=request, stream=True)

    def translate_response(self, payload: dict, model: dict, context: dict) -> dict:
        if not self.supports_model(model):
            raise ProviderError("Responses cannot serve this model's advertised capabilities")
        completion, _ = _completed(payload, model, context)
        _remember(context, model, payload, completion)
        return completion

    def translate_stream(
        self, lines: Iterable[str], model: dict, context: dict,
    ) -> Iterator[ProviderStreamEvent]:
        if not self.supports_model(model):
            raise ProviderError("Responses cannot serve this model's advertised capabilities")
        stream, deferred, done = _Stream(model), [], False
        for event_name, data in _sse(lines):
            if done:
                raise ProviderError("Responses emitted data after its stream terminator")
            if data == "[DONE]":
                if stream.payload is None or event_name not in (None, "", "message"):
                    raise ProviderError("Responses stream ended without response.completed")
                done = True
                continue
            event = _object(_json(data))
            if event_name not in (None, "", "message", event.get("type")):
                raise ProviderError("Responses SSE event name disagrees with its payload")
            delta = stream.accept(event)
            if delta:
                deferred.append(delta)
            if "id" in stream.metadata:
                for text in deferred:
                    yield stream.chunk({"role": "assistant", "content": text})
                deferred.clear()
        if stream.payload is None:
            raise ProviderError("Responses stream ended without response.completed")
        completion, parts = _completed(stream.payload, model, context)
        stream.verify(completion, parts)
        message = completion["choices"][0]["message"]
        for text in deferred:
            yield stream.chunk({"role": "assistant", "content": text})
        if not stream.deltas and message["content"]:
            yield stream.chunk({"role": "assistant", "content": message["content"]})
        if message.get("tool_calls"):
            calls = [{"index": index, **deepcopy(call)} for index, call in enumerate(message["tool_calls"])]
            yield stream.chunk({"role": "assistant", "tool_calls": calls})
        yield stream.chunk({}, completion["choices"][0]["finish_reason"], completion.get("usage"))
        _remember(context, model, stream.payload, completion)
        yield ProviderStreamEvent(completion=completion)
