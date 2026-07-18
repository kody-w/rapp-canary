#!/bin/bash
# device_probe.sh — ZERO-CONFIG on-device ring test. Run on ANY fresh VM or
# machine; no runner registration, no tailnet linkage, nothing to configure:
#
#   curl -sL https://raw.githubusercontent.com/kody-w/rapp-canary/main/.ring/tools/device_probe.sh \
#     | bash -s -- --ring canary --report-issue 42
#
# The device PULLS the ring, installs it with the REAL installer inside a
# throwaway sandbox HOME, runs the full test suite plus live health/chat
# asserts, writes a findings report, and (opt-in) posts it to a GitHub issue
# with `gh`. Blow the VM away afterwards — nothing outside /tmp is touched.
# An agent (Copilot CLI, Claude) needs no more context than the command above.
#
# Prereqs on the VM: bash, git, curl, python3. `gh auth login` only if reporting.
#
#   --ring canary|nightly|alpha|beta   ring to test              (default canary)
#   --ref <branch|sha>                 e.g. a flight/* branch    (default ring main)
#   --report-issue <N>                 post findings to issue N  (default: OFF)
#   --keep                             keep the sandbox for inspection
#
# SAFETY: refuses to run if port 7071 is busy — the sacred installer kills any
# existing 7071 listener (install.sh "kill any existing brainstem" step), and a
# probe must never execute that against a machine's real brainstem.
set -uo pipefail

RING=canary REF="" ISSUE="" KEEP=0
while [ $# -gt 0 ]; do case "$1" in
  --ring) RING="$2"; shift 2 ;;
  --ref) REF="$2"; shift 2 ;;
  --report-issue) ISSUE="$2"; shift 2 ;;
  --keep) KEEP=1; shift ;;
  *) echo "unknown arg: $1 (see header for usage)" >&2; exit 2 ;;
esac; done
case "$RING" in
  canary|nightly|alpha|beta) REPO="kody-w/rapp-$RING" ;;
  grail|installer) echo "grail is frozen production — probe a pre-grail ring" >&2; exit 2 ;;
  *) echo "unknown ring: $RING" >&2; exit 2 ;;
esac

for t in git curl python3; do
  command -v "$t" >/dev/null || { echo "missing prereq: $t" >&2; exit 2; }
done
if lsof -ti tcp:7071 -sTCP:LISTEN >/dev/null 2>&1 || { command -v ss >/dev/null && ss -ltn 2>/dev/null | grep -q ':7071 '; }; then
  echo "REFUSING: port 7071 is busy — a real brainstem may be running on this machine." >&2
  echo "The installer would kill it. Probe on a fresh VM, or stop the server first." >&2
  exit 3
fi

WORK=$(mktemp -d /tmp/rapp-probe.XXXXXX)
SANDBOX="$WORK/home"; SRC="$WORK/src"; LOG="$WORK/install.log"; REPORT="$WORK/report.md"
mkdir -p "$SANDBOX"
PASS=() FAIL=()
check() { # check <name> <cmd...> — record pass/fail, never abort the probe
  local name="$1"; shift
  if "$@" >>"$WORK/probe.log" 2>&1; then PASS+=("$name"); echo "  PASS $name"
  else FAIL+=("$name"); echo "  FAIL $name"; fi
}

echo "── device_probe: $REPO${REF:+ @ $REF} ──"
git clone -q "https://github.com/$REPO.git" "$SRC" || { echo "clone failed" >&2; exit 1; }
[ -n "$REF" ] && { git -C "$SRC" fetch -q origin "$REF" 2>/dev/null || true; git -C "$SRC" checkout -q "$REF" || { echo "unknown ref: $REF" >&2; exit 1; }; }
SHA=$(git -C "$SRC" rev-parse --short HEAD)
VER=$(tr -d '[:space:]' < "$SRC/rapp_brainstem/VERSION")

# Fake origin (preflight's proven trick): the UNMODIFIED installer clones the
# production URL — redirect it, inside the sandbox HOME only, at the candidate.
BARE="$WORK/fake-origin.git"
git clone -q --bare "$SRC" "$BARE"
git -C "$BARE" update-ref refs/heads/main "$(git -C "$SRC" rev-parse HEAD)"
git -C "$BARE" symbolic-ref HEAD refs/heads/main
HOME="$SANDBOX" git config --global "url.file://$BARE.insteadOf" "https://github.com/kody-w/rapp-installer.git"

echo "installing (real installer, sandboxed HOME) — this takes a few minutes…"
if [ "$(uname)" = "Darwin" ]; then
  HOME="$SANDBOX" script -q "$LOG" bash "$SRC/install.sh" >/dev/null 2>&1 &
else
  HOME="$SANDBOX" script -qec "bash $SRC/install.sh" "$LOG" >/dev/null 2>&1 &
fi

HEALTH="$WORK/health.json"; UP=0
for i in $(seq 1 120); do
  sleep 5
  curl -sf http://localhost:7071/health -o "$HEALTH" 2>/dev/null && { UP=1; break; }
done
if [ "$UP" = 1 ]; then PASS+=("server came up on :7071"); echo "  PASS server came up on :7071"
else FAIL+=("server came up on :7071"); echo "  FAIL server never came up"; fi

if [ "$UP" = 1 ]; then
  check "health contract (status/version/agents)" python3 - "$HEALTH" "$VER" <<'EOF'
import json, sys
d = json.load(open(sys.argv[1]))
assert d.get("status") in ("ok", "unauthenticated"), d
assert d.get("version") == sys.argv[2], f'server v{d.get("version")} != candidate v{sys.argv[2]}'
assert "ContextMemory" in (d.get("agents") or []), d.get("agents")
EOF
  check "web UI serves" bash -c 'curl -sf http://localhost:7071/ | grep -q "RAPP Brainstem"'
  check "/models serves" curl -sf -o /dev/null http://localhost:7071/models
  check "/chat answers JSON (never crashes)" bash -c \
    'curl -s -X POST http://localhost:7071/chat -H "Content-Type: application/json" -d "{\"user_input\":\"device probe ping\"}" | python3 -c "import json,sys; json.load(sys.stdin)"'
  VENVPY="$SANDBOX/.brainstem/venv/bin/python"
  check "unit tests (pytest, install venv)" env HOME="$SANDBOX" "$VENVPY" -m pytest "$SRC/rapp_brainstem/tests/" -q
  check "installer test suite" env HOME="$SANDBOX" PATH="$SANDBOX/.brainstem/venv/bin:$PATH" \
    bash -c "cd '$SRC' && bash tests/test_installer.sh"
fi

# Teardown the server before reporting — the VM may live on.
pkill -f "$SANDBOX" 2>/dev/null; sleep 2

STATUS="PASS"; [ ${#FAIL[@]} -gt 0 ] && STATUS="FAIL"
{
  echo "## Device probe: \`$REPO\`${REF:+ @ \`$REF\`} — **$STATUS**"
  echo
  echo "| | |"
  echo "|---|---|"
  echo "| Device | \`$(hostname)\` |"
  echo "| OS | \`$(uname -sm) $(uname -r)\` |"
  echo "| Python | \`$(python3 -V 2>&1)\` |"
  echo "| Candidate | \`$SHA\` (v$VER) |"
  echo "| When | $(date -u '+%Y-%m-%d %H:%M UTC') |"
  echo
  for p in ${PASS[@]+"${PASS[@]}"}; do echo "- ✅ $p"; done
  for f in ${FAIL[@]+"${FAIL[@]}"}; do echo "- ❌ $f"; done
  if [ "$STATUS" = "FAIL" ]; then
    echo; echo "<details><summary>install.log tail</summary>"; echo; echo '```'
    tail -40 "$LOG" 2>/dev/null; echo '```'; echo "</details>"
    echo; echo "<details><summary>probe.log tail</summary>"; echo; echo '```'
    tail -60 "$WORK/probe.log" 2>/dev/null; echo '```'; echo "</details>"
  fi
} > "$REPORT"

echo; cat "$REPORT"
if [ -n "$ISSUE" ]; then
  if command -v gh >/dev/null && gh auth status >/dev/null 2>&1; then
    gh issue comment "$ISSUE" -R "$REPO" --body-file "$REPORT" \
      && echo "reported to https://github.com/$REPO/issues/$ISSUE" \
      || echo "WARNING: issue comment failed — report is above" >&2
  else
    echo "WARNING: gh not authenticated — cannot report to issue #$ISSUE (report is above)" >&2
  fi
fi

if [ "$KEEP" = 1 ]; then echo "sandbox kept at: $WORK"
else rm -rf "$WORK"; fi
[ "$STATUS" = "PASS" ]
