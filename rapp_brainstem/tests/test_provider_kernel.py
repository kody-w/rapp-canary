"""Exercise the external provider binding through unchanged kernel routes."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib
import inspect
import io
import json
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import threading
from unittest import mock

import pytest
import requests
from werkzeug.test import Client
from werkzeug.wrappers import Response

from kernel_compat import KERNEL_SHA256, bind_providers, load_kernel
from provider_host import ProviderHTTPClient, ProviderRegistry
from provider_plugins.base import ProviderError


ROOT = Path(__file__).resolve().parents[1]
ORIGIN = "https://api.enterprise.githubcopilot.com"
ASTRA = {
    "id": "gpt-6-astra", "name": "GPT-6 Astra", "policy": {"state": "enabled"},
    "model_picker_enabled": True, "supported_endpoints": ["/responses", "ws:/responses"],
    "capabilities": {
        "type": "chat",
        "supports": {
            "tool_calls": True, "parallel_tool_calls": True, "streaming": True,
            "reasoning_effort": ["low", "medium", "high", "xhigh", "max"],
        },
        "limits": {"max_context_window_tokens": 1000000, "max_output_tokens": 128000},
    },
}
LEGACY = {
    "id": "gpt-4o", "name": "GPT-4o", "supported_endpoints": ["/chat/completions"],
    "capabilities": {"type": "chat"},
}


class LocalClient(Client):
    def open(self, *args, **kwargs):
        overrides = dict(kwargs.get("environ_overrides") or {})
        overrides.setdefault("REMOTE_ADDR", "127.0.0.1")
        kwargs["environ_overrides"] = overrides
        return super().open(*args, **kwargs)


def upstream_response(payload, streaming=False):
    response = requests.Response()
    response.status_code = 200
    response.url = ORIGIN + ("/responses" if streaming else "/models")
    response.request = requests.Request("GET", response.url).prepare()
    if streaming:
        frames = "".join(f"data: {json.dumps(event)}\n\n" for event in payload)
        response.raw = io.BytesIO(frames.encode("utf-8"))
    else:
        response._content = json.dumps(payload).encode("utf-8")
        response._content_consumed = True
    response.encoding = "utf-8"
    response.close = mock.Mock(wraps=response.close)
    return response


def completed(output, label="fixture"):
    return {
        "id": "resp_" + label, "object": "response", "model": "gpt-6-astra",
        "status": "completed", "created_at": 1, "output": output,
        "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
    }


def text_item(text):
    return {
        "id": "msg_fixture", "type": "message", "role": "assistant", "status": "completed",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
    }


def function_item(call_id, value):
    return {
        "id": "fc_" + call_id, "type": "function_call", "status": "completed",
        "call_id": call_id, "name": "FixtureEcho", "arguments": json.dumps({"value": value}),
    }


def reasoning_item(label):
    return {
        "id": "rs_" + label, "type": "reasoning", "summary": [],
        "encrypted_content": "opaque-fixture-" + label,
    }


class Echo:
    name = "FixtureEcho"

    def __init__(self):
        self.calls = []

    def to_tool(self):
        return {"type": "function", "function": {
            "name": self.name, "description": "Echo fictional fixture data only.",
            "parameters": {
                "type": "object", "properties": {"value": {"type": "string"}}, "required": [],
            },
        }}

    def system_context(self):
        return ""

    def perform(self, value="", **kwargs):
        self.calls.append(value)
        return "echo:" + value


class CopilotFixture:
    exceptions = requests.exceptions

    def __init__(self, mode="text", barrier=None):
        self.mode = mode
        self.barrier = barrier
        self.requests = []
        self.responses = []
        self.catalog_calls = []

    def get(self, url, **kwargs):
        self.catalog_calls.append((url, kwargs))
        assert url == ORIGIN + "/models"
        return upstream_response({"data": [deepcopy(ASTRA), deepcopy(LEGACY)]})

    def post(self, url, **kwargs):
        body = kwargs["json"]
        self.requests.append((url, deepcopy(body)))
        if url.endswith("/chat/completions"):
            assert body["model"] == "gpt-4o"
            return upstream_response({"choices": [{
                "message": {"role": "assistant", "content": "legacy reply"}, "finish_reason": "stop",
            }]})
        assert url == ORIGIN + "/responses"
        assert body["model"] == "gpt-6-astra"
        assert body["stream"] is True
        assert body["store"] is False
        assert kwargs["allow_redirects"] is False
        users = [item for item in body["input"] if item.get("role") == "user"]
        content = users[-1]["content"]
        label = content if isinstance(content, str) else content[0]["text"]
        tool_results = [item for item in body["input"] if item.get("type") == "function_call_output"]
        if self.mode == "error":
            events = [
                {"type": "response.output_text.delta", "delta": "partial", "output_index": 0, "content_index": 0},
                {"type": "response.failed", "response": {
                    "id": "resp_failed", "status": "failed",
                    "error": {"code": "fixture_error", "message": "Synthetic failure"},
                }},
            ]
        else:
            output = [text_item("Astra fixture answer")]
            if self.mode in ("tools", "concurrent") and not tool_results:
                if self.barrier is not None:
                    self.barrier.wait(timeout=5)
                calls = [function_item("call_first", label)]
                if self.mode == "tools":
                    calls.append(function_item("call_second", "second"))
                output = [reasoning_item(label), *calls]
            elif self.mode in ("tools", "concurrent"):
                reasoning = [item for item in body["input"] if item.get("type") == "reasoning"]
                assert [item["encrypted_content"] for item in reasoning] == ["opaque-fixture-" + label]
                assert all(item["output"].startswith("echo:") for item in tool_results)
                output = [text_item("completed " + label)]
            elif self.mode == "budget" and body.get("tools"):
                output = [reasoning_item(str(len(tool_results))), function_item(
                    "call_" + str(len(tool_results) + 1), str(len(tool_results) + 1)
                )]
            elif self.mode == "budget":
                assert len(tool_results) == 3
                output = [text_item("budget complete")]
            result = completed(output, label.replace(" ", "_"))
            events = [{"type": "response.completed", "response": result}]
        response = upstream_response(events, streaming=True)
        self.responses.append(response)
        return response


@pytest.fixture
def snapshot(tmp_path, monkeypatch):
    directory = tmp_path / "isolated-kernel"
    directory.mkdir()
    for name in ("brainstem.py", "VERSION", "index.html"):
        shutil.copyfile(ROOT / name, directory / name)
    (directory / ".env").write_text("", encoding="utf-8")
    (directory / "soul.md").write_text("Use only fictional fixture data.", encoding="utf-8")
    (directory / "agents").mkdir()
    for key in ("GITHUB_TOKEN", "GH_TOKEN", "COPILOT_GITHUB_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    for key, value in {
        "HOME": str(tmp_path), "USERPROFILE": str(tmp_path),
        "XDG_CONFIG_HOME": str(tmp_path / ".config"), "GH_CONFIG_DIR": str(tmp_path / "gh"),
        "GITHUB_MODEL": "gpt-4o", "BRAINSTEM_LAN_MODE": "false",
        "SOUL_PATH": str(directory / "soul.md"), "AGENTS_PATH": str(directory / "agents"),
    }.items():
        monkeypatch.setenv(key, value)
    def refuse(*args, **kwargs):
        raise AssertionError("Kernel provider fixtures attempted external I/O")
    monkeypatch.setattr(requests.sessions.Session, "request", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    monkeypatch.setattr(subprocess, "Popen", refuse)
    with mock.patch.dict(sys.modules), mock.patch("dotenv.load_dotenv", return_value=False):
        sys.modules.pop("brainstem", None)
        loaded = load_kernel(directory / "brainstem.py")
        monkeypatch.setattr(loaded.module, "get_copilot_token", lambda: ("synthetic-token", ORIGIN))
        yield loaded


def bound(snapshot, monkeypatch, mode="text", barrier=None):
    from provider_plugins.responses import ResponsesPlugin
    network = CopilotFixture(mode, barrier)
    echo = Echo()
    monkeypatch.setattr(snapshot.module, "requests", network)
    monkeypatch.setattr(snapshot.module, "load_agents", lambda: {echo.name: echo})
    application = bind_providers(snapshot, ProviderRegistry([ResponsesPlugin()]))
    client = LocalClient(application, Response)
    models = client.get("/models").get_json()
    assert "gpt-6-astra" in [model["id"] for model in models["models"]]
    assert client.post("/models/set", json={"model": "gpt-6-astra"}).status_code == 200
    return client, application, network, echo


def parsed_events(response):
    return [
        json.loads(line[5:])
        for line in response.get_data(as_text=True).splitlines() if line.startswith("data:")
    ]


def test_binding_keeps_kernel_functions_routes_and_global_requests(snapshot, monkeypatch):
    from provider_plugins.responses import ResponsesPlugin
    kernel = snapshot.module
    functions = {name: value for name, value in vars(kernel).items() if inspect.isfunction(value)}
    routes = dict(kernel.app.view_functions)
    original_requests_get = requests.get
    original_requests_post = requests.post
    application = bind_providers(snapshot, ProviderRegistry([ResponsesPlugin()]))
    assert isinstance(kernel.requests, ProviderHTTPClient)
    assert application.application is kernel.app
    assert functions == {name: value for name, value in vars(kernel).items() if inspect.isfunction(value)}
    assert routes == kernel.app.view_functions
    assert requests.get is original_requests_get and requests.post is original_requests_post
    assert hashlib.sha256(snapshot.source.read_bytes()).hexdigest() == KERNEL_SHA256
    with pytest.raises(ProviderError, match="cannot be replaced"):
        bind_providers(snapshot, ProviderRegistry([ResponsesPlugin()]))


@pytest.mark.parametrize("streaming", [False, True])
def test_real_kernel_model_selection_and_astra_reply(snapshot, monkeypatch, streaming):
    client, _, network, echo = bound(snapshot, monkeypatch)
    endpoint = "/chat/stream" if streaming else "/chat"
    response = client.post(endpoint, json={"user_input": "fictional request"})
    assert response.status_code == 200
    result = parsed_events(response)[-1] if streaming else response.get_json()
    assert result["response"] == "Astra fixture answer"
    assert result["model"] == result["requested_model"] == "gpt-6-astra"
    if streaming:
        assert result["type"] == "done" and result["streamed"] is True
    assert echo.calls == []
    assert response.headers["X-Brainstem-Kernel-SHA256"] == KERNEL_SHA256
    assert snapshot.module._load_sticky_model() == "gpt-6-astra"
    assert len(network.requests) == 1


@pytest.mark.parametrize("streaming", [False, True])
def test_real_kernel_parallel_tools_and_reasoning_round_trip(snapshot, monkeypatch, streaming):
    client, _, network, echo = bound(snapshot, monkeypatch, "tools")
    response = client.post("/chat/stream" if streaming else "/chat", json={"user_input": "fixture"})
    result = parsed_events(response)[-1] if streaming else response.get_json()
    assert result["response"] == "completed fixture"
    assert echo.calls == ["fixture", "second"]
    assert len(network.requests) == 2
    assert "opaque-fixture-" not in response.get_data(as_text=True)
    assert "FixtureEcho" in result["agent_logs"]
    for upstream in network.responses:
        upstream.close.assert_called_once()


@pytest.mark.parametrize("streaming", [False, True])
def test_real_kernel_final_toolless_synthesis(snapshot, monkeypatch, streaming):
    client, _, network, echo = bound(snapshot, monkeypatch, "budget")
    response = client.post("/chat/stream" if streaming else "/chat", json={"user_input": "budget"})
    result = parsed_events(response)[-1] if streaming else response.get_json()
    assert result["response"] == "budget complete"
    assert echo.calls == ["1", "2", "3"]
    assert len(network.requests) == 4
    assert not network.requests[-1][1].get("tools")


def test_real_kernel_stream_errors_do_not_execute_tools_or_report_success(snapshot, monkeypatch):
    client, _, network, echo = bound(snapshot, monkeypatch, "error")
    response = client.post("/chat/stream", json={"user_input": "fictional failure"})
    events = parsed_events(response)
    assert events[-1]["type"] == "error"
    assert not any(event["type"] in ("done", "agent") for event in events)
    assert echo.calls == []
    assert len(network.requests) == 1


def test_kernel_auth_boundary_still_precedes_provider_access(snapshot, monkeypatch):
    client, _, network, _ = bound(snapshot, monkeypatch)
    before = len(network.catalog_calls), len(network.requests)
    response = client.get("/models", environ_overrides={"REMOTE_ADDR": "198.51.100.3"})
    assert response.status_code == 403
    assert (len(network.catalog_calls), len(network.requests)) == before


def test_real_kernel_legacy_model_stays_on_original_transport(snapshot, monkeypatch):
    client, _, network, _ = bound(snapshot, monkeypatch)
    assert client.post("/models/set", json={"model": "gpt-4o"}).status_code == 200
    result = client.post("/chat", json={"user_input": "legacy"}).get_json()
    assert result["response"] == "legacy reply"
    assert network.requests[-1][0].endswith("/chat/completions")


@pytest.mark.parametrize("streaming", [False, True])
def test_concurrent_kernel_requests_do_not_mix_reasoning_or_tool_state(snapshot, monkeypatch, streaming):
    _, application, network, echo = bound(snapshot, monkeypatch, "concurrent", threading.Barrier(2))
    def request(label):
        client = LocalClient(application, Response)
        response = client.post(
            "/chat/stream" if streaming else "/chat", json={"user_input": label}
        )
        return parsed_events(response)[-1] if streaming else response.get_json()
    with ThreadPoolExecutor(max_workers=2) as executor:
        first, second = executor.map(request, ["first", "second"])
    assert first["response"] == "completed first"
    assert second["response"] == "completed second"
    assert sorted(echo.calls) == ["first", "second"]
    assert len(network.requests) == 4


def test_modified_or_already_loaded_kernel_is_refused(tmp_path):
    bad = tmp_path / "brainstem.py"
    bad.write_bytes((ROOT / "brainstem.py").read_bytes() + b"\n# fixture mutation\n")
    with pytest.raises(ProviderError, match="compatibility"):
        load_kernel(bad)
    with mock.patch.dict(sys.modules, {"brainstem": object()}):
        with pytest.raises(ProviderError, match="already loaded"):
            load_kernel(ROOT / "brainstem.py")


def test_kernel_is_executed_from_the_one_verified_read(snapshot):
    raw = snapshot.source.read_bytes()
    original_read = Path.read_bytes
    reads = []
    def read_then_change_path(path):
        data = original_read(path)
        if path == snapshot.source:
            reads.append(path)
            path.write_bytes(b"raise AssertionError('mutable path was executed')\n")
        return data
    try:
        with mock.patch.dict(sys.modules), mock.patch.object(Path, "read_bytes", read_then_change_path):
            sys.modules.pop("brainstem", None)
            loaded = load_kernel(snapshot.source)
        assert reads == [snapshot.source]
        assert loaded.sha256 == KERNEL_SHA256
        assert loaded.module.__file__ == str(snapshot.source)
        assert loaded.module.chat.__code__.co_code == snapshot.module.chat.__code__.co_code
        assert loaded.module.chat.__code__.co_filename == str(snapshot.source)
    finally:
        snapshot.source.write_bytes(raw)


def test_supplied_verified_kernel_bytes_are_not_reopened(snapshot):
    raw = snapshot.source.read_bytes()
    with mock.patch.dict(sys.modules), mock.patch.object(
        Path, "read_bytes", side_effect=AssertionError("snapshot source was reopened")
    ):
        sys.modules.pop("brainstem", None)
        loaded = load_kernel(snapshot.source, source_bytes=raw)
    assert loaded.sha256 == KERNEL_SHA256
    assert loaded.module.chat.__code__.co_code == snapshot.module.chat.__code__.co_code
