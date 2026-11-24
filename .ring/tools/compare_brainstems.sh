#!/usr/bin/env bash
# compare_brainstems.sh — run ONE identical check list on the Grail and every ring and
# print one JSON line per Brainstem (kernel climb rule 4). Read-only on every repo:
# shallow clones into a scratch folder, sandboxed installs only, nothing pushed.
#
# usage: compare_brainstems.sh [workdir] [--quick]
#   --quick skips the sandboxed fresh install (the slow check).
set -uo pipefail

WORK="${TMPDIR:-/tmp}/compare-brainstems"; for a in "$@"; do case "$a" in --*) ;; *) WORK="$a" ;; esac; done
QUICK=0; for a in "$@"; do [ "$a" = "--quick" ] && QUICK=1; done
PY="${PYTHON:-}"
if [ -z "$PY" ]; then
    if python3 -c "import requests, pytest, flask" 2>/dev/null; then PY=python3
    elif [ -x "$HOME/.brainstem/venv/bin/python" ]; then PY="$HOME/.brainstem/venv/bin/python"
    else echo '{"error":"need a Python with requests, pytest and flask (set PYTHON)"}'; exit 2; fi
fi
PYDIR="$(cd "$(dirname "$PY")" 2>/dev/null && pwd)"
REPOS="installer canary nightly alpha beta"
mkdir -p "$WORK"

count() { # pull "N of M" out of a results line
    sed -nE 's/.*Results: ([0-9]+)\/([0-9]+).*/\1 of \2/p; s/.*([0-9]+) passed, ([0-9]+) failed.*/\1 of \1+\2/p' | tail -1
}

for r in $REPOS; do
    d="$WORK/$r"
    if [ -d "$d/.git" ]; then git -C "$d" fetch -q --depth 1 origin main && git -C "$d" reset -q --hard origin/main
    else git clone -q --depth 1 "https://github.com/kody-w/rapp-$r" "$d"; fi
    commit=$(git -C "$d" rev-parse --short HEAD)
    version=$(cat "$d/rapp_brainstem/VERSION" 2>/dev/null)
    kernel=$(shasum -a 256 "$d/rapp_brainstem/brainstem.py" | cut -c1-12)
    unit=$(cd "$d/rapp_brainstem" && "$PY" -m pytest tests -q 2>&1 | tail -1 | sed -nE 's/^([0-9]+) passed.*/\1 passed/p; s/.*([0-9]+) failed.*/& /p' | head -1)
    inst=$(cd "$d" && PATH="$PYDIR:$PATH" bash tests/test_installer.sh 2>&1 | grep "Results:" | tail -1 | sed -nE 's/.*Results: ([0-9]+)\/([0-9]+).*/\1 of \2/p')
    plain=$(cd "$d" && bash tests/test_installer.sh 2>&1 | grep "Results:" | tail -1 | sed -nE 's/.*Results: ([0-9]+)\/([0-9]+).*/\1 of \2/p')
    fresh="skipped"
    if [ "$QUICK" = 0 ]; then
        fresh=$(cd "$d" && PATH="$PYDIR:$PATH" bash tests/preflight_local.sh fresh 2>&1 | sed -nE 's/.*preflight result: ([0-9]+) passed, ([0-9]+) failed.*/\1 passed \2 failed/p' | tail -1)
    fi
    printf '{"brainstem":"%s","commit":"%s","version":"%s","kernel":"%s","unit":"%s","installer":"%s","installer_plain_python3":"%s","fresh":"%s"}\n' \
        "$r" "$commit" "$version" "$kernel" "${unit:-none}" "${inst:-none}" "${plain:-none}" "${fresh:-none}"
done
