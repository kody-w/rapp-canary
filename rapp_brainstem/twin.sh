#!/usr/bin/env bash
# twin.sh — TWIN IN RESIDENCE: run a repo's .twin/ (".claude but for RAPP
# twins"). Any repo carrying .twin/ gets a project-specialized brainstem that
# any AI can invoke over the same /chat contract as the daily driver.
# Spec: TWIN.md at the repo root of the project-twin flight.
#
#   twin.sh init [repo]     scaffold .twin/ into a repo (refuses to overwrite)
#   twin.sh up [repo]       launch the twin: own port, isolated memory
#   twin.sh status [repo]
#   twin.sh down [repo]
#
# Isolation: the engine is SYMLINKED into .twin/private/engine/ — brainstem
# anchors its data dir at its own script path without resolving symlinks, so
# the twin's memories land in .twin/private/engine/.brainstem_data/, never in
# the daily driver's. .twin/private/ is gitignored: on-device only.
set -euo pipefail

CMD="${1:-}"
ARG="${2:-$PWD}"

REPO=$(cd "$ARG" 2>/dev/null && git rev-parse --show-toplevel 2>/dev/null) \
    || { echo "not a git repo: $ARG" >&2; exit 2; }
TWIN="$REPO/.twin"

engine_dir() {
    if [ -f "$REPO/rapp_brainstem/brainstem.py" ]; then
        echo "$REPO/rapp_brainstem"           # repo carries its own payload
    elif [ -f "$HOME/.brainstem/src/rapp_brainstem/brainstem.py" ]; then
        echo "$HOME/.brainstem/src/rapp_brainstem"   # daily driver
    else
        echo ""
    fi
}

py_exe() {
    if [ -x "$HOME/.brainstem/venv/bin/python" ]; then
        echo "$HOME/.brainstem/venv/bin/python"
    else
        command -v python3
    fi
}

twin_port() {
    python3 -c "import json,sys; print(json.load(open(sys.argv[1])).get('port', 7091))" "$TWIN/twin.json"
}

case "$CMD" in
    init)
        [ -f "$TWIN/twin.json" ] && { echo "✗ $TWIN already exists — not overwriting" >&2; exit 1; }
        name=$(basename "$REPO")
        mkdir -p "$TWIN/agents" "$TWIN/memories" "$TWIN/private/agents"
        cat > "$TWIN/private/README.md" <<'EOF'
# .twin/private/ — ON-DEVICE ONLY (gitignored, never travels)

The public `.twin/` layer (soul.md, agents/, memories/) is committed and travels
with the repo. This private layer stays on THIS machine and is layered on top at
launch:
- `private/soul.md`      → appended to the public soul (sensitive knowledge, real names, secrets)
- `private/agents/*.py`  → private specialist agents (override public on same filename)
- runtime (engine/, memory data, logs) lands here automatically
Put anything here that must NOT be exposed when the public twin travels.
EOF
        cat > "$TWIN/twin.json" <<EOF
{
  "schema": "rapp-twin/1",
  "name": "$name-twin",
  "description": "Resident twin of $name",
  "port": 7091
}
EOF
        cat > "$TWIN/soul.md" <<EOF
# $name Twin

You are the resident twin of the $name repository — its in-house expert, not
a general assistant. Learn the project, keep its memory, guard its rules.
(Edit this soul to teach the twin what this project is.)
EOF
        cat > "$TWIN/memories/README.md" <<'EOF'
Public, versioned seed knowledge. Runtime memories live in ../private/ (gitignored).
EOF
        cat > "$TWIN/agents/README.md" <<'EOF'
Drop *_agent.py files here — agents specific to THIS repo, overlaid on the
engine's agent set at launch. Find the repo via os.environ["TWIN_REPO_ROOT"].
EOF
        if ! grep -qs '^\.twin/private/' "$REPO/.gitignore"; then
            printf '\n# twin-in-residence: on-device memory stays on device\n.twin/private/\n' >> "$REPO/.gitignore"
        fi
        echo "✓ twin scaffolded at $TWIN — edit soul.md, then: twin.sh up"
        ;;

    up)
        [ -f "$TWIN/twin.json" ] || { echo "✗ no $TWIN/twin.json — run: twin.sh init" >&2; exit 1; }
        ENGINE=$(engine_dir)
        [ -n "$ENGINE" ] || { echo "✗ no brainstem engine found (repo payload or ~/.brainstem/src)" >&2; exit 1; }
        PY=$(py_exe)
        PORT=$(twin_port)

        RT="$TWIN/private/engine"
        mkdir -p "$RT"
        # Symlink engine contents (glob skips dotfiles, so .env/.copilot_token/
        # .brainstem_data are naturally excluded); agents/ handled separately.
        for item in "$ENGINE"/*; do
            base=$(basename "$item")
            [ "$base" = "agents" ] && continue
            ln -sfn "$item" "$RT/$base"
        done
        # Borrow auth read-only: a COPY, so the twin's token refresh writes to
        # its own file and never races the daily driver's.
        for tok in "$ENGINE/.copilot_token" "$HOME/.brainstem/src/rapp_brainstem/.copilot_token"; do
            if [ -f "$tok" ]; then cp "$tok" "$RT/.copilot_token"; chmod 600 "$RT/.copilot_token"; break; fi
        done

        # Agent overlay, PUBLIC then PRIVATE (later wins on same filename):
        #   engine set  +  .twin/agents/ (public, committed, TRAVELS)
        #                +  .twin/private/agents/ (on-device only, NEVER travels)
        AR="$TWIN/private/agents-runtime"
        rm -rf "$AR"; mkdir -p "$AR"
        cp "$ENGINE"/agents/*.py "$AR/" 2>/dev/null || true
        cp "$TWIN"/agents/*.py "$AR/" 2>/dev/null || true
        cp "$TWIN"/private/agents/*.py "$AR/" 2>/dev/null || true

        # Soul overlay, PUBLIC then PRIVATE: .twin/soul.md is committed and
        # travels; an optional on-device .twin/private/soul.md (sensitive project
        # knowledge, secrets, real names) is appended and never leaves the machine.
        SOUL_RT="$TWIN/private/soul-runtime.md"
        cat "$TWIN/soul.md" > "$SOUL_RT" 2>/dev/null || : > "$SOUL_RT"
        if [ -f "$TWIN/private/soul.md" ]; then
            printf '\n\n<!-- private on-device overlay (never committed) -->\n' >> "$SOUL_RT"
            cat "$TWIN/private/soul.md" >> "$SOUL_RT"
        fi

        existing=$(lsof -ti tcp:"$PORT" -sTCP:LISTEN 2>/dev/null | head -1) || true
        if [ -n "$existing" ]; then
            echo "  stopping previous twin (PID $existing)..."
            kill "$existing" 2>/dev/null || true
            sleep 1
        fi

        (
            cd "$RT"
            TWIN_REPO_ROOT="$REPO" SOUL_PATH="$SOUL_RT" AGENTS_PATH="$AR" PORT="$PORT" \
                nohup "$PY" brainstem.py > "$TWIN/private/twin.log" 2>&1 &
        )

        name=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1])).get('name','twin'))" "$TWIN/twin.json")
        for _ in $(seq 1 30); do
            if curl -sf -o /dev/null --max-time 1 "http://localhost:$PORT/health"; then
                echo "✓ $name up: http://localhost:$PORT  (log: $TWIN/private/twin.log)"
                echo "  any AI can invoke it:"
                echo "  curl -s -X POST http://localhost:$PORT/chat -H 'Content-Type: application/json' -d '{\"user_input\":\"who are you?\"}'"
                exit 0
            fi
            sleep 1
        done
        echo "✗ $name did not answer on :$PORT — see $TWIN/private/twin.log" >&2
        exit 1
        ;;

    status)
        [ -f "$TWIN/twin.json" ] || { echo "no twin here"; exit 1; }
        PORT=$(twin_port)
        echo "twin at $TWIN  port=$PORT"
        curl -sf --max-time 2 "http://localhost:$PORT/health" && echo "" || echo "  (not answering)"
        [ -f "$TWIN/private/twin.log" ] && { echo "--- log tail ---"; tail -5 "$TWIN/private/twin.log"; }
        ;;

    down)
        [ -f "$TWIN/twin.json" ] || { echo "no twin here"; exit 1; }
        PORT=$(twin_port)
        pid=$(lsof -ti tcp:"$PORT" -sTCP:LISTEN 2>/dev/null | head -1) || true
        if [ -n "$pid" ]; then kill "$pid" && echo "✓ twin stopped (PID $pid)"
        else echo "twin is not running"; fi
        ;;

    *)
        echo "usage: twin.sh {init|up|status|down} [repo-path]" >&2
        exit 1
        ;;
esac
