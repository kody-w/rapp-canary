"""The sole compatibility binding for the immutable Brainstem kernel."""

from dataclasses import dataclass
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import sys
from types import ModuleType

from provider_host import ProviderApplication, ProviderHTTPClient
from provider_plugins.base import API_VERSION, ProviderError


KERNEL_SHA256 = "bd55a7f0bcf5efd3f7966ca39bb146da3c25fda9a0b1ce5ba587919d3c3775f4"


@dataclass(frozen=True)
class KernelSnapshot:
    module: ModuleType
    sha256: str
    source: Path


@dataclass(frozen=True)
class RuntimeProfile:
    support_repository: str
    providers: tuple[str, ...]


def load_runtime_profile(path):
    try:
        profile = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ProviderError("Cannot read the Brainstem runtime profile") from error
    if (
        not isinstance(profile, dict)
        or set(profile) != {
            "schema", "entrypoint", "kernel_sha256", "provider_api", "providers", "support_repository"
        }
        or profile.get("schema") != "brainstem-runtime-profile/1"
        or profile.get("entrypoint") != "launch.py"
        or profile.get("kernel_sha256") != KERNEL_SHA256
        or type(profile.get("provider_api")) is not int
        or profile["provider_api"] != API_VERSION
        or not isinstance(profile.get("providers"), list)
        or not all(
            isinstance(name, str) and re.fullmatch(r"[a-z][a-z0-9_.-]{0,63}", name)
            for name in profile["providers"]
        )
        or len(set(profile["providers"])) != len(profile["providers"])
        or not isinstance(profile.get("support_repository"), str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*", profile["support_repository"])
    ):
        raise ProviderError("Incompatible Brainstem runtime profile")
    return RuntimeProfile(profile["support_repository"], tuple(profile["providers"]))


def load_kernel(path=None, *, source_bytes=None):
    source = Path(path) if path is not None else Path(__file__).with_name("brainstem.py")
    if source.is_symlink():
        raise ProviderError("The provider launcher requires a regular kernel file")
    source = source.resolve()
    if source_bytes is None:
        try:
            raw = source.read_bytes()
        except OSError as error:
            raise ProviderError("Cannot read the pinned Brainstem kernel") from error
    elif isinstance(source_bytes, bytes):
        raw = source_bytes
    else:
        raise ProviderError("A verified kernel snapshot must be immutable bytes")
    digest = hashlib.sha256(raw).hexdigest()
    if digest != KERNEL_SHA256:
        raise ProviderError(
            "Kernel compatibility check failed. This launcher requires the pinned "
            "Brainstem kernel; it will not modify or replace that file."
        )
    if "brainstem" in sys.modules:
        raise ProviderError("The kernel is already loaded; provider bindings are startup-only")
    spec = importlib.util.spec_from_file_location("brainstem", source)
    if spec is None:
        raise ProviderError("Cannot create the Brainstem module specification")
    kernel = importlib.util.module_from_spec(spec)
    sys.modules["brainstem"] = kernel
    loaded = False
    try:
        # Execute the bytes just verified, not a second read of a mutable path.
        exec(compile(raw, str(source), "exec"), kernel.__dict__)
        loaded = True
    finally:
        if not loaded:
            sys.modules.pop("brainstem", None)
    return KernelSnapshot(kernel, digest, source)


def bind_providers(snapshot, registry, *, profile=None):
    kernel = snapshot.module
    if isinstance(kernel.requests, ProviderHTTPClient):
        raise ProviderError("Provider bindings cannot be replaced in a running kernel")
    if profile is not None and not isinstance(profile, RuntimeProfile):
        raise ProviderError("Runtime identity requires a validated profile")
    if registry.plugins:
        kernel.requests = ProviderHTTPClient(
            registry,
            delegate=kernel.requests,
            legacy_models=(model["id"] for model in kernel.AVAILABLE_MODELS),
        )
    if profile is not None:
        kernel.SUPPORT_REPO = profile.support_repository
    return ProviderApplication(kernel.app, registry, snapshot.sha256)


def initialize_kernel(snapshot, registry):
    """Reuse the kernel's initialization helpers; do not fork chat or auth logic."""
    kernel = snapshot.module
    kernel._tlog_load()
    kernel._start_tlog_autosave()
    kernel._tlog("server.starting", {
        "version": kernel.VERSION,
        "model": kernel.MODEL,
        "port": kernel.PORT,
        "lan_mode": kernel.LAN_MODE,
        "bind_host": kernel.BIND_HOST,
    })
    print(f"\n\U0001f9e0 RAPP Brainstem v{kernel.VERSION} starting on http://localhost:{kernel.PORT}")
    kernel._fetch_copilot_models()
    if isinstance(kernel.requests, ProviderHTTPClient) and kernel.requests.catalog_error is not None:
        raise kernel.requests.catalog_error
    kernel._auto_select_default_model()
    print(f"   Soul:   {kernel.SOUL_PATH}")
    print(f"   Agents: {kernel.AGENTS_PATH}")
    print(f"   Model:  {kernel.MODEL}")
    print(f"   Voice:  {'on' if kernel.VOICE_MODE else 'off'} (POST /voice/toggle to change)")
    print("   Auth:   GitHub Copilot API (via gh CLI)\n")
    kernel.load_soul()
    agents = kernel.load_agents()
    kernel._tlog("server.agents_loaded", {"agents": list(agents)})
    kernel._load_pending_login()
    if kernel.LAN_MODE:
        kernel._load_or_create_secret()
        print("   LAN:    enabled; non-loopback API calls require X-Brainstem-Secret")
    else:
        print("   LAN:    disabled (set BRAINSTEM_LAN_MODE=true to opt in)")
    kernel._tlog("provider.plugins_loaded", {
        "kernel_sha256": snapshot.sha256,
        "plugins": [
            {"id": plugin.spec.id, "version": plugin.spec.version, "api_version": plugin.spec.api_version}
            for plugin in registry.plugins
        ],
    })
    kernel._tlog("server.ready", {"url": f"http://localhost:{kernel.PORT}"})
