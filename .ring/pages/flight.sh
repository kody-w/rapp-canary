#!/bin/bash
# flight.sh — sandboxed TEST FLIGHT of any pre-grail ring, or of a specific
# feature branch ("flight"), kicked off from a public one-liner on any machine:
#
#   curl -fsSL https://kody-w.github.io/rapp-canary/flight.sh | bash -s -- nightly
#   curl -fsSL https://kody-w.github.io/rapp-canary/flight.sh | bash -s -- canary fix/streaming
#
# Nothing touches an existing ~/.brainstem install: everything lives under
# ~/.rapp-flight/<ring>[-<branch>]/ and serves on its own port (default 7075).
# Needs: git, python3, curl. No account required — auth happens in the web UI
# via GitHub device flow if and when you want a live model.
set -euo pipefail

RING="${1:-canary}"
BRANCH="${2:-main}"
FLIGHT_PORT="${FLIGHT_PORT:-7075}"
HUB_RAW="https://raw.githubusercontent.com/kody-w/rapp-canary/main"

case "$RING" in
    canary|nightly|alpha|beta) ;;
    *) echo "✗ unknown ring: $RING (canary|nightly|alpha|beta)" >&2; exit 2 ;;
esac
if [ "$BRANCH" != "main" ] && [ "$RING" != "canary" ]; then
    echo "✗ feature flights ride canary — branch flights are canary-only" >&2; exit 2
fi
command -v git >/dev/null || { echo "✗ git is required" >&2; exit 1; }
command -v python3 >/dev/null || { echo "✗ python3 is required" >&2; exit 1; }

SLUG="$RING"; [ "$BRANCH" != "main" ] && SLUG="$RING-$(echo "$BRANCH" | tr '/' '-')"
FLIGHT_HOME="$HOME/.rapp-flight/$SLUG"
REPO_URL="https://github.com/kody-w/rapp-$RING.git"

echo "🛫 flight: $RING @ $BRANCH -> $FLIGHT_HOME (port $FLIGHT_PORT)"
if [ -f "$FLIGHT_HOME/flight.pid" ] && kill -0 "$(cat "$FLIGHT_HOME/flight.pid")" 2>/dev/null; then
    kill "$(cat "$FLIGHT_HOME/flight.pid")"; sleep 1
fi
rm -rf "$FLIGHT_HOME/src" "$FLIGHT_HOME/render"; mkdir -p "$FLIGHT_HOME"

git clone --quiet --depth 1 --branch "$BRANCH" "$REPO_URL" "$FLIGHT_HOME/src"
SHA=$(git -C "$FLIGHT_HOME/src" rev-parse --short HEAD)

# Render the ring's real identity (URL rewrites + drift oracle). The hub carries
# the tool; other rings carry only their ring.json.
RENDER="$FLIGHT_HOME/src/.ring/tools/render_ring.py"
if [ ! -f "$RENDER" ]; then
    RENDER="$FLIGHT_HOME/render_ring.py"
    curl -fsSL "$HUB_RAW/.ring/tools/render_ring.py" -o "$RENDER"
fi
if ! python3 "$RENDER" \
    --repo "$FLIGHT_HOME/src" --config "$FLIGHT_HOME/src/.ring/ring.json" \
    --output "$FLIGHT_HOME/render" --report "$FLIGHT_HOME/render.json"; then
    echo "✗ render refused this flight (rewrite drift oracle) — the branch likely" >&2
    echo "  changed grail-URL counts without bumping .ring/ring.json. That is a" >&2
    echo "  finding, not a flake: report it on the ring repo." >&2
    exit 1
fi

if [ ! -d "$FLIGHT_HOME/venv" ]; then python3 -m venv "$FLIGHT_HOME/venv"; fi
"$FLIGHT_HOME/venv/bin/python" -m pip install --quiet -r "$FLIGHT_HOME/render/rapp_brainstem/requirements.txt"

(
    cd "$FLIGHT_HOME/render/rapp_brainstem"
    HOME="$FLIGHT_HOME" PORT="$FLIGHT_PORT" \
        nohup "$FLIGHT_HOME/venv/bin/python" brainstem.py > "$FLIGHT_HOME/flight.log" 2>&1 &
    echo $! > "$FLIGHT_HOME/flight.pid"
)
for _ in $(seq 1 20); do
    sleep 1
    if curl -fsS "http://localhost:$FLIGHT_PORT/health" >/dev/null 2>&1; then
        echo "✅ $RING@$SHA is flying: http://localhost:$FLIGHT_PORT"
        echo "   auth (optional): open the UI and use Login — GitHub device flow"
        echo "   stop:  kill \$(cat $FLIGHT_HOME/flight.pid)"
        echo "   wipe:  rm -rf $FLIGHT_HOME"
        exit 0
    fi
done
tail -5 "$FLIGHT_HOME/flight.log" >&2
echo "✗ flight did not answer /health in 20s (log above) — please report this on kody-w/rapp-$RING" >&2
exit 1
