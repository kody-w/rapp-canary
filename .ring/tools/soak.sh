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
#   soak.sh evidence --beta-commit <sha> --qualification-run <id>
#                    --model-id <id> --output <path>
set -euo pipefail

SOAK_HOME="${SOAK_HOME:-$HOME/.brainstem-soak}"
SOAK_PORT="${SOAK_PORT:-7073}"
SOAK_PROBE_INTERVAL="${SOAK_PROBE_INTERVAL:-60}"
RING_REPO="${RING_REPO:-https://github.com/kody-w/rapp-canary.git}"
SOAK_REF="${SOAK_REF:-main}"
TOKEN_SOURCE="${TOKEN_SOURCE:-}"
HERE="$(cd "$(dirname "$0")" && pwd)"
RING_DIR="$(dirname "$HERE")"

say() { echo "[soak] $1"; }
die() { echo "[soak] ✗ $1" >&2; exit 1; }

pid_matches() {
    local pid="$1" needle="$2" command
    [[ "$pid" =~ ^[0-9]+$ ]] || return 1
    kill -0 "$pid" 2>/dev/null || return 1
    command=$(ps -p "$pid" -o command= 2>/dev/null) || return 1
    case "$command" in
        *"$needle"*) return 0 ;;
        *) return 1 ;;
    esac
}

pid_alive() {
    [ -f "$SOAK_HOME/soak.pid" ] || return 1
    pid_matches \
        "$(cat "$SOAK_HOME/soak.pid")" \
        "$SOAK_HOME/render/rapp_brainstem/brainstem.py"
}

stop_owned_process() {
    local pid_file="$1" needle="$2" label="$3" pid
    [ -f "$pid_file" ] || return 0
    pid=$(cat "$pid_file")
    [[ "$pid" =~ ^[0-9]+$ ]] || {
        echo "[soak] ✗ invalid $label pid file; refusing cleanup" >&2
        return 1
    }
    if ! kill -0 "$pid" 2>/dev/null; then
        rm -f "$pid_file"
        return 0
    fi
    pid_matches "$pid" "$needle" || {
        echo "[soak] ✗ $label pid $pid belongs to another process; refusing to kill it" >&2
        return 1
    }
    kill "$pid"
    for _ in $(seq 1 50); do
        if ! kill -0 "$pid" 2>/dev/null; then
            wait "$pid" 2>/dev/null || true
            rm -f "$pid_file"
            return 0
        fi
        sleep 0.1
    done
    echo "[soak] ✗ $label pid $pid did not exit; pid file retained" >&2
    return 1
}

do_stop() {
    local server_pid=""
    [ ! -f "$SOAK_HOME/soak.pid" ] \
        || server_pid=$(cat "$SOAK_HOME/soak.pid")
    stop_owned_process \
        "$SOAK_HOME/monitor.pid" \
        "rapp-soak-monitor:$SOAK_HOME" \
        "monitor" || return 1
    if [ -n "$server_pid" ]; then
        stop_owned_process \
            "$SOAK_HOME/soak.pid" \
            "$SOAK_HOME/render/rapp_brainstem/brainstem.py" \
            "server" || return 1
        say "stopped pid $server_pid"
    else
        say "not running"
    fi
}

do_start() {
    local auth="${1:-auth}"
    if [ -f "$SOAK_HOME/soak.pid" ]; then
        if pid_alive; then
            die "already running (pid $(cat "$SOAK_HOME/soak.pid")) — use refresh"
        fi
        if kill -0 "$(cat "$SOAK_HOME/soak.pid")" 2>/dev/null; then
            die "stale soak pid belongs to another process; refusing to overwrite it"
        fi
        rm -f "$SOAK_HOME/soak.pid"
    fi
    [[ "$SOAK_PROBE_INTERVAL" =~ ^[0-9]+$ ]] \
        && [ "$SOAK_PROBE_INTERVAL" -ge 15 ] \
        && [ "$SOAK_PROBE_INTERVAL" -le 300 ] \
        || die "SOAK_PROBE_INTERVAL must be 15-300 seconds"
    local state="$SOAK_HOME/state"
    mkdir -p "$SOAK_HOME" "$state"

    say "cloning Canary ref $SOAK_REF"
    rm -rf "$SOAK_HOME/src"
    git clone --quiet "$RING_REPO" "$SOAK_HOME/src"
    git -C "$SOAK_HOME/src" fetch --quiet origin "$SOAK_REF"
    git -C "$SOAK_HOME/src" checkout --quiet --detach FETCH_HEAD
    local sha
    sha="$(git -C "$SOAK_HOME/src" rev-parse HEAD)"

    say "rendering ring identity"
    rm -rf "$SOAK_HOME/render"
    python3 -I "$SOAK_HOME/src/.ring/tools/render_ring.py" \
        --repo "$SOAK_HOME/src" \
        --config "$SOAK_HOME/src/.ring/ring.json" \
        --output "$SOAK_HOME/render" \
        --report "$SOAK_HOME/render.json"

    say "creating a clean candidate-specific venv"
    rm -rf "$SOAK_HOME/venv"
    python3 -I -m venv "$SOAK_HOME/venv"
    "$SOAK_HOME/venv/bin/python" -I -m pip install --quiet \
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
    local started_at
    started_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    rm -f "$SOAK_HOME/session.json" "$SOAK_HOME/probes.jsonl" \
        "$SOAK_HOME/monitor.pid"
    (
        cd "$SOAK_HOME/render/rapp_brainstem"
        HOME="$SOAK_HOME" BRAINSTEM_STATE_DIR="$state" PORT="$SOAK_PORT" \
            nohup "$SOAK_HOME/venv/bin/python" "$SOAK_HOME/render/rapp_brainstem/brainstem.py" \
            > "$SOAK_HOME/soak.log" 2>&1 &
        echo $! > "$SOAK_HOME/soak.pid"
    )
    trap 'status=$?; if [ "$status" -ne 0 ]; then
        stop_owned_process "$SOAK_HOME/monitor.pid" "rapp-soak-monitor:$SOAK_HOME" "monitor" || true
        stop_owned_process "$SOAK_HOME/soak.pid" "$SOAK_HOME/render/rapp_brainstem/brainstem.py" "server" || true
    fi' EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM
    echo "$sha $started_at" > "$SOAK_HOME/soaking-since"

    for _ in $(seq 1 20); do
        sleep 1
        if curl -fsS "http://localhost:$SOAK_PORT/health" \
            -o "$SOAK_HOME/start-health.json" 2>/dev/null; then
            local start_chat=""
            if [ "$auth" = "auth" ]; then
                start_chat="$SOAK_HOME/start-chat.json"
                curl -fsS -X POST "http://localhost:$SOAK_PORT/chat" \
                    -H 'Content-Type: application/json' \
                    -d '{"user_input":"Reply with exactly the single word: pong"}' \
                    --max-time 120 \
                    -o "$start_chat" \
                    || die "authenticated start probe failed"
                curl -fsS "http://localhost:$SOAK_PORT/health" \
                    -o "$SOAK_HOME/start-health.json" \
                    || die "post-auth health probe failed"
            fi
            local first_probe_at
            first_probe_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
            python3 -I - \
                "$SOAK_HOME/start-health.json" "$start_chat" "$auth" \
                "$sha" "$started_at" "$SOAK_PROBE_INTERVAL" \
                "$first_probe_at" "$SOAK_HOME/session.json" \
                "$SOAK_HOME/probes.jsonl" <<'PY'
import json
import pathlib
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    health = json.load(handle)
mode = sys.argv[3]
if mode == "auth":
    with open(sys.argv[2], encoding="utf-8") as handle:
        chat = json.load(handle)
    if health.get("status") != "ok" or not chat.get("response"):
        raise SystemExit("authenticated start probes did not pass")
    if chat.get("model") != health.get("model"):
        raise SystemExit("start chat and health used different models")
else:
    if health.get("status") != "unauthenticated":
        raise SystemExit("unauthenticated soak unexpectedly found credentials")
session = {
    "auth_mode": "authenticated" if mode == "auth" else "unauthenticated",
    "canary_commit": sys.argv[4],
    "started_at": sys.argv[5],
    "model_id": health.get("model"),
    "probe_interval_seconds": int(sys.argv[6]),
    "authenticated_chat_times": [sys.argv[7]] if mode == "auth" else [],
}
pathlib.Path(sys.argv[8]).write_text(
    json.dumps(session, sort_keys=True) + "\n",
    encoding="utf-8",
)
pathlib.Path(sys.argv[9]).write_text(
    json.dumps(
        {
            "at": sys.argv[7],
            "status": health.get("status"),
            "model_id": health.get("model"),
        },
        sort_keys=True,
    )
    + "\n",
    encoding="utf-8",
)
PY
            rm -f "$SOAK_HOME/start-chat.json"
            python3 -I -c '
import json
import os
import pathlib
import sys
import time
import urllib.request
from datetime import datetime, timezone

server_pid = int(sys.argv[2])
url = sys.argv[3]
interval = int(sys.argv[4])
output = pathlib.Path(sys.argv[5])
while True:
    try:
        os.kill(server_pid, 0)
    except OSError:
        break
    time.sleep(interval)
    record = {
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "status": "unavailable",
        "model_id": None,
    }
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            health = json.load(response)
        record["status"] = health.get("status")
        record["model_id"] = health.get("model")
    except Exception:
        pass
    with output.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")
' "rapp-soak-monitor:$SOAK_HOME" \
                "$(cat "$SOAK_HOME/soak.pid")" \
                "http://localhost:$SOAK_PORT/health" \
                "$SOAK_PROBE_INTERVAL" \
                "$SOAK_HOME/probes.jsonl" >/dev/null 2>&1 &
            echo $! > "$SOAK_HOME/monitor.pid"
            trap - EXIT INT TERM
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
    curl -fsS "http://localhost:$SOAK_PORT/health" | python3 -I -m json.tool | sed 's/^/[soak]   /' || die "/health failed"
    say "log tail:"
    tail -5 "$SOAK_HOME/soak.log" | sed 's/^/[soak]   /'
}

do_evidence() {
    local beta_commit="" qualification_run="" model_id="" output=""
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --beta-commit) beta_commit="${2:-}"; shift 2 ;;
            --qualification-run) qualification_run="${2:-}"; shift 2 ;;
            --model-id) model_id="${2:-}"; shift 2 ;;
            --output) output="${2:-}"; shift 2 ;;
            *) die "unknown evidence option: $1" ;;
        esac
    done
    [[ "$beta_commit" =~ ^[0-9a-f]{40}$ ]] || die "beta commit must be a full SHA"
    [[ "$qualification_run" =~ ^[0-9]+$ ]] || die "qualification run must be numeric"
    [[ "$model_id" =~ ^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$ ]] \
        && [ "$(printf '%s' "$model_id" | tr '[:upper:]' '[:lower:]')" != "auto" ] \
        || die "model id must be explicit"
    [ -n "$output" ] || die "evidence output path is required"
    pid_alive || die "soak is not running"
    [ -f "$SOAK_HOME/soaking-since" ] || die "soak start record is missing"
    [ -f "$SOAK_HOME/session.json" ] || die "soak session record is missing"
    [ -f "$SOAK_HOME/probes.jsonl" ] || die "soak probe history is missing"

    local auth_mode
    auth_mode=$(python3 -I -c \
        'import json,sys; print(json.load(open(sys.argv[1]))["auth_mode"])' \
        "$SOAK_HOME/session.json")
    [ "$auth_mode" = "authenticated" ] \
        || die "real-auth evidence cannot be created from a --no-auth soak"

    local canary_commit started_at completed_at minimum_minutes minimum_seconds elapsed
    read -r canary_commit started_at < "$SOAK_HOME/soaking-since"
    [[ "$canary_commit" =~ ^[0-9a-f]{40}$ ]] || die "invalid soak Canary commit"
    completed_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    minimum_minutes=$(python3 -I - "$RING_DIR/preprod-policy.json" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    print(json.load(handle)["minimum_soak_minutes"])
PY
)
    [[ "$minimum_minutes" =~ ^[0-9]+$ ]] || die "invalid soak duration policy"
    minimum_seconds=$((minimum_minutes * 60))
    elapsed=$(python3 -I - "$started_at" "$completed_at" <<'PY'
from datetime import datetime
import sys

start = datetime.fromisoformat(sys.argv[1].replace("Z", "+00:00"))
end = datetime.fromisoformat(sys.argv[2].replace("Z", "+00:00"))
print(int((end - start).total_seconds()))
PY
)
    [ "$elapsed" -ge "$minimum_seconds" ] \
        || die "soak must run at least $minimum_minutes minutes before evidence is recorded"

    local health="$SOAK_HOME/evidence-health.json"
    local chat="$SOAK_HOME/evidence-chat.json"
    if [ -f "$SOAK_HOME/monitor.pid" ]; then
        stop_owned_process \
            "$SOAK_HOME/monitor.pid" \
            "rapp-soak-monitor:$SOAK_HOME" \
            "monitor" || die "could not stop the verified soak monitor"
    fi
    curl -fsS "http://localhost:$SOAK_PORT/health" -o "$health" \
        || die "cannot record evidence: /health failed"
    curl -fsS -X POST "http://localhost:$SOAK_PORT/chat" \
        -H 'Content-Type: application/json' \
        -d '{"user_input":"Reply with exactly the single word: pong"}' \
        --max-time 120 \
        -o "$chat" || die "cannot record evidence: authenticated /chat failed"
    if grep -Eiq 'Traceback|CRITICAL|segmentation fault' "$SOAK_HOME/soak.log"; then
        die "cannot record evidence: critical event found in soak log"
    fi

    mkdir -p "$(dirname "$output")"
    python3 -I - \
        "$health" "$chat" "$SOAK_HOME/session.json" "$SOAK_HOME/probes.jsonl" \
        "$model_id" "$SOAK_HOME/render/rapp_brainstem" \
        "$canary_commit" "$beta_commit" "$qualification_run" \
        "$started_at" "$completed_at" "$output" <<'PY'
import json
import os
import pathlib
import sys
from datetime import datetime

with open(sys.argv[1], encoding="utf-8") as handle:
    health = json.load(handle)
with open(sys.argv[2], encoding="utf-8") as handle:
    chat = json.load(handle)
with open(sys.argv[3], encoding="utf-8") as handle:
    session = json.load(handle)
probes = [
    json.loads(line)
    for line in pathlib.Path(sys.argv[4]).read_text(encoding="utf-8").splitlines()
    if line
]
model_id = sys.argv[5]
expected_brainstem = os.path.realpath(sys.argv[6])
if health.get("status") != "ok":
    raise SystemExit(f"health is not authenticated and healthy: {health}")
if os.path.realpath(health.get("brainstem_dir", "")) != expected_brainstem:
    raise SystemExit("soak is not serving the isolated rendered checkout")
if not chat.get("response"):
    raise SystemExit(f"authenticated chat returned no response: {chat}")
if chat.get("model") != model_id or health.get("model") != model_id:
    raise SystemExit(
        f"soak model mismatch: health={health.get('model')} chat={chat.get('model')}"
    )
if (
    session.get("auth_mode") != "authenticated"
    or session.get("canary_commit") != sys.argv[7]
    or session.get("started_at") != sys.argv[10]
    or session.get("model_id") != model_id
):
    raise SystemExit("soak session is not bound to this authenticated interval")
interval = session.get("probe_interval_seconds")
if not isinstance(interval, int) or not 15 <= interval <= 300:
    raise SystemExit("invalid soak probe interval")
probes.append(
    {
        "at": sys.argv[11],
        "status": health.get("status"),
        "model_id": health.get("model"),
    }
)
with pathlib.Path(sys.argv[4]).open("a", encoding="utf-8") as handle:
    handle.write(json.dumps(probes[-1], sort_keys=True) + "\n")
start = datetime.fromisoformat(sys.argv[10].replace("Z", "+00:00"))
end = datetime.fromisoformat(sys.argv[11].replace("Z", "+00:00"))
minimum_probes = max(2, int((end - start).total_seconds()) // interval)
if len(probes) < minimum_probes:
    raise SystemExit(
        f"soak probe history is incomplete: {len(probes)} < {minimum_probes}"
    )
if any(
    probe.get("status") != "ok" or probe.get("model_id") != model_id
    for probe in probes
):
    raise SystemExit("soak probe history contains an unhealthy interval")
chat_times = session.get("authenticated_chat_times")
if not isinstance(chat_times, list) or len(chat_times) != 1:
    raise SystemExit("authenticated start probe record is invalid")
chat_times.append(sys.argv[11])
value = {
    "schema": "rapp/1:soak",
    "result": "passed",
    "canary_commit": sys.argv[7],
    "beta_commit": sys.argv[8],
    "qualification_run_id": sys.argv[9],
    "model_id": model_id,
    "started_at": sys.argv[10],
    "completed_at": sys.argv[11],
    "probe_interval_seconds": interval,
    "health_probe_count": len(probes),
    "authenticated_chat_count": 2,
    "authenticated_chat_times": chat_times,
    "probes": probes,
    "checks": {
        "authenticated_chat": True,
        "state_isolated": True,
        "health_stable": True,
        "no_critical_events": True,
    },
}
target = pathlib.Path(sys.argv[12])
temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
temporary.write_text(
    json.dumps(value, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)
os.replace(temporary, target)
PY
    say "✓ wrote candidate-bound soak evidence to $output"
    say "commit this file under .ring/soak before staging Preprod"
}

case "${1:-}" in
    start)   do_start "$([ "${2:-}" = "--no-auth" ] && echo no-auth || echo auth)" ;;
    stop)    do_stop ;;
    status)  do_status ;;
    refresh) do_stop; do_start "$([ "${2:-}" = "--no-auth" ] && echo no-auth || echo auth)" ;;
    evidence) shift; do_evidence "$@" ;;
    *) echo "usage: soak.sh start|status|stop|refresh [--no-auth] | evidence ..." >&2; exit 2 ;;
esac
