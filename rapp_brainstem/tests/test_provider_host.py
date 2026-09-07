"""ProviderTransport v1 conformance and HTTP-boundary regressions, without network."""

from copy import deepcopy
from dataclasses import replace
import io
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests
from werkzeug.test import Client
from werkzeug.wrappers import Response

import provider_host as host
from provider_plugins.base import (
    CAPABILITIES, PluginSpec, PreparedRequest, ProviderError, ProviderPlugin, ProviderStreamEvent,
)


ORIGIN = "https://api.enterprise.githubcopilot.com"
HEADERS = {"Authorization": "Bearer synthetic-a", "Editor-Version": "fixture"}
MODEL = {
    "id": "fixture-responses", "name": "Fixture Responses",
    "model_picker_enabled": True, "policy": {"state": "enabled"},
    "capabilities": {"type": "chat", "supports": {"tool_calls": True, "streaming": True}},
    "supported_endpoints": ["/responses"],
}
LEGACY = {
    "id": "gpt-4o", "name": "Legacy", "supported_endpoints": ["/chat/completions"],
    "capabilities": {"type": "chat"},
}


def completion(text="answer", calls=None):
    message = {"role": "assistant", "content": text}
    if calls:
        message["tool_calls"] = calls
    return {"model": MODEL["id"], "choices": [{
        "message": message, "finish_reason": "tool_calls" if calls else "stop",
    }]}


def chunk(content=None, reason=None, calls=None):
    delta = {}
    if content is not None:
        delta["content"] = content
    if calls is not None:
        delta["tool_calls"] = calls
    return {"choices": [{"delta": delta, "finish_reason": reason}]}


def response(payload, status=200, streaming=False):
    result = requests.Response()
    result.status_code = status
    result.reason = "fixture"
    result.url = ORIGIN + "/responses"
    result.request = requests.Request("POST", result.url).prepare()
    encoded = json.dumps(payload).encode("utf-8")
    if streaming:
        result.raw = io.BytesIO(encoded)
    else:
        result._content = encoded
        result._content_consumed = True
    result.encoding = "utf-8"
    result.close = Mock(wraps=result.close)
    return result


class StubPlugin(ProviderPlugin):
    spec = PluginSpec("stub", 1, CAPABILITIES, "1.0.0")
    upstream_stream = True

    def __init__(self):
        self.seen = []

    def supports_model(self, model):
        return model.get("supported_endpoints") == ["/responses"]

    def prepare_request(self, body, model, context):
        self.seen.append(deepcopy(context))
        context["prepared"] = True
        return PreparedRequest("/responses", {
            "model": body["model"], "input": body["messages"], "stream": self.upstream_stream,
        }, self.upstream_stream)

    def translate_response(self, payload, model, context):
        context["receipt"] = payload["text"]
        return completion(payload["text"])

    def translate_stream(self, lines, model, context):
        payload = json.loads("".join(lines))
        context["receipt"] = payload["text"]
        yield ProviderStreamEvent(chunk=chunk(payload["text"]))
        if payload.get("fail"):
            raise ProviderError("Synthetic interrupted provider")
        yield ProviderStreamEvent(chunk=chunk(reason="stop"))
        yield ProviderStreamEvent(completion=completion(payload.get("final", payload["text"])))
        if payload.get("late"):
            yield ProviderStreamEvent(chunk=chunk("late"))


class FakeHTTP:
    exceptions = requests.exceptions

    def __init__(self):
        self.get_calls = []
        self.post_calls = []
        self.catalog = {"data": [deepcopy(MODEL), deepcopy(LEGACY)]}
        self.catalog_status = 200
        self.payloads = [{"text": "answer"}]
        self.responses = []

    def get(self, url, **kwargs):
        self.get_calls.append((url, kwargs))
        return response(self.catalog, self.catalog_status)

    def post(self, url, **kwargs):
        self.post_calls.append((url, kwargs))
        payload = self.payloads.pop(0) if self.payloads else {"text": "answer"}
        result = response(payload, streaming=kwargs.get("stream", False))
        self.responses.append(result)
        return result


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        pytest.fail("Provider host tests attempted real HTTP")
    monkeypatch.setattr(requests.sessions.Session, "request", refuse)


@pytest.fixture
def transport():
    plugin = StubPlugin()
    network = FakeHTTP()
    client = host.ProviderHTTPClient(host.ProviderRegistry([plugin]), network, ["gpt-4o"])
    return client, plugin, network


def call(client, model=MODEL["id"], streaming=False, headers=None):
    return client.post(ORIGIN + "/chat/completions", headers=headers or HEADERS, json={
        "model": model, "messages": [{"role": "user", "content": "fixture"}],
    }, stream=streaming, timeout=(1, 2))


def manifest(tmp_path, **overrides):
    declaration = {
        "id": "stub", "api_version": 1, "version": "1.0.0",
        "entrypoint": "provider_plugins.fixture:StubPlugin",
        "capabilities": sorted(CAPABILITIES),
    }
    declaration.update(overrides)
    path = tmp_path / "plugins.json"
    path.write_text(json.dumps({"schema": "brainstem-provider-plugins/1", "plugins": [declaration]}))
    return path


def test_builtin_registration_and_explicit_disable(tmp_path, monkeypatch):
    load = Mock(return_value=SimpleNamespace(StubPlugin=StubPlugin))
    monkeypatch.setattr(host.importlib, "import_module", load)
    path = manifest(tmp_path)
    assert host.load_registry(path, selection="stub", installed_entries=[]).ids == ("stub",)
    load.assert_called_once_with("provider_plugins.fixture")
    load.reset_mock()
    assert host.load_registry(path, selection="none", installed_entries=[]).ids == ()
    load.assert_not_called()


def test_installed_entry_points_are_not_implicitly_loaded(tmp_path):
    entry = SimpleNamespace(name="external", group=host.ENTRY_POINT_GROUP, load=Mock())
    assert host.load_registry(manifest(tmp_path), installed_entries=[entry]).ids == ()
    entry.load.assert_not_called()


def test_explicit_installed_plugin_and_duplicate_refusal(tmp_path):
    class External(StubPlugin):
        spec = replace(StubPlugin.spec, id="external")
    entry = SimpleNamespace(
        name="external", group=host.ENTRY_POINT_GROUP, load=Mock(return_value=External)
    )
    path = manifest(tmp_path)
    assert host.load_registry(path, "external", [entry]).ids == ("external",)
    with pytest.raises(ProviderError, match="Ambiguous"):
        host.load_registry(path, "external", [entry, entry])


@pytest.mark.parametrize("selection", ["missing", "stub,stub", "stub,", "../stub"])
def test_invalid_plugin_selections_fail(tmp_path, selection):
    with pytest.raises(ProviderError):
        host.load_registry(manifest(tmp_path), selection, [])


@pytest.mark.parametrize("overrides", [
    {"api_version": 2}, {"api_version": True}, {"version": "1.0"},
    {"version": "01.0.0"}, {"version": "1.0.0-01"},
    {"capabilities": ["catalog"]}, {"entrypoint": "untrusted.module:Plugin"},
    {"capabilities": sorted(CAPABILITIES) + ["shell"]},
])
def test_incompatible_manifests_fail_before_import(tmp_path, overrides, monkeypatch):
    load = Mock()
    monkeypatch.setattr(host.importlib, "import_module", load)
    with pytest.raises(ProviderError):
        host.load_registry(manifest(tmp_path, **overrides), selection="stub", installed_entries=[])
    load.assert_not_called()


def test_semver_prerelease_build_metadata_is_supported():
    plugin = StubPlugin()
    plugin.spec = replace(plugin.spec, version="1.2.3-rc.1+build.7")
    assert host.ProviderRegistry([plugin]).ids == ("stub",)


def test_registration_must_match_implementation(tmp_path, monkeypatch):
    class WrongVersion(StubPlugin):
        spec = replace(StubPlugin.spec, version="2.0.0")
    monkeypatch.setattr(host.importlib, "import_module", lambda name: SimpleNamespace(StubPlugin=WrongVersion))
    with pytest.raises(ProviderError, match="does not match"):
        host.load_registry(manifest(tmp_path), selection="stub", installed_entries=[])


def test_duplicate_ids_and_overlapping_model_claims_fail():
    with pytest.raises(ProviderError, match="Duplicate"):
        host.ProviderRegistry([StubPlugin(), StubPlugin()])
    other = StubPlugin()
    other.spec = replace(other.spec, id="other")
    with pytest.raises(ProviderError, match="Conflicting"):
        host.ProviderRegistry([StubPlugin(), other]).resolve(MODEL)


def test_hook_signatures_and_async_hooks_are_rejected_at_registration():
    plugin = StubPlugin()
    plugin.prepare_request = lambda: None
    with pytest.raises(ProviderError, match="signature"):
        host.ProviderRegistry([plugin])
    async def asynchronous(model):
        return False
    plugin = StubPlugin()
    plugin.supports_model = asynchronous
    with pytest.raises(ProviderError, match="incompatible supports_model"):
        host.ProviderRegistry([plugin])


def test_catalog_contract_failure_invalidates_stale_routing(transport):
    client, _, network = transport
    client.get(ORIGIN + "/models", headers=HEADERS)
    network.catalog = {"data": [deepcopy(MODEL), deepcopy(MODEL)]}
    with pytest.raises(ProviderError, match="unique"):
        client.get(ORIGIN + "/models", headers=HEADERS)
    assert client.catalog_error is not None
    with pytest.raises(ProviderError, match="unique"):
        call(client)
    assert network.post_calls == []


def test_catalog_adds_only_earned_route_and_preserves_policy(transport):
    client, _, network = transport
    before = deepcopy(network.catalog)
    visible = client.get(ORIGIN + "/models", headers=HEADERS).json()
    assert visible["data"][0]["supported_endpoints"] == ["/responses", "/chat/completions"]
    expected = deepcopy(before)
    expected["data"][0]["supported_endpoints"].append("/chat/completions")
    assert visible == expected
    assert network.catalog == before


def test_legacy_transport_and_unrelated_requests_are_unchanged(transport):
    client, _, network = transport
    client.get(ORIGIN + "/models", headers=HEADERS)
    body = {"model": "gpt-4o", "messages": []}
    client.post(ORIGIN + "/chat/completions", json=body, headers=HEADERS, timeout=9)
    assert network.post_calls[-1] == (
        ORIGIN + "/chat/completions", {"json": body, "headers": HEADERS, "timeout": 9}
    )
    assert network.post_calls[-1][1]["json"] is body
    client.post("https://api.github.com/login", data={"fixture": "only"})
    assert network.post_calls[-1][1] == {"data": {"fixture": "only"}}
    assert client.exceptions is requests.exceptions


def test_disabled_registry_is_exact_pass_through():
    network = FakeHTTP()
    client = host.ProviderHTTPClient(host.ProviderRegistry(), network)
    call(client)
    assert network.get_calls == []
    assert network.post_calls[0][0].endswith("/chat/completions")


def test_sync_caller_buffers_stream_and_closes_upstream(transport):
    client, _, network = transport
    reply = call(client).json()
    assert reply == completion()
    url, kwargs = network.post_calls[0]
    assert url == ORIGIN + "/responses"
    assert kwargs["stream"] is True
    assert kwargs["allow_redirects"] is False
    assert kwargs["timeout"] == (1, 2)
    assert kwargs["headers"] is HEADERS
    assert "Authorization" not in kwargs["json"]
    network.responses[0].close.assert_called_once()


def test_stream_caller_gets_deltas_then_done_and_can_close_once(transport):
    client, _, network = transport
    reply = call(client, streaming=True)
    lines = list(reply.iter_lines(decode_unicode=True))
    assert any('"content": "answer"' in line for line in lines)
    assert lines[-2:] == ["data: [DONE]", ""]
    reply.close()
    network.responses[0].close.assert_called_once()


@pytest.mark.parametrize("bad", [{"fail": True}, {"final": "contradiction"}, {"late": True}])
def test_failed_stream_has_no_done_or_committed_context(transport, bad):
    client, _, network = transport
    network.payloads = [{"text": "partial", **bad}]
    with host.request_scope() as scope:
        reply = call(client, streaming=True)
        lines = []
        with pytest.raises(ProviderError):
            for line in reply.iter_lines(decode_unicode=True):
                lines.append(line)
        assert any('"content": "partial"' in line for line in lines)
        assert "data: [DONE]" not in lines
        binding = next(iter(scope.bindings.values()))
        assert not binding.completed
        assert binding.value == {}
        reply.close()
    network.responses[0].close.assert_called_once()


def test_context_is_per_request_and_continues_only_completed_rounds(transport):
    client, plugin, network = transport
    network.payloads = [{"text": "one"}, {"text": "two"}, {"text": "three"}]
    with host.request_scope() as scope:
        call(client)
        call(client)
        assert plugin.seen == [{}, {"prepared": True, "receipt": "one"}]
    assert scope.bindings == {}
    with host.request_scope():
        call(client)
    assert plugin.seen[-1] == {}


def test_credential_change_cannot_reuse_a_completed_tool_context(transport):
    client, _, network = transport
    with host.request_scope():
        call(client)
        with pytest.raises(ProviderError, match="credentials changed"):
            call(client, headers={"Authorization": "Bearer synthetic-b"})
    assert len(network.post_calls) == 1
    assert len(network.get_calls) == 2


def test_legacy_can_still_run_when_catalog_is_unavailable(transport):
    client, _, network = transport
    network.catalog_status = 503
    call(client, model="gpt-4o")
    assert network.post_calls[-1][0].endswith("/chat/completions")
    result = call(client)
    assert result.status_code == 503
    assert len(network.post_calls) == 1


def test_unadvertised_target_or_model_substitution_cannot_send(transport):
    client, plugin, network = transport
    for prepared in (
        PreparedRequest("https://other.invalid/responses", {"model": MODEL["id"], "stream": True}, True),
        PreparedRequest("/responses", {"model": "another-model", "stream": True}, True),
    ):
        plugin.prepare_request = lambda *args, selected=prepared: selected
        with pytest.raises(ProviderError, match="unadvertised"):
            call(client)
    assert network.post_calls == []


def test_wsgi_stream_scope_survives_view_return_and_is_cleared(transport):
    client, plugin, network = transport
    network.payloads = [{"text": "one"}, {"text": "two"}]
    captured = []
    def application(environ, start_response):
        start_response("200 OK", [("Content-Type", "text/plain")])
        captured.append(host._CURRENT_SCOPE.get())
        def generate():
            call(client)
            yield b"one"
            call(client)
            yield b"two"
        return generate()
    wrapped = host.ProviderApplication(application, client.registry, "f" * 64)
    result = Client(wrapped, Response).get("/")
    assert host._CURRENT_SCOPE.get() is None
    assert result.get_data() == b"onetwo"
    result.close()
    assert plugin.seen == [{}, {"prepared": True, "receipt": "one"}]
    assert captured[0].bindings == {}
    assert result.headers["X-Brainstem-Provider-API"] == "1"
    assert result.headers["X-Brainstem-Provider-Plugins"] == "stub"


def test_wsgi_cancellation_closes_an_inflight_provider(transport):
    client, _, network = transport
    captured = []
    def application(environ, start_response):
        start_response("200 OK", [])
        captured.append(host._CURRENT_SCOPE.get())
        def generate():
            reply = call(client, streaming=True)
            try:
                yield from reply.iter_content()
            finally:
                reply.close()
        return generate()
    wrapped = host.ProviderApplication(application, client.registry, "f" * 64)
    result = Client(wrapped, Response).get("/", buffered=False)
    assert captured[0].bindings
    result.close()
    network.responses[0].close.assert_called_once()
    assert captured[0].bindings == {}
    assert host._CURRENT_SCOPE.get() is None


def test_wsgi_application_failure_clears_its_request_state(transport):
    client, _, _ = transport
    captured = []
    def application(environ, start_response):
        captured.append(host._CURRENT_SCOPE.get())
        call(client)
        raise ProviderError("Synthetic application failure")
    wrapped = host.ProviderApplication(application, client.registry, "f" * 64)
    with pytest.raises(ProviderError, match="application failure"):
        Client(wrapped, Response).get("/")
    assert captured[0].bindings == {}
    assert host._CURRENT_SCOPE.get() is None


def test_provider_http_errors_preserve_status_without_echoing_private_protocol_data(transport):
    client, _, network = transport
    private = response({"error": {"message": "opaque-context-and-tool-input"}}, status=401)
    network.post = Mock(return_value=private)
    returned = call(client)
    assert returned.status_code == 401
    assert "opaque-context" not in returned.text
    assert "401" in returned.text
    private.close.assert_called_once()
