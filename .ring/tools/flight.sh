#!/usr/bin/env bash
# flight.sh — run an experimental flight (a flight/<name> branch) fully
# isolated from the daily driver: own home, own clone, own venv, own data,
# own port. Flights can never ride the train (SOP.md §4) — this runner is how
# they become usable on-device anyway.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
FLIGHTS_HOME="${BRAINSTEM_FLIGHTS_HOME:-$HOME/.brainstem-flights}"

usage() {
    echo "usage: flight.sh {list|start|stop|status} [flight-name]" >&2
    exit 1
}

meta() { # meta <FLIGHT.json> <key> [default]
    python3 -c "
import json, sys
value = json.load(open(sys.argv[1])).get(sys.argv[2], '')
print(value if value != '' else (sys.argv[3] if len(sys.argv) > 3 else ''))
" "$@"
}

cmd="${1:-}"
name="${2:-}"

case "$cmd" in
    list)
        echo "flights on origin:"
        git -C "$REPO_ROOT" ls-remote --heads origin 'refs/heads/flight/*' \
            | sed 's#.*refs/heads/flight/#  #'
        ;;

    start)
        [ -n "$name" ] || usage
        base="$FLIGHTS_HOME/$name"
        src="$base/src"
        mkdir -p "$base"

        origin_url=$(git -C "$REPO_ROOT" remote get-url origin)
        if [ ! -d "$src/.git" ]; then
            echo "  Cloning flight/$name..."
            git clone --quiet --branch "flight/$name" --single-branch "$origin_url" "$src"
        else
            git -C "$src" pull --quiet 2>/dev/null || \
                echo "  ⚠ could not pull latest flight bytes — flying what's local"
        fi

        [ -f "$src/FLIGHT.json" ] || {
            echo "✗ flight/$name has no FLIGHT.json — not a flight branch" >&2
            exit 1
        }
        port=$(meta "$src/FLIGHT.json" port 7081)

        if [ ! -x "$base/venv/bin/python" ]; then
            echo "  Creating venv..."
            python3 -m venv "$base/venv"
            "$base/venv/bin/python" -m pip install --upgrade pip --quiet 2>/dev/null || true
        fi
        "$base/venv/bin/pip" install -q -r "$src/rapp_brainstem/requirements.txt"

        # Borrow the daily driver's auth read-only so a flight needs no re-login.
        daily_token="$HOME/.brainstem/src/rapp_brainstem/.copilot_token"
        if [ -f "$daily_token" ] && [ ! -f "$src/rapp_brainstem/.copilot_token" ]; then
            cp "$daily_token" "$src/rapp_brainstem/.copilot_token"
            chmod 600 "$src/rapp_brainstem/.copilot_token"
        fi

        # FLIGHT.json env block → exported for the server process only.
        envfile="$base/flight.env"
        python3 - "$src/FLIGHT.json" > "$envfile" <<'PY'
import json, sys
for key, value in (json.load(open(sys.argv[1])).get("env") or {}).items():
    print(f'export {key}="{value}"')
PY

        existing=$(lsof -ti tcp:"$port" -sTCP:LISTEN 2>/dev/null | head -1) || true
        if [ -n "$existing" ]; then
            echo "  Stopping previous flight server (PID $existing)..."
            kill "$existing" 2>/dev/null || true
            sleep 1
        fi

        (
            cd "$src/rapp_brainstem"
            set -a
            # shellcheck disable=SC1090
            . "$envfile"
            set +a
            PORT="$port" nohup "$base/venv/bin/python" brainstem.py \
                > "$base/flight.log" 2>&1 &
        )

        for _ in $(seq 1 30); do
            if curl -sf -o /dev/null --max-time 1 "http://localhost:$port/health"; then
                echo "✓ flight/$name up: http://localhost:$port  (log: $base/flight.log)"
                exit 0
            fi
            sleep 1
        done
        echo "✗ flight/$name did not answer on :$port — see $base/flight.log" >&2
        exit 1
        ;;

    status)
        [ -n "$name" ] || usage
        base="$FLIGHTS_HOME/$name"
        port=$(meta "$base/src/FLIGHT.json" port 7081 2>/dev/null || echo 7081)
        echo "flight/$name  port=$port  home=$base"
        curl -sf --max-time 2 "http://localhost:$port/health" \
            && echo "" || echo "  (not answering)"
        [ -f "$base/flight.log" ] && { echo "--- log tail ---"; tail -5 "$base/flight.log"; }
        ;;

    stop)
        [ -n "$name" ] || usage
        base="$FLIGHTS_HOME/$name"
        port=$(meta "$base/src/FLIGHT.json" port 7081 2>/dev/null || echo 7081)
        pid=$(lsof -ti tcp:"$port" -sTCP:LISTEN 2>/dev/null | head -1) || true
        if [ -n "$pid" ]; then
            kill "$pid" && echo "✓ flight/$name stopped (PID $pid)"
        else
            echo "flight/$name is not running"
        fi
        ;;

    *)
        usage
        ;;
esac
