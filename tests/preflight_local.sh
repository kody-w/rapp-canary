#!/bin/bash
# Local preflight — install the CURRENT CHECKOUT "in the wild" without touching
# your real ~/.brainstem or the server running on port 7071.
#
#   bash tests/preflight_local.sh [fresh|upgrade] [--auth]
#
#   fresh    (default) factory-machine install of this checkout via the real install.sh
#   upgrade  seed a real production-main install first, then upgrade to this checkout
#   --auth   copy your real Copilot token into the sandbox so /chat is tested end-to-end
#
# How it stays safe:
#   * Everything runs under a throwaway $HOME in /tmp — your real install is untouched.
#   * The installer's clone URL is redirected (git url.insteadOf) to a local bare repo
#     whose `main` ref is this checkout's HEAD. install.sh itself is NOT modified.
#   * PATH shims: `lsof` is a no-op (the installer can never kill your live server),
#     `open` is a no-op (no browser popups), and `curl` fails fast for GitHub auth
#     endpoints (exercising the graceful-degradation path, same as CI).
#   * A free loopback port is selected per run; PREFLIGHT_PORT must be free if set.
#   * Health is accepted only while this run's installer wrapper is still alive.
#   * Cleanup signals only that tracked wrapper, never an unknown port owner.
#
# See RELEASING.md for where this fits in the release process.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SCENARIO="fresh"
AUTH=false
for arg in "$@"; do
    case "$arg" in
        fresh|upgrade) SCENARIO="$arg" ;;
        --auth) AUTH=true ;;
        *) echo "usage: bash tests/preflight_local.sh [fresh|upgrade] [--auth]"; exit 2 ;;
    esac
done

# Probe before creating the sandbox or changing any git configuration.
if ! PORT="$(python3 - <<'PY'
import errno
import os
import socket
import sys

requested = os.environ.get("PREFLIGHT_PORT")
if requested is not None and (
    not requested.isascii() or not requested.isdecimal()
    or not 1 <= int(requested) <= 65535
):
    sys.exit("PREFLIGHT_PORT must be an integer between 1 and 65535.")
port = int(requested) if requested is not None else 0
try:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", port))
        print(probe.getsockname()[1])
except OSError as exc:
    if exc.errno == errno.EADDRINUSE:
        sys.exit(f"Loopback port {port} is already in use; refusing to install.")
    sys.exit(f"Cannot select loopback port {port}: {exc}")
PY
)"; then
    exit 1
fi
BASE_URL="http://127.0.0.1:$PORT"
SANDBOX="$(mktemp -d /tmp/brainstem-preflight-XXXXXX)"
FAKE_HOME="$SANDBOX/home"
BARE="$SANDBOX/fake-origin.git"
SHIMS="$SANDBOX/shims"
LOG="$SANDBOX/install.log"
SERVER_PID=""

mkdir -p "$FAKE_HOME" "$SHIMS"

# Pin git's --global scope to an explicit sandbox file. Overriding HOME alone is
# not enough: with XDG_CONFIG_HOME set and no fake ~/.gitconfig yet, git would
# write the insteadOf rewrite into the user's REAL $XDG_CONFIG_HOME/git/config.
export GIT_CONFIG_GLOBAL="$FAKE_HOME/.gitconfig"

server_is_running() {
    local pid
    [ -n "$SERVER_PID" ] || return 1
    # A live PID alone can have been reused. It must still be our running job.
    for pid in $(jobs -pr); do
        if [ "$pid" = "$SERVER_PID" ]; then
            kill -0 "$SERVER_PID" 2>/dev/null
            return $?
        fi
    done
    return 1
}

cleanup() {
    local result=$?
    if server_is_running; then
        if ! kill "$SERVER_PID" 2>/dev/null; then
            echo "  ✗ could not stop installer wrapper $SERVER_PID" >&2
            result=1
        fi
    fi
    echo ""
    echo "  Sandbox kept for inspection: $SANDBOX"
    echo "  (installer log: $LOG — rm -rf when done)"
    exit "$result"
}
trap cleanup EXIT

startup_failure() {
    echo "  ✗ $1" >&2
    if [ -s "$LOG" ]; then
        echo "  Last 40 installer log lines:" >&2
        tail -40 "$LOG" >&2
    fi
    exit 1
}

require_candidate() {
    if [ ! -s "$LOG" ]; then
        startup_failure "installer log is empty or missing — refusing endpoint responses"
    fi
    if ! server_is_running; then
        startup_failure "installer wrapper $SERVER_PID exited — refusing endpoint responses"
    fi
}

echo "═══ brainstem local preflight ═══ scenario=$SCENARIO auth=$AUTH port=$PORT"
echo "  sandbox: $SANDBOX"

# ── 1. Fake origin: bare repo whose `main` is this checkout's HEAD ────────────
git clone --quiet --bare "$REPO_ROOT" "$BARE"
# Keep Git's safe.bareRepository=explicit guard enabled for these owned fixtures.
git --git-dir="$BARE" update-ref refs/heads/main "$(git -C "$REPO_ROOT" rev-parse HEAD)"
git --git-dir="$BARE" symbolic-ref HEAD refs/heads/main
if git -C "$REPO_ROOT" rev-parse origin/main >/dev/null 2>&1; then
    git --git-dir="$BARE" update-ref refs/heads/production-baseline "$(git -C "$REPO_ROOT" rev-parse origin/main)"
fi
HOME="$FAKE_HOME" git config --global "url.file://$BARE.insteadOf" "https://github.com/kody-w/rapp-installer.git"
HOME="$FAKE_HOME" git config --global user.email preflight@localhost
HOME="$FAKE_HOME" git config --global user.name preflight

# ── 2. PATH shims ─────────────────────────────────────────────────────────────
cat > "$SHIMS/lsof" <<'EOF'
#!/bin/bash
exit 0
EOF
cat > "$SHIMS/open" <<'EOF'
#!/bin/bash
exit 0
EOF
cat > "$SHIMS/curl" <<EOF
#!/bin/bash
for a in "\$@"; do
    case "\$a" in
        *github.com/login/*|*raw.githubusercontent.com*) exit 6 ;;
    esac
done
exec /usr/bin/curl "\$@"
EOF
chmod +x "$SHIMS"/lsof "$SHIMS"/open "$SHIMS"/curl

# ── 3. Upgrade scenario: seed a real production-main install with user files ──
if [ "$SCENARIO" = "upgrade" ]; then
    if ! git --git-dir="$BARE" rev-parse production-baseline >/dev/null 2>&1; then
        echo "  ✗ no origin/main in this checkout — cannot seed the upgrade baseline"; exit 1
    fi
    git clone --quiet "$BARE" "$FAKE_HOME/.brainstem/src"
    # Cloning the bare puts its branches under origin/*, so reset to the remote-tracking ref.
    git -C "$FAKE_HOME/.brainstem/src" reset --hard --quiet origin/production-baseline
    cat > "$FAKE_HOME/.brainstem/src/rapp_brainstem/agents/preflight_custom_agent.py" <<'EOF'
from agents.basic_agent import BasicAgent
class PreflightCustomAgent(BasicAgent):
    def __init__(self):
        self.name = 'PreflightCustom'
        self.metadata = {"name": self.name, "description": "preflight marker agent",
                         "parameters": {"type": "object", "properties": {}, "required": []}}
        super().__init__(name=self.name, metadata=self.metadata)
    def perform(self, **kwargs):
        return "preflight-marker"
EOF
    printf '\nPREFLIGHT-SOUL-MARKER\n' >> "$FAKE_HOME/.brainstem/src/rapp_brainstem/soul.md"
    printf 'GITHUB_MODEL=auto\nPORT=7071\n# PREFLIGHT-ENV-MARKER\n' > "$FAKE_HOME/.brainstem/src/rapp_brainstem/.env"
    echo "  ✓ seeded production baseline ($(git -C "$FAKE_HOME/.brainstem/src" rev-parse --short HEAD)) + user files"
fi

# (The optional --auth token copy happens AFTER the install — see step 6b — because
# install.sh's fresh path re-clones $HOME/.brainstem/src, which would wipe it.)

# ── 5. Run the REAL installer inside the sandbox ─────────────────────────────
echo ""
echo "── running install.sh (log: $LOG) ──"
(
    export HOME="$FAKE_HOME"
    export PATH="$SHIMS:$PATH"
    export PORT="$PORT"          # env beats .env — server binds the sandbox port
    # `script` allocates a pty so the installer launches the server exactly as it
    # would in a user's terminal (its final exec needs a controlling tty).
    # Flush each write so buffering cannot look like an empty installer log.
    if [ "$(uname)" = "Darwin" ]; then
        exec script -qF "$LOG" bash "$REPO_ROOT/install.sh" </dev/null >/dev/null 2>&1
    else
        exec script -qefc "bash '$REPO_ROOT/install.sh'" "$LOG" </dev/null >/dev/null 2>&1
    fi
) &
SERVER_PID=$!

# ── 6. Poll for a serving brainstem, then assert the contract ────────────────
BRANCH_VERSION="$(tr -d '[:space:]' < "$REPO_ROOT/rapp_brainstem/VERSION")"
HEALTH="$SANDBOX/health.json"
up=false
for ((i=0; i<60; i++)); do
    sleep 3
    require_candidate
    if /usr/bin/curl --noproxy '*' -sf --max-time 2 "$BASE_URL/health" -o "$HEALTH" 2>/dev/null; then
        require_candidate
        up=true
        break
    fi
    require_candidate
done
if [ "$up" != true ]; then
    startup_failure "server never came up after 60 health attempts"
fi

# ── 6b. Optional: real token for an end-to-end /chat test ────────────────────
# Seeded post-install (the server reads auth lazily, per request, so this works).
if [ "$AUTH" = true ]; then
    # Token files are gitignored, so a clean checkout has none in the repo (issue #37).
    # Source from the repo working tree if present, else the REAL install at
    # ~/.brainstem/src/rapp_brainstem (where a device-code login actually persists them).
    _auth_copied=false
    for f in .copilot_token .copilot_session; do
        for src in "$REPO_ROOT/rapp_brainstem/$f" "$HOME/.brainstem/src/rapp_brainstem/$f"; do
            if [ -f "$src" ]; then
                cp "$src" "$FAKE_HOME/.brainstem/src/rapp_brainstem/$f"
                _auth_copied=true
                break
            fi
        done
    done
    if [ "$_auth_copied" = true ]; then
        echo "  ✓ copied real Copilot token into sandbox (stays inside $SANDBOX)"
    else
        echo "  ⚠ --auth requested but no .copilot_token found (repo tree or ~/.brainstem) — skipping the authenticated /chat probe"
    fi
fi

PASS=0; FAIL=0
ok()   { PASS=$((PASS+1)); echo "  ✓ $1"; }
bad()  { FAIL=$((FAIL+1)); echo "  ✗ $1"; }

python3 - "$HEALTH" "$BRANCH_VERSION" <<'EOF' && ok "health: status + candidate version + agents" || bad "health contract"
import json, sys
d = json.load(open(sys.argv[1]))
assert d.get("status") in ("ok", "unauthenticated"), d
assert d.get("version") == sys.argv[2], f'{d.get("version")} != {sys.argv[2]}'
assert "ContextMemory" in (d.get("agents") or []), d.get("agents")
EOF

# Fetch to a file, then grep — a `curl | grep -q` pipe makes grep close the pipe on
# first match, SIGPIPE-ing curl, which `set -o pipefail` then reports as a failure.
/usr/bin/curl --noproxy '*' -sf "$BASE_URL/" -o "$SANDBOX/index.html" 2>/dev/null \
    && grep -q "RAPP Brainstem" "$SANDBOX/index.html" && ok "web UI serves" || bad "web UI"
/usr/bin/curl --noproxy '*' -sf "$BASE_URL/models" >/dev/null && ok "/models responds" || bad "/models"
/usr/bin/curl --noproxy '*' -s -X POST "$BASE_URL/chat" -H 'Content-Type: application/json' -d '{}' \
    | python3 -c 'import json,sys; d=json.load(sys.stdin); assert "error" in d' \
    && ok "/chat rejects bad input as JSON" || bad "/chat error contract"

if [ "$SCENARIO" = "upgrade" ]; then
    test -f "$FAKE_HOME/.brainstem/src/rapp_brainstem/agents/preflight_custom_agent.py" \
        && ok "custom agent survived upgrade" || bad "custom agent lost in upgrade"
    grep -q "PREFLIGHT-SOUL-MARKER" "$FAKE_HOME/.brainstem/src/rapp_brainstem/soul.md" \
        && ok "soul.md survived upgrade" || bad "soul.md lost in upgrade"
    grep -q "PREFLIGHT-ENV-MARKER" "$FAKE_HOME/.brainstem/src/rapp_brainstem/.env" \
        && ok ".env survived upgrade" || bad ".env lost in upgrade"
    /usr/bin/curl --noproxy '*' -sf "$BASE_URL/health" \
        | python3 -c 'import json,sys; assert "PreflightCustom" in json.load(sys.stdin).get("agents",[])' \
        && ok "custom agent loads in upgraded server" || bad "custom agent not loaded"
    NEWVER="$(tr -d '[:space:]' < "$FAKE_HOME/.brainstem/src/rapp_brainstem/VERSION" 2>/dev/null)"
    [ "$NEWVER" = "$BRANCH_VERSION" ] && ok "upgraded to candidate v$NEWVER" || bad "version after upgrade: $NEWVER"
fi

if [ "$AUTH" = true ]; then
    RESP="$SANDBOX/chat.json"
    /usr/bin/curl --noproxy '*' -s -X POST "$BASE_URL/chat" -H 'Content-Type: application/json' \
        -d '{"user_input":"Reply with exactly the single word: pong"}' -o "$RESP" --max-time 120 || true
    python3 - "$RESP" <<'EOF' && ok "REAL /chat round-trip (authenticated)" || bad "real /chat round-trip"
import json, sys
d = json.load(open(sys.argv[1]))
assert d.get("response"), d
print("      model:", d.get("model"), "| response:", d["response"][:60])
EOF
fi

# ── 7. Run the unit suite against the INSTALLED copy ─────────────────────────
if "$FAKE_HOME/.brainstem/venv/bin/python" -m pytest --version >/dev/null 2>&1 || \
   "$FAKE_HOME/.brainstem/venv/bin/pip" install -q pytest >/dev/null 2>&1; then
    if (cd "$FAKE_HOME/.brainstem/src/rapp_brainstem" && \
        "$FAKE_HOME/.brainstem/venv/bin/python" -m pytest tests/ -q >"$SANDBOX/pytest.log" 2>&1); then
        ok "unit suite green inside the installed copy ($(tail -1 "$SANDBOX/pytest.log"))"
    else
        bad "unit suite failed inside installed copy — see $SANDBOX/pytest.log"
    fi
fi

require_candidate
echo ""
echo "═══ preflight result: $PASS passed, $FAIL failed ═══"
[ "$FAIL" -eq 0 ]
