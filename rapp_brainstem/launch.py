"""Normal Brainstem startup with explicitly enabled external provider plugins."""

import argparse
import json
import os
from pathlib import Path
import socketserver
import sys


ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from werkzeug.serving import ThreadedWSGIServer

from kernel_compat import bind_providers, initialize_kernel, load_kernel, load_runtime_profile
from provider_host import load_registry
from provider_plugins.base import API_VERSION, ProviderError


class _BrainstemServer(ThreadedWSGIServer):
    def server_bind(self):
        # Match the kernel's no-reverse-DNS startup without patching HTTPServer.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


def main(argv=None, *, verified_source=None, kernel_path=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check", action="store_true",
        help="Load the pinned kernel and enabled plugins without starting a server or making model requests.",
    )
    args = parser.parse_args(argv)
    try:
        snapshot = load_kernel(kernel_path, source_bytes=verified_source)
        profile = load_runtime_profile(ROOT / "runtime_profile.json")
        registry = load_registry(
            ROOT / "provider_plugins" / "plugins.json",
            selection=os.environ.get("BRAINSTEM_PROVIDER_PLUGINS", ",".join(profile.providers)),
        )
        application = bind_providers(snapshot, registry, profile=profile)
        if args.check:
            print(json.dumps({
                "kernel_sha256": snapshot.sha256,
                "provider_api": API_VERSION,
                "plugins": [
                    {"id": plugin.spec.id, "version": plugin.spec.version}
                    for plugin in registry.plugins
                ],
            }))
            return 0
        initialize_kernel(snapshot, registry)
    except ProviderError as error:
        print(f"[brainstem] Startup refused: {error}", file=sys.stderr)
        return 1
    with _BrainstemServer(snapshot.module.BIND_HOST, snapshot.module.PORT, application) as server:
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\n[brainstem] Server stopped.")
    return 0


def run_verified_kernel(source, path):
    """Compose the same runtime around a release gate's already-read kernel bytes."""
    if Path(path).resolve().parent != ROOT:
        raise ProviderError("Verified kernel snapshot belongs to another runtime directory")
    return main([], verified_source=source, kernel_path=path)


if __name__ == "__main__":
    raise SystemExit(main())
