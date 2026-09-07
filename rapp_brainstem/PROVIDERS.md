# Provider plugins

ProviderTransport v1 extends Brainstem's provider protocols without editing
`brainstem.py`. The kernel remains responsible for authentication, HTTP routes,
model selection, conversations, and agent execution. This is not a second
server, a replacement agent framework, or a Python sandbox.

The normal `brainstem`, `start.sh`, and `start.ps1` paths run `launch.py`.
Running `python brainstem.py` directly retains the legacy kernel-only behavior
and does not enable provider plugins.

## Ownership boundaries

| Component | Responsibility |
|---|---|
| `kernel_compat.py` | Read and verify the pinned kernel once, execute those bytes, and own the explicit HTTP-dependency and runtime-identity bindings. Never replace kernel functions or routes. |
| `runtime_profile.json` | Versioned startup profile: kernel identity, provider API, default enabled provider IDs, and support repository. |
| `provider_host.py` | Explicit registration, compatibility validation, credential-bearing HTTP, routing, transactional request context, streaming validation, and cleanup. |
| `provider_plugins/base.py` | The public ProviderTransport v1 contract. |
| `provider_plugins/plugins.json` | Built-in registrations, not activation policy. |
| `provider_plugins/responses.py` | Copilot Responses conversion, including GPT-6 Astra. |

Tools still use `BasicAgent.perform()` and the existing discovery mechanism.
System context still uses `system_context()`. Provider plugins are neither
those agents nor the Copilot/Claude skill-marketplace plugins.

## Activation and diagnosis

The shipped runtime profile explicitly enables `responses`. Existing
Chat Completions models retain their original transport. The adapter exposes
its Chat Completions-compatible route only for models it can actually convert;
it retains the raw upstream endpoint metadata for routing.

An optional `.env` override replaces the profile's provider selection:

```dotenv
BRAINSTEM_PROVIDER_PLUGINS=responses,acme-provider
```

Use `none` to disable provider plugins. Select a legacy-compatible model before
disabling the provider required by a remembered model selection. Remove the
override to return to the versioned profile's defaults.

Changes to installed plugins, registrations, or activation require a restart.
There is no hot-swapping during conversations and no automatic package install,
download, activation, or update.

From `rapp_brainstem/`, validate startup without starting a server:

```bash
python launch.py --check
```

This imports explicitly enabled, trusted plugins and checks their contracts.
It does not initialize the agent loop or make model requests. The report includes
the kernel hash, provider API, and active plugin versions. Normal responses also
carry `X-Brainstem-Provider-API`, `X-Brainstem-Provider-Plugins`, and
`X-Brainstem-Kernel-SHA256` headers.

The version string `0.6.16` alone is not a compatibility guarantee. A different
kernel hash is refused, even when it reports that version. Supporting another
kernel requires a separately reviewed identity and compatibility binding, not
an "ignore the hash" switch.

## Writing a provider

Implement `ProviderPlugin` from `provider_plugins.base` and declare a class-level
`PluginSpec` with a stable ID, semantic plugin version, `api_version=1`, and
supported capabilities. Constructors must be argument-free and side-effect-free.

The required capabilities are `catalog`, `completion`, and `tool-calls`;
`streaming` is optional. Capabilities describe compatibility, not permissions.

| Method | Contract |
|---|---|
| `supports_model(model)` | Return a boolean based on the advertised model protocol. Never claim a model merely because its name looks familiar. |
| `prepare_request(body, model, context)` | Return `PreparedRequest(endpoint, body, stream)`. The model ID must remain unchanged and the endpoint must be one the upstream model advertised. |
| `translate_response(payload, model, context)` | Return one complete Chat Completions choice or raise `ProviderError`. Refusals must remain refusals; failures and incomplete output must not become success. |
| `translate_stream(lines, model, context)` | Yield `ProviderStreamEvent` chunks and exactly one completion. Chunks must assemble to that completion. Truncated, contradictory, or failed streams must raise. |

These are synchronous Python interfaces; asynchronous hooks require a different
contract. The host gives converters JSON, model metadata, and request-local
context, not credentials or network clients. Converters must not perform
network, process, filesystem, or kernel-mutating operations.

Context must be deepcopy-compatible protocol data, without open handles or
background work. It is copied transactionally for each provider call and
committed only after a complete, consistent response. It survives tool rounds
within that HTTP request, including SSE iteration after the Flask view returns.
It is cleared on completion, failure, or cancellation and never persisted.
Encrypted reasoning belongs there, not in user messages or logs.

The host refuses ambiguous model claims rather than choosing a plugin by import
order. It does not give plugins fallback authority to substitute another model.
The existing kernel's model selection and fallback behavior remain unchanged.
In particular, this layer does not provide per-conversation model selection,
durable conversation storage, or exactly-once agent execution.

The Responses adapter requires the final upstream `model` to exactly match the
selected catalog ID. Missing or mismatched identity is an error, never an
Astra-labeled success. Copilot can rewrap opaque Base64 response/item identifiers
between SSE snapshots; only those opaque identifiers may rotate. Clear-text
identifiers, model identity, content order, function names/arguments, and final
output consistency remain checked. Continuation uses the final output objects,
not provisional stream identifiers, and opaque values are never diagnostic text.

If credentials change after a completed provider response within a tool round,
the host stops rather than reusing opaque context under another credential.
Review any agent actions already performed before retrying.

## Packaging an installed plugin

Use Python's existing package entry-point mechanism; do not add ambient folder
scanning or execute a downloaded manifest.

```toml
[project]
name = "acme-brainstem-provider"
version = "1.0.0"
requires-python = ">=3.11"

[project.entry-points."rapp.brainstem.providers.v1"]
acme-provider = "acme_brainstem_provider:AcmeProvider"
```

The entry point must export a `ProviderPlugin` class whose spec ID is exactly
`acme-provider`. Install the reviewed package into the Brainstem environment
through normal package management, then explicitly enable its ID. Merely
installing it does not activate or import its provider entry point.

The v1 API is currently shipped with Brainstem's source, not as a separately
published PyPI SDK. Develop and exercise the plugin against that source and
its `provider_plugins.base` contract. Do not depend on host-private helpers,
kernel globals, or undocumented state.

Only trusted code belongs in-process. A malicious Python package can ignore
these rules, including through Python startup hooks. Untrusted plugin support
requires a separate process-isolation and permission design; a capabilities
array is not such a boundary.

## Compatibility and release policy

- Plugin version and provider API version are independent. Keep v1 compatible;
  breaking method, lifecycle, or data-contract changes require a new API and
  entry-point group rather than silently redefining v1.
- Built-in registration must match the implementation's ID, version, API, and
  capabilities. Duplicate IDs, unsupported APIs, bad hook signatures, and
  conflicting claims fail rather than silently degrading.
- Use the host conformance and real-kernel integration fixtures under `tests/`
  as the reference. Cover both normal and streaming calls, tool round trips,
  malformed output, cancellation, credential changes, and independent contexts.
- Package dependencies explicitly. Enabling or changing a plugin changes the
  application being qualified even when the kernel hash is unchanged.
- Sealed launches take their enabled provider IDs from the artifact's runtime
  profile, not ambient `.env` or parent-process overrides. External plugins for
  a sealed release must therefore be declared in that profile and present in
  the sealed dependency material.

Profile-enabled ring rendering never rewrites the kernel. It rewrites the
external runtime profile instead; the compatibility binding preserves the
ring's support destination. The sealed launcher passes its already-verified
kernel bytes into the same composition path instead of silently starting a
kernel-only runtime. Legacy artifacts without a profile retain their legacy
launch/render path; incomplete profiles are errors, not fallback requests.

State relocation, new credential providers, storage, UI extensions, and
untrusted-plugin isolation are not ProviderTransport v1 capabilities. Give
those real contracts when they are needed; do not expand this interface into
arbitrary hooks into the frozen kernel.
