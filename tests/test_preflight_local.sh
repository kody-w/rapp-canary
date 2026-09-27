#!/bin/bash
# Offline regression: no installer, listener, network request, or process signal.
# Run: bash tests/test_preflight_local.sh [path/to/preflight_local.sh]

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PREFLIGHT="${1:-$REPO_ROOT/tests/preflight_local.sh}"
TEST_ROOT="$(mktemp -d "$REPO_ROOT/tests/.preflight-test-XXXXXX")"
PASS=0
FAIL=0

cleanup_tests() {
    case "$TEST_ROOT" in
        "$REPO_ROOT"/tests/.preflight-test-??????) rm -rf "$TEST_ROOT" ;;
        *) echo "Refusing to remove unexpected test directory: $TEST_ROOT" >&2 ;;
    esac
}
trap cleanup_tests EXIT

mkdir -p "$TEST_ROOT/bin" "$TEST_ROOT/python"
cat > "$TEST_ROOT/python/socket.py" <<'PY'
import errno
import os

AF_INET = 2
SOCK_STREAM = 1


class socket:
    def __init__(self, family, kind):
        assert (family, kind) == (AF_INET, SOCK_STREAM)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def bind(self, address):
        host, port = address
        assert host == "127.0.0.1", address
        with open(os.environ["CASE_DIR"] + "/trace", "a") as trace:
            trace.write(f"bind {host} {port}\n")
        if os.environ["CASE_MODE"] == "occupied":
            raise OSError(errno.EADDRINUSE, "Address already in use")
        if os.environ["CASE_MODE"] == "port-unavailable":
            raise OSError(errno.EACCES, "Permission denied")
        self.port = port or int(os.environ["AUTO_PORT"])

    def getsockname(self):
        return "127.0.0.1", self.port
PY

# Absolute-command functions intercept the preflight's intentional PATH bypasses.
# The fake script executable is the only launched child; sleep waits for it to
# finish, while jobs/kill report entirely synthetic candidate process state.
cat > "$TEST_ROOT/stubs.sh" <<'SH'
trace() { printf '%s\n' "$*" >> "$CASE_DIR/trace"; }

mktemp() {
    trace setup
    command mkdir -p "$CASE_DIR/sandbox"
    printf '%s\n' "$CASE_DIR/sandbox"
}

git() {
    trace "git $*"
    case "$*" in
        *"config --global"*)
            [ "$GIT_CONFIG_GLOBAL" = "$CASE_DIR/sandbox/home/.gitconfig" ] || exit 90
            [ "$HOME" = "$CASE_DIR/sandbox/home" ] || exit 91
            ;;
        *"rev-parse"*) printf '%s\n' synthetic-commit ;;
        "clone --quiet "*)
            command mkdir -p "$CASE_DIR/sandbox/home/.brainstem/src/rapp_brainstem/agents"
            printf '9.8.7\n' > "$CASE_DIR/sandbox/home/.brainstem/src/rapp_brainstem/VERSION"
            ;;
    esac
}

uname() { printf '%s\n' "$FAKE_OS"; }

sleep() {
    trace sleep
    if [ -n "${SERVER_PID:-}" ]; then
        wait "$SERVER_PID" || true
    fi
}

jobs() {
    [ "$*" = "-pr" ] || exit 92
    trace "jobs $SERVER_PID"
    case "$CASE_MODE" in
        dead|dead-empty) return 0 ;;
        not-owned) printf '99999998\n'; return 0 ;;
    esac
    [ ! -e "$CASE_DIR/dead" ] || return 0
    printf '%s\n' "$SERVER_PID"
}

kill() {
    if [ "$1" = "-0" ]; then
        trace "alive $2"
        [ "$2" = "$SERVER_PID" ] || exit 95
        case "$CASE_MODE" in dead|dead-empty|pid-gone) return 1 ;; esac
        [ ! -e "$CASE_DIR/dead" ]
    else
        trace "signal $*"
        [ "$CASE_MODE" != cleanup-failure ]
    fi
}

/usr/sbin/lsof() {
    trace "unknown-listener $*"
    printf '99999999\n'
}

xargs() {
    [ "$*" = "kill" ] || exit 93
    while read -r pid; do
        kill "$pid"
    done
}

fake_curl() {
    local url="" output="" data="" body=""
    trace "curl-args $*"
    while [ "$#" -gt 0 ]; do
        case "$1" in
            http://*) url="$1" ;;
            -o) output="$2"; shift ;;
            -d) data="$2"; shift ;;
        esac
        shift
    done
    trace "curl $url"
    case "$url" in
        http://localhost:*/health|http://127.0.0.1:*/health)
            case "$CASE_MODE" in
                timeout) return 28 ;;
                death-on-failure) touch "$CASE_DIR/dead"; return 7 ;;
                death-on-health) touch "$CASE_DIR/dead" ;;
                truncate-log) : > "$CASE_DIR/sandbox/install.log" ;;
            esac
            body='{"status":"unauthenticated","version":"9.8.7","agents":["ContextMemory","PreflightCustom"]}'
            case "$CASE_MODE" in
                wrong-version) body='{"status":"ok","version":"0.0.0","agents":["ContextMemory"]}' ;;
                missing-agent) body='{"status":"ok","version":"9.8.7","agents":[]}' ;;
            esac
            ;;
        http://localhost:*/models|http://127.0.0.1:*/models) body='{"models":[]}' ;;
        http://localhost:*/chat|http://127.0.0.1:*/chat)
            if [ "$data" = '{}' ]; then
                body='{"error":"user_input required"}'
            else
                body='{"response":"pong","model":"synthetic"}'
            fi
            ;;
        http://localhost:*/|http://127.0.0.1:*/) body='RAPP Brainstem' ;;
        *) echo "Unexpected request blocked: $url" >&2; exit 94 ;;
    esac
    if [ -n "$output" ]; then
        printf '%s\n' "$body" > "$output"
    else
        printf '%s\n' "$body"
    fi
    if [ "$CASE_MODE" = "death-after-health" ] && [[ "$url" = */models ]]; then
        touch "$CASE_DIR/dead"
    fi
}

/usr/bin/curl() { fake_curl "$@"; }
curl() { fake_curl "$@"; }
SH

cat > "$TEST_ROOT/bin/script" <<'SH'
#!/bin/bash
set -euo pipefail
printf 'launch %s\n' "$*" >> "$CASE_DIR/trace"
case "$1" in
    -q|-qF) log="$2" ;;
    -qec|-qefc) log="$3" ;;
    *) exit 96 ;;
esac
case "$CASE_MODE" in
    empty|dead-empty) : > "$log" ;;
    missing-log) ;;
    buffered)
        case "$1" in
            -qF|-qefc) printf 'Flushed synthetic installer output\n' > "$log" ;;
            *) : > "$log" ;;
        esac
        ;;
    *) printf 'Synthetic installer output\n' > "$log" ;;
esac
mkdir -p "$HOME/.brainstem/venv/bin" "$HOME/.brainstem/src/rapp_brainstem"
printf '9.8.7\n' > "$HOME/.brainstem/src/rapp_brainstem/VERSION"
cat > "$HOME/.brainstem/venv/bin/python" <<'PY'
#!/bin/bash
printf 'pytest %s\n' "$*" >> "$CASE_DIR/trace"
printf '1 passed (synthetic installed suite)\n'
PY
chmod +x "$HOME/.brainstem/venv/bin/python"
SH
chmod +x "$TEST_ROOT/bin/script"

check() {
    local name="$1"
    shift
    if "$@"; then
        PASS=$((PASS + 1))
        printf '  PASS %s\n' "$name"
    else
        FAIL=$((FAIL + 1))
        printf '  FAIL %s\n' "$name"
        sed -n '1,30p' "$CASE_DIR/output"
    fi
}

run_case() {
    local name="$1" mode="$2" port="$3" os="${4:-Darwin}"
    shift 4
    CASE_DIR="$TEST_ROOT/$name"
    mkdir -p "$CASE_DIR/repo/tests" "$CASE_DIR/repo/rapp_brainstem" \
        "$CASE_DIR/user-home/.brainstem/src/rapp_brainstem" "$CASE_DIR/user-config"
    cp "$PREFLIGHT" "$CASE_DIR/repo/tests/preflight_local.sh"
    printf '9.8.7\n' > "$CASE_DIR/repo/rapp_brainstem/VERSION"
    printf 'untouched\n' > "$CASE_DIR/user-config/gitconfig"
    : > "$CASE_DIR/trace"
    if [ "$name" = "upgrade-auth" ]; then
        printf 'synthetic-token-not-a-credential\n' \
            > "$CASE_DIR/user-home/.brainstem/src/rapp_brainstem/.copilot_token"
    fi
    if (
        export CASE_DIR CASE_MODE="$mode" AUTO_PORT=49152 FAKE_OS="$os"
        export HOME="$CASE_DIR/user-home" XDG_CONFIG_HOME="$CASE_DIR/user-config"
        export GIT_CONFIG_GLOBAL="$CASE_DIR/user-config/gitconfig"
        export BASH_ENV="$TEST_ROOT/stubs.sh" PYTHONPATH="$TEST_ROOT/python"
        export PATH="$TEST_ROOT/bin:$PATH"
        unset GITHUB_TOKEN GH_TOKEN
        if [ "$port" = unset ]; then unset PREFLIGHT_PORT; else export PREFLIGHT_PORT="$port"; fi
        if [ "$name" = auto-second ]; then export AUTO_PORT=49153; fi
        bash "$CASE_DIR/repo/tests/preflight_local.sh" "$@"
    ) > "$CASE_DIR/output" 2>&1; then
        STATUS=0
    else
        STATUS=$?
    fi
}

no_endpoints() { ! grep -q '^curl ' "$CASE_DIR/trace"; }
no_setup() { ! grep -Eq '^(setup|git |launch |curl |signal )' "$CASE_DIR/trace"; }
failed_before_endpoints() { [ "$STATUS" -ne 0 ] && no_endpoints; }
owned_cleanup() {
    local pid
    pid="$(sed -n 's/^jobs //p' "$CASE_DIR/trace" | head -1)"
    [ -n "$pid" ] && grep -qx "signal $pid" "$CASE_DIR/trace" \
        && [ "$(grep -c '^signal ' "$CASE_DIR/trace")" -eq 1 ] \
        && ! grep -q '^unknown-listener ' "$CASE_DIR/trace"
}
health_identity_checked() {
    python3 - "$CASE_DIR/trace" <<'PY'
import pathlib
import sys

events = [
    line for line in pathlib.Path(sys.argv[1]).read_text().splitlines()
    if line.startswith(("jobs ", "alive ", "curl "))
]
index = events.index("curl http://127.0.0.1:50123/health")
before, after = events[index - 2:index], events[index + 1:index + 3]
assert len(before) == 2 and before[0].startswith("jobs "), before
pid = before[0].split()[1]
assert before == after == [f"jobs {pid}", f"alive {pid}"], (before, after)
PY
}

echo "=== Offline local preflight regression ==="

run_case occupied occupied 50123 Darwin fresh
check "occupied explicit port fails before setup" test "$STATUS" -ne 0
check "occupied port is reported clearly" grep -qi 'port.*50123.*in use' "$CASE_DIR/output"
check "occupied port causes no setup or process signals" no_setup

run_case port-unavailable port-unavailable 50123 Darwin fresh
check "port probe errors fail closed" test "$STATUS" -ne 0
check "port probe errors cause no setup" no_setup

for port in '' 0 65536 nope -1; do
    run_case "invalid-${port:-empty}" healthy "$port" Darwin fresh
    check "invalid explicit port '$port' fails before setup" no_setup
    check "invalid explicit port '$port' is rejected" test "$STATUS" -ne 0
done

for name in auto-first auto-second; do
    run_case "$name" healthy unset Darwin fresh
    check "$name succeeds" test "$STATUS" -eq 0
    check "$name asks the kernel for a free loopback port" grep -qx 'bind 127.0.0.1 0' "$CASE_DIR/trace"
    if [ "$name" = auto-first ]; then port=49152; else port=49153; fi
    check "$name uses the selected port" grep -qx "curl http://127.0.0.1:$port/health" "$CASE_DIR/trace"
done

for mode in dead dead-empty empty missing-log not-owned pid-gone; do
    run_case "$mode" "$mode" 50123 Darwin fresh
    check "$mode candidate cannot reach endpoint assertions" failed_before_endpoints
    check "$mode never signals an unknown port owner" test "$(grep -c '^unknown-listener ' "$CASE_DIR/trace" || true)" -eq 0
    case "$mode" in
        dead|dead-empty|not-owned|pid-gone)
            check "$mode candidate is not signaled" test "$(grep -c '^signal ' "$CASE_DIR/trace" || true)" -eq 0
            ;;
        empty|missing-log)
            check "$mode has an explicit log failure" grep -q 'installer log is empty or missing' "$CASE_DIR/output"
            ;;
    esac
done

for mode in death-on-health truncate-log death-on-failure timeout death-after-health wrong-version missing-agent; do
    run_case "$mode" "$mode" 50123 Darwin fresh
    check "$mode fails preflight" test "$STATUS" -ne 0
    if [ "$mode" = death-on-health ] || [ "$mode" = truncate-log ]; then
        check "$mode stops before non-health endpoints" test "$(grep -c '^curl ' "$CASE_DIR/trace")" -eq 1
    fi
    if [ "$mode" = timeout ]; then
        check "timeout is bounded to 60 health attempts" test "$(grep -c '^curl ' "$CASE_DIR/trace")" -eq 60
        check "each health request has a two-second timeout" test "$(grep -c 'curl-args .*--max-time 2 ' "$CASE_DIR/trace")" -eq 60
        check "timeout cleans up only its wrapper" owned_cleanup
    fi
done

for os in Darwin Linux; do
    run_case "healthy-$os" buffered 50123 "$os" fresh
    check "$os healthy candidate passes all checks" test "$STATUS" -eq 0
    check "$os exact wrapper liveness brackets health" health_identity_checked
    check "$os cleanup signals only its tracked wrapper" owned_cleanup
    check "$os sandbox is retained" test -s "$CASE_DIR/sandbox/install.log"
    check "$os leaves user global config untouched" grep -qx untouched "$CASE_DIR/user-config/gitconfig"
    check "$os probes bypass proxies" test "$(grep -c 'curl-args --noproxy \* ' "$CASE_DIR/trace")" -eq 4
done

run_case cleanup-failure cleanup-failure 50123 Darwin fresh
check "cleanup signal failure is not silently ignored" test "$STATUS" -ne 0
check "cleanup signal failure is reported" grep -q 'could not stop installer wrapper' "$CASE_DIR/output"
check "cleanup signal failure never falls back to a port owner" owned_cleanup

run_case upgrade-auth healthy 50123 Linux upgrade --auth
check "upgrade and synthetic optional auth preserve existing contracts" test "$STATUS" -eq 0
check "upgrade retains the custom agent" test -f "$CASE_DIR/sandbox/home/.brainstem/src/rapp_brainstem/agents/preflight_custom_agent.py"
check "auth token copy stays in the sandbox" cmp -s \
    "$CASE_DIR/user-home/.brainstem/src/rapp_brainstem/.copilot_token" \
    "$CASE_DIR/sandbox/home/.brainstem/src/rapp_brainstem/.copilot_token"

echo "=== Results: $PASS passed, $FAIL failed ==="
[ "$FAIL" -eq 0 ]
