#!/bin/bash
# soak.sh — run the Canary ring's rendered bytes as a long-lived local server
# with REAL Copilot auth, isolated from the production ~/.brainstem install.
# This is the train's honest crash signal: live token exchange, live model
# selection, server rot over days — everything CI mocks.
#
#   soak.sh start [--no-auth]   render canary main -> serve on :7073
#   soak.sh status              health + version + uptime + log tail
#   soak.sh stop
#   soak.sh refresh [--no-auth] pull latest canary main, re-render, relaunch
set -euo pipefail

SOAK_HOME="${SOAK_HOME:-$HOME/.brainstem-soak}"
SOAK_PORT="${SOAK_PORT:-7073}"
RING_REPO="${RING_REPO:-https://github.com/kody-w/rapp-canary.git}"
TOKEN_SOURCE="${TOKEN_SOURCE:-}"
HERE="$(cd "$(dirname "$0")" && pwd)"

say() { echo "[soak] $1"; }
die() { echo "[soak] ✗ $1" >&2; exit 1; }

pid_alive() {
    [ -f "$SOAK_HOME/soak.pid" ] || return 1
    kill -0 "$(cat "$SOAK_HOME/soak.pid")" 2>/dev/null
}

do_stop() {
    if pid_alive; then
        kill "$(cat "$SOAK_HOME/soak.pid")" 2>/dev/null || true
        sleep 1
        say "stopped pid $(cat "$SOAK_HOME/soak.pid")"
    else
        say "not running"
    fi
    rm -f "$SOAK_HOME/soak.pid"
}

do_start() {
    local auth="${1:-auth}"
    pid_alive && die "already running (pid $(cat "$SOAK_HOME/soak.pid")) — use refresh"
    local state="$SOAK_HOME/state"
    mkdir -p "$SOAK_HOME" "$state"

    say "cloning canary main"
    rm -rf "$SOAK_HOME/src"
    git clone --quiet --depth 1 --branch main "$RING_REPO" "$SOAK_HOME/src"
    local sha
    sha="$(git -C "$SOAK_HOME/src" rev-parse HEAD)"

    say "rendering ring identity"
    rm -rf "$SOAK_HOME/render"
    python3 "$SOAK_HOME/src/.ring/tools/render_ring.py" \
        --repo "$SOAK_HOME/src" \
        --config "$SOAK_HOME/src/.ring/ring.json" \
        --output "$SOAK_HOME/render" \
        --report "$SOAK_HOME/render.json"

    if [ ! -d "$SOAK_HOME/venv" ]; then
        say "creating venv"
        python3 -m venv "$SOAK_HOME/venv"
    fi
    "$SOAK_HOME/venv/bin/python" -m pip install --quiet \
        -r "$SOAK_HOME/render/rapp_brainstem/requirements.txt"

    if [ "$auth" = "auth" ]; then
        local token_source="$TOKEN_SOURCE"
        if [ -z "$token_source" ]; then
            for candidate in "$HOME/.brainstem/state/.copilot_token" \
                             "$HOME/.brainstem/src/rapp_brainstem/.copilot_token"; do
                if [ -f "$candidate" ]; then token_source="$candidate"; break; fi
            done
        fi
        [ -n "$token_source" ] && [ -f "$token_source" ] \
            || die "no Copilot token found (use --no-auth for an unauthenticated soak)"
        cp "$token_source" "$state/.copilot_token"
        chmod 600 "$state/.copilot_token"
        for session_source in "$HOME/.brainstem/state/.copilot_session" \
                              "$HOME/.brainstem/src/rapp_brainstem/.copilot_session"; do
            if [ -f "$session_source" ]; then
                cp "$session_source" "$state/.copilot_session"
                chmod 600 "$state/.copilot_session"
                break
            fi
        done
        say "real Copilot token installed (soak-local copy)"
    else
        rm -f "$state/.copilot_token" "$state/.copilot_session"
    fi

    say "launching on :$SOAK_PORT"
    (
        cd "$SOAK_HOME/render/rapp_brainstem"
        HOME="$SOAK_HOME" BRAINSTEM_STATE_DIR="$state" PORT="$SOAK_PORT" \
            nohup "$SOAK_HOME/venv/bin/python" brainstem.py \
            > "$SOAK_HOME/soak.log" 2>&1 &
        echo $! > "$SOAK_HOME/soak.pid"
    )
    echo "$sha $(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$SOAK_HOME/soaking-since"

    for _ in $(seq 1 20); do
        sleep 1
        if curl -fsS "http://localhost:$SOAK_PORT/health" >/dev/null 2>&1; then
            say "✓ serving canary@${sha:0:12} on http://localhost:$SOAK_PORT"
            return 0
        fi
    done
    tail -5 "$SOAK_HOME/soak.log" >&2
    die "server did not answer /health within 20s (log above)"
}

do_status() {
    if ! pid_alive; then
        say "not running"
        [ -f "$SOAK_HOME/soaking-since" ] && say "last soak: $(cat "$SOAK_HOME/soaking-since")"
        exit 1
    fi
    say "pid $(cat "$SOAK_HOME/soak.pid") since $(cat "$SOAK_HOME/soaking-since" 2>/dev/null || echo '?')"
    curl -fsS "http://localhost:$SOAK_PORT/health" | python3 -m json.tool | sed 's/^/[soak]   /' || die "/health failed"
    say "log tail:"
    tail -5 "$SOAK_HOME/soak.log" | sed 's/^/[soak]   /'
}

case "${1:-}" in
    start)   do_start "$([ "${2:-}" = "--no-auth" ] && echo no-auth || echo auth)" ;;
    stop)    do_stop ;;
    status)  do_status ;;
    refresh) do_stop; do_start "$([ "${2:-}" = "--no-auth" ] && echo no-auth || echo auth)" ;;
    *) echo "usage: soak.sh start|status|stop|refresh [--no-auth]" >&2; exit 2 ;;
esac
