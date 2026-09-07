"""External provider registration, scoped HTTP adaptation, and request lifetime."""

from collections import OrderedDict
from collections.abc import Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass, field
import hashlib
import importlib
from importlib import metadata
import inspect
import json
import logging
from pathlib import Path
import re
import threading
from urllib.parse import urlsplit

import requests

from provider_plugins.base import (
    API_VERSION,
    CAPABILITIES,
    PluginSpec,
    PreparedRequest,
    ProviderError,
    ProviderPlugin,
    ProviderStreamEvent,
)


ENTRY_POINT_GROUP = "rapp.brainstem.providers.v1"
_PLUGIN_ID = re.compile(r"[a-z][a-z0-9_.-]{0,63}")
_VERSION = re.compile(
    r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
)
_BUILTIN_ENTRY = re.compile(r"(provider_plugins\.[a-z_][a-z0-9_]*):([A-Za-z_][A-Za-z0-9_]*)")
_REQUIRED_CAPABILITIES = frozenset({"catalog", "completion", "tool-calls"})
_COPILOT_HOSTS = frozenset({
    "api.githubcopilot.com",
    "api.individual.githubcopilot.com",
    "api.business.githubcopilot.com",
    "api.enterprise.githubcopilot.com",
})
_LOG = logging.getLogger("brainstem.providers")


def _valid_spec(spec):
    if not isinstance(spec, PluginSpec):
        raise ProviderError("Provider plugins must declare a PluginSpec")
    if not isinstance(spec.id, str) or not _PLUGIN_ID.fullmatch(spec.id):
        raise ProviderError("Invalid provider plugin id")
    if type(spec.api_version) is not int or spec.api_version != API_VERSION:
        raise ProviderError(f"Provider {spec.id} requires an unsupported API version")
    version = _VERSION.fullmatch(spec.version) if isinstance(spec.version, str) else None
    if version is None or any(
        part.isdigit() and len(part) > 1 and part.startswith("0")
        for part in (version.group(1) or "").split(".")
    ):
        raise ProviderError(f"Provider {spec.id} requires a semantic version")
    if (
        not isinstance(spec.capabilities, frozenset)
        or not _REQUIRED_CAPABILITIES <= spec.capabilities <= CAPABILITIES
    ):
        raise ProviderError(f"Provider {spec.id} declares incompatible capabilities")


def _valid_methods(plugin):
    for name, arity in (
        ("supports_model", 1), ("prepare_request", 3),
        ("translate_response", 3), ("translate_stream", 3),
    ):
        method = getattr(plugin, name, None)
        if (
            not callable(method)
            or inspect.iscoroutinefunction(method)
            or inspect.isasyncgenfunction(method)
        ):
            raise ProviderError(f"Provider {plugin.spec.id} has an incompatible {name} hook")
        try:
            inspect.signature(method).bind(*([None] * arity))
        except (TypeError, ValueError) as error:
            raise ProviderError(f"Provider {plugin.spec.id} has an incompatible {name} signature") from error


class ProviderRegistry:
    def __init__(self, plugins=()):
        self.plugins = tuple(plugins)
        seen = set()
        for plugin in self.plugins:
            if not isinstance(plugin, ProviderPlugin):
                raise ProviderError("Provider entry points must implement ProviderPlugin")
            _valid_spec(getattr(plugin, "spec", None))
            _valid_methods(plugin)
            if plugin.spec.id in seen:
                raise ProviderError(f"Duplicate provider plugin: {plugin.spec.id}")
            seen.add(plugin.spec.id)

    @property
    def ids(self):
        return tuple(plugin.spec.id for plugin in self.plugins)

    def resolve(self, model):
        matches = []
        for plugin in self.plugins:
            claimed = plugin.supports_model(deepcopy(model))
            if type(claimed) is not bool:
                raise ProviderError(f"Provider {plugin.spec.id} returned an invalid model claim")
            if claimed:
                matches.append(plugin)
        if len(matches) > 1:
            names = ", ".join(plugin.spec.id for plugin in matches)
            raise ProviderError(f"Conflicting provider registrations: {names}")
        return matches[0] if matches else None


def load_registry(manifest_path, selection=None, installed_entries=None):
    """Discover metadata only; import exactly the explicitly enabled plugins."""
    try:
        manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ProviderError("Cannot read the provider plugin manifest") from error
    if not isinstance(manifest, dict) or manifest.get("schema") != "brainstem-provider-plugins/1":
        raise ProviderError("Unsupported provider plugin manifest schema")
    declarations = manifest.get("plugins")
    if not isinstance(declarations, list):
        raise ProviderError("Provider manifest requires a plugins array")
    builtins = {}
    for item in declarations:
        if not isinstance(item, dict) or set(item) != {
            "id", "api_version", "version", "entrypoint", "capabilities"
        }:
            raise ProviderError("Invalid built-in provider declaration")
        capabilities = item["capabilities"]
        if (
            not isinstance(capabilities, list)
            or not all(isinstance(value, str) for value in capabilities)
            or len(set(capabilities)) != len(capabilities)
            or not isinstance(item["entrypoint"], str)
            or not _BUILTIN_ENTRY.fullmatch(item["entrypoint"])
        ):
            raise ProviderError("Invalid built-in provider declaration")
        spec = PluginSpec(
            item["id"], item["api_version"], frozenset(capabilities), item["version"]
        )
        _valid_spec(spec)
        if spec.id in builtins:
            raise ProviderError(f"Duplicate built-in provider: {spec.id}")
        builtins[spec.id] = (item, spec)

    if selection is None:
        wanted = []
    elif isinstance(selection, str):
        wanted = [] if selection.strip() in ("", "none") else [
            name.strip() for name in selection.split(",")
        ]
    else:
        raise ProviderError("Provider selection must be a comma-separated string")
    if len(set(wanted)) != len(wanted) or any(not _PLUGIN_ID.fullmatch(name) for name in wanted):
        raise ProviderError("Provider selection contains an invalid or duplicate id")
    if not wanted:
        return ProviderRegistry()

    entries = metadata.entry_points(group=ENTRY_POINT_GROUP) if installed_entries is None else installed_entries
    external = {}
    for entry in entries:
        if entry.group == ENTRY_POINT_GROUP and entry.name in wanted:
            if entry.name in builtins or entry.name in external:
                raise ProviderError(f"Ambiguous installed provider id: {entry.name}")
            external[entry.name] = entry

    plugins = []
    for name in wanted:
        expected = None
        try:
            if name in builtins:
                item, expected = builtins[name]
                module_name, class_name = item["entrypoint"].split(":")
                plugin_type = getattr(importlib.import_module(module_name), class_name)
            elif name in external:
                plugin_type = external[name].load()
            else:
                raise ProviderError(f"Enabled provider is not installed or registered: {name}")
        except (ImportError, AttributeError) as error:
            raise ProviderError(f"Cannot import enabled provider: {name}") from error
        if not isinstance(plugin_type, type) or not issubclass(plugin_type, ProviderPlugin):
            raise ProviderError(f"Provider {name} must export a ProviderPlugin class")
        spec = getattr(plugin_type, "spec", None)
        _valid_spec(spec)
        if spec.id != name or (expected is not None and spec != expected):
            raise ProviderError(f"Provider {name} does not match its registration")
        try:
            plugin = plugin_type()
        except TypeError as error:
            raise ProviderError(f"Cannot construct provider: {name}") from error
        if plugin.spec != spec:
            raise ProviderError(f"Provider {name} changed its declared contract during construction")
        plugins.append(plugin)
    return ProviderRegistry(plugins)


@dataclass
class _Binding:
    credential: str
    value: dict = field(default_factory=dict)
    completed: bool = False


@dataclass
class _RequestScope:
    bindings: dict = field(default_factory=dict)

    def bind(self, key, credential):
        binding = self.bindings.get(key)
        if binding is None:
            binding = self.bindings[key] = _Binding(credential)
        elif binding.credential != credential:
            if binding.completed:
                raise ProviderError(
                    "Provider credentials changed during a tool round. The request was stopped; "
                    "review prior agent actions before retrying."
                )
            binding = self.bindings[key] = _Binding(credential)
        return binding

    def clear(self):
        for binding in self.bindings.values():
            binding.value.clear()
        self.bindings.clear()


_CURRENT_SCOPE = ContextVar("brainstem_provider_scope", default=None)


@contextmanager
def _activate(scope):
    token = _CURRENT_SCOPE.set(scope)
    try:
        yield scope
    finally:
        _CURRENT_SCOPE.reset(token)


@contextmanager
def request_scope():
    """Give direct library callers the same bounded tool-round lifetime as HTTP."""
    scope = _RequestScope()
    try:
        with _activate(scope):
            yield scope
    finally:
        scope.clear()


class _ScopedIterable:
    def __init__(self, iterable, scope):
        self.iterable = iterable
        self.iterator = iter(iterable)
        self.scope = scope
        self.closed = False

    def __iter__(self):
        try:
            while not self.closed:
                with _activate(self.scope):
                    try:
                        chunk = next(self.iterator)
                    except StopIteration:
                        return
                yield chunk
        finally:
            self.close()

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            with _activate(self.scope):
                close = getattr(self.iterable, "close", None)
                if close is None:
                    close = getattr(self.iterator, "close", None)
                if close is not None:
                    close()
        finally:
            self.scope.clear()


class ProviderApplication:
    """Wrap WSGI iteration too: the kernel's SSE generator outlives Flask context."""
    def __init__(self, application, registry, kernel_sha256):
        self.application = application
        self.registry = registry
        self.kernel_sha256 = kernel_sha256

    def __call__(self, environ, start_response):
        scope = _RequestScope()

        def start(status, headers, exc_info=None):
            return start_response(status, [
                *headers,
                ("X-Brainstem-Provider-API", str(API_VERSION)),
                ("X-Brainstem-Provider-Plugins", ",".join(self.registry.ids)),
                ("X-Brainstem-Kernel-SHA256", self.kernel_sha256),
            ], exc_info)

        with _activate(scope):
            wrapped = None
            try:
                iterable = self.application(environ, start)
                wrapped = _ScopedIterable(iterable, scope)
                return wrapped
            finally:
                if wrapped is None:
                    scope.clear()


def _copilot_origin(url, path):
    if not isinstance(url, str):
        return None
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in _COPILOT_HOSTS
        or parsed.port not in (None, 443)
        or parsed.username or parsed.password or parsed.query or parsed.fragment
        or parsed.path != path
    ):
        return None
    return f"https://{parsed.netloc}"


def _credential_key(headers):
    if headers is None:
        return hashlib.sha256(b"").hexdigest()
    if not isinstance(headers, Mapping):
        raise ProviderError("Provider request headers must be a mapping")
    if not all(isinstance(key, str) for key in headers):
        raise ProviderError("Provider request header names must be strings")
    authorization = next(
        (value for key, value in headers.items() if key.lower() == "authorization"), ""
    )
    if not isinstance(authorization, str):
        raise ProviderError("Provider authorization must be a string")
    return hashlib.sha256(authorization.encode("utf-8")).hexdigest()


def _json_response(upstream, payload):
    response = requests.Response()
    response.status_code = upstream.status_code
    response.reason = upstream.reason
    response.url = upstream.url
    response.request = upstream.request
    response.headers = upstream.headers.copy()
    response.headers.pop("Content-Encoding", None)
    response._content = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    response._content_consumed = True
    response.encoding = "utf-8"
    response.headers["Content-Type"] = "application/json; charset=utf-8"
    response.headers["Content-Length"] = str(len(response._content))
    return response


def _validate_completion(completion):
    if not isinstance(completion, dict):
        raise ProviderError("Provider completion must be an object")
    choices = completion.get("choices")
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        raise ProviderError("Provider completion requires exactly one choice")
    choice = choices[0]
    message = choice.get("message")
    if not isinstance(message, dict) or message.get("role") != "assistant":
        raise ProviderError("Provider completion requires an assistant message")
    content = message.get("content")
    if content is not None and not isinstance(content, str):
        raise ProviderError("Provider completion content must be text or null")
    calls = message.get("tool_calls", [])
    if not isinstance(calls, list):
        raise ProviderError("Provider tool calls must be an array")
    ids = set()
    for call in calls:
        if not isinstance(call, dict) or call.get("type") != "function":
            raise ProviderError("Provider tool calls must be functions")
        call_id, function = call.get("id"), call.get("function")
        if not isinstance(call_id, str) or not call_id or call_id in ids:
            raise ProviderError("Provider tool call ids must be nonempty and unique")
        if (
            not isinstance(function, dict)
            or not isinstance(function.get("name"), str) or not function["name"]
            or not isinstance(function.get("arguments"), str)
        ):
            raise ProviderError("Malformed provider function call")
        ids.add(call_id)
    if choice.get("finish_reason") != ("tool_calls" if calls else "stop"):
        raise ProviderError("Provider completion has an inconsistent finish reason")
    if not calls and not content:
        raise ProviderError("Provider returned an empty completion")
    _json_safe(completion)
    return completion


def _json_safe(value):
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError) as error:
        raise ProviderError("Provider output is not JSON-compatible") from error


class _StreamContract:
    """Enforce the frozen kernel's delta assembly before releasing a DONE marker."""
    def __init__(self):
        self.content = []
        self.calls = {}
        self.finish_reason = None

    def add(self, chunk):
        if not isinstance(chunk, dict) or not isinstance(chunk.get("choices"), list):
            raise ProviderError("Malformed provider stream chunk")
        _json_safe(chunk)
        if len(chunk["choices"]) > 1:
            raise ProviderError("Provider streams require a single choice")
        for choice in chunk["choices"]:
            if not isinstance(choice, dict):
                raise ProviderError("Malformed provider stream choice")
            delta = choice.get("delta")
            delta = {} if delta is None else delta
            if not isinstance(delta, dict) or delta.get("role", "assistant") != "assistant":
                raise ProviderError("Malformed provider stream delta")
            reason = choice.get("finish_reason")
            if reason not in (None, "stop", "tool_calls"):
                raise ProviderError("Provider stream has an incomplete finish reason")
            if self.finish_reason and (delta or reason):
                raise ProviderError("Provider emitted data after its finish marker")
            if reason:
                self.finish_reason = reason
            content = delta.get("content")
            if content is not None and not isinstance(content, str):
                raise ProviderError("Provider content deltas must be text")
            if content:
                self.content.append(content)
            calls = delta.get("tool_calls")
            calls = [] if calls is None else calls
            if not isinstance(calls, list):
                raise ProviderError("Provider tool deltas must be an array")
            for call in calls:
                if not isinstance(call, dict):
                    raise ProviderError("Malformed provider tool delta")
                index = call.get("index", 0)
                if type(index) is not int or index < 0 or call.get("type", "function") != "function":
                    raise ProviderError("Malformed provider tool delta index or type")
                slot = self.calls.setdefault(index, {
                    "id": "", "type": "function", "function": {"name": "", "arguments": ""},
                })
                call_id = call.get("id")
                if call_id is not None and not isinstance(call_id, str):
                    raise ProviderError("Provider tool ids must be strings")
                if call_id:
                    if slot["id"] and slot["id"] != call_id:
                        raise ProviderError("Provider changed a tool id during streaming")
                    slot["id"] = call_id
                function = call.get("function")
                function = {} if function is None else function
                if not isinstance(function, dict):
                    raise ProviderError("Malformed provider function delta")
                for key in ("name", "arguments"):
                    value = function.get(key)
                    if value is not None and not isinstance(value, str):
                        raise ProviderError("Provider function deltas must be strings")
                    if value:
                        slot["function"][key] += value

    def finish(self, completion):
        choice = completion["choices"][0]
        message = choice["message"]
        calls = [self.calls[index] for index in sorted(self.calls)]
        expected_calls = [
            {"id": call["id"], "type": call["type"], "function": {
                "name": call["function"]["name"], "arguments": call["function"]["arguments"],
            }}
            for call in message.get("tool_calls", [])
        ]
        reason = self.finish_reason or ("tool_calls" if calls else "stop")
        if (
            ("".join(self.content) or None) != (message.get("content") or None)
            or calls != expected_calls or reason != choice["finish_reason"]
        ):
            raise ProviderError("Provider stream disagrees with its completed response")


def _provider_events(plugin, upstream, model, binding, context):
    upstream.encoding = "utf-8"
    complete = False
    contract = _StreamContract()
    for event in plugin.translate_stream(
        upstream.iter_lines(decode_unicode=True), deepcopy(model), context
    ):
        if not isinstance(event, ProviderStreamEvent) or complete:
            raise ProviderError("Invalid provider stream event sequence")
        if event.completion is not None:
            _validate_completion(event.completion)
            contract.finish(event.completion)
            complete = True
        else:
            contract.add(event.chunk)
        yield event
    if not complete:
        raise ProviderError("Provider stream ended without a completed response")
    binding.value.clear()
    binding.value.update(context)
    binding.completed = True


class _StreamResponse(requests.Response):
    def __init__(self, upstream, events):
        super().__init__()
        self.status_code = upstream.status_code
        self.reason = upstream.reason
        self.url = upstream.url
        self.request = upstream.request
        self.headers = upstream.headers.copy()
        self.headers.pop("Content-Length", None)
        self.headers.pop("Content-Encoding", None)
        self.headers["Content-Type"] = "text/event-stream; charset=utf-8"
        self.encoding = "utf-8"
        self._upstream = upstream
        self._events = events
        self._started = False
        self._closed = False

    def iter_content(self, chunk_size=1, decode_unicode=False):
        if self._started or self._closed:
            raise ProviderError("Provider stream cannot be consumed twice or after close")
        self._started = True
        try:
            for event in self._events:
                if event.chunk is not None:
                    frame = f"data: {json.dumps(event.chunk, ensure_ascii=False)}\n\n"
                    yield frame if decode_unicode else frame.encode("utf-8")
            ending = "data: [DONE]\n\n"
            yield ending if decode_unicode else ending.encode("utf-8")
        finally:
            self._content_consumed = True
            self.close()

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            self._events.close()
        finally:
            self._upstream.close()


@dataclass(frozen=True)
class _ModelRoute:
    model: dict
    plugin: ProviderPlugin | None


class ProviderHTTPClient:
    """A kernel-local Requests facade; unrelated modules keep ordinary Requests."""
    def __init__(self, registry, delegate=requests, legacy_models=()):
        self.registry = registry
        self.delegate = delegate
        self.legacy_models = frozenset(legacy_models)
        self._catalogs = OrderedDict()
        self._lock = threading.RLock()
        self.catalog_error = None

    def __getattr__(self, name):
        return getattr(self.delegate, name)

    def _record_catalog(self, origin, headers, payload):
        if isinstance(payload, list):
            models, field_name = payload, None
        elif isinstance(payload, dict):
            field_name = "data" if "data" in payload else "models"
            models = payload.get(field_name)
        else:
            raise ProviderError("Copilot catalog must be an object or array")
        if not isinstance(models, list):
            raise ProviderError("Copilot catalog requires a models array")
        routes, visible = {}, []
        for model in models:
            if not isinstance(model, dict):
                raise ProviderError("Invalid Copilot model metadata")
            model_id = model.get("id", model.get("model"))
            if not isinstance(model_id, str) or not model_id or model_id in routes:
                raise ProviderError("Copilot model ids must be nonempty and unique")
            descriptor = deepcopy(model)
            descriptor["id"] = model_id
            plugin = self.registry.resolve(descriptor)
            routes[model_id] = _ModelRoute(descriptor, plugin)
            exposed = deepcopy(model)
            if plugin is not None:
                endpoints = exposed.get("supported_endpoints")
                if not isinstance(endpoints, list) or not all(isinstance(item, str) for item in endpoints):
                    raise ProviderError("Adapted models must advertise their upstream endpoints")
                exposed["supported_endpoints"] = list(dict.fromkeys([*endpoints, "/chat/completions"]))
            visible.append(exposed)
        key = (origin, _credential_key(headers))
        with self._lock:
            self._catalogs[key] = routes
            self._catalogs.move_to_end(key)
            self.catalog_error = None
            while len(self._catalogs) > 8:
                self._catalogs.popitem(last=False)
        if field_name is None:
            return visible
        result = dict(payload)
        result[field_name] = visible
        return result

    def get(self, url, **kwargs):
        response = self.delegate.get(url, **kwargs)
        origin = _copilot_origin(url, "/models")
        if not self.registry.plugins or origin is None or response.status_code != 200:
            return response
        try:
            try:
                payload = response.json()
            except ValueError as error:
                raise ProviderError("Copilot returned an invalid model catalog") from error
            return _json_response(
                response, self._record_catalog(origin, kwargs.get("headers"), payload)
            )
        except ProviderError as error:
            with self._lock:
                self._catalogs.pop((origin, _credential_key(kwargs.get("headers"))), None)
                self.catalog_error = error
            _LOG.error("Provider catalog rejected: %s", error)
            raise
        finally:
            response.close()

    def _lookup(self, origin, headers, model_id):
        key = (origin, _credential_key(headers))
        with self._lock:
            catalog = self._catalogs.get(key)
            if catalog is not None:
                self._catalogs.move_to_end(key)
                return True, catalog.get(model_id)
        return False, None

    def post(self, url, **kwargs):
        origin = _copilot_origin(url, "/chat/completions")
        if not self.registry.plugins or origin is None:
            return self.delegate.post(url, **kwargs)
        body = kwargs.get("json")
        if not isinstance(body, dict) or not isinstance(body.get("model"), str):
            raise ProviderError("Provider requests require a JSON model id")
        model_id = body["model"]
        headers = kwargs.get("headers")
        cached, route = self._lookup(origin, headers, model_id)
        if not cached:
            catalog = self.get(
                origin + "/models", headers=headers,
                timeout=kwargs.get("timeout", 10), allow_redirects=False,
            )
            if catalog.status_code != 200:
                if model_id not in self.legacy_models:
                    return catalog
                _LOG.warning("Catalog unavailable; retaining legacy transport for %s", model_id)
                catalog.close()
                return self.delegate.post(url, **kwargs)
            catalog.close()
            _, route = self._lookup(origin, headers, model_id)
        if route is None:
            if model_id in self.legacy_models:
                return self.delegate.post(url, **kwargs)
            raise ProviderError("Selected model is absent from the current Copilot catalog")
        if route.plugin is None:
            return self.delegate.post(url, **kwargs)
        if kwargs.get("params") or "data" in kwargs or "files" in kwargs:
            raise ProviderError("Adapted provider requests require a plain JSON body")
        client_stream = kwargs.get("stream", False)
        if type(client_stream) is not bool:
            raise ProviderError("Provider streaming selection must be boolean")
        plugin = route.plugin
        if client_stream and "streaming" not in plugin.spec.capabilities:
            response = requests.Response()
            response.status_code = 400
            response._content = b'{"error":"This provider plugin does not support streaming"}'
            response._content_consumed = True
            response.encoding = "utf-8"
            return response
        scope = _CURRENT_SCOPE.get() or _RequestScope()
        binding = scope.bind(
            (origin, model_id, plugin.spec.id), _credential_key(headers)
        )
        context = deepcopy(binding.value)
        prepared = plugin.prepare_request(deepcopy(body), deepcopy(route.model), context)
        if (
            not isinstance(prepared, PreparedRequest)
            or not isinstance(prepared.body, dict)
            or type(prepared.stream) is not bool
            or prepared.body.get("stream") is not prepared.stream
            or prepared.body.get("model") != model_id
            or prepared.endpoint not in route.model.get("supported_endpoints", [])
            or not isinstance(prepared.endpoint, str)
            or not re.fullmatch(r"/[a-zA-Z0-9/_-]+", prepared.endpoint)
        ):
            raise ProviderError("Provider prepared an invalid or unadvertised request")
        if client_stream and not prepared.stream:
            raise ProviderError("A streaming provider request must use a streaming transport")
        outgoing = dict(kwargs)
        outgoing.update(json=prepared.body, stream=prepared.stream, allow_redirects=False)
        response = self.delegate.post(origin + prepared.endpoint, **outgoing)
        if 300 <= response.status_code < 400:
            response.close()
            raise ProviderError("Provider redirects are not allowed")
        if response.status_code != 200:
            try:
                # Errors can echo encrypted reasoning or tool input. Do not pass that
                # new protocol material to the legacy kernel's error-body logger.
                return _json_response(response, {"error": {
                    "code": "provider_http_error",
                    "message": f"Provider request failed (HTTP {response.status_code}).",
                }})
            finally:
                response.close()
        if prepared.stream:
            events = _provider_events(plugin, response, route.model, binding, context)
            if client_stream:
                return _StreamResponse(response, events)
            try:
                completion = None
                for event in events:
                    if event.completion is not None:
                        completion = event.completion
                return _json_response(response, completion)
            finally:
                events.close()
                response.close()
        try:
            try:
                payload = response.json()
            except ValueError as error:
                raise ProviderError("Provider returned invalid JSON") from error
            completion = _validate_completion(
                plugin.translate_response(payload, deepcopy(route.model), context)
            )
            translated = _json_response(response, completion)
            binding.value.clear()
            binding.value.update(context)
            binding.completed = True
            return translated
        finally:
            response.close()
