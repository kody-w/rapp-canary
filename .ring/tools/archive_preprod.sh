#!/bin/bash
# Archive a sealed Preprod candidate into Canary-owned history before artifacts expire.
set -euo pipefail

HUB="kody-w/rapp-canary"
RUN_ID="${1:-}"
[ -n "$RUN_ID" ] || { echo "usage: archive_preprod.sh <run-id>" >&2; exit 2; }
[[ "$RUN_ID" =~ ^[0-9]+$ ]] || { echo "run id must be numeric" >&2; exit 2; }

HERE="$(cd "$(dirname "$0")" && pwd)"
RING_DIR="$(dirname "$HERE")"
DEST="$RING_DIR/preprod/run-$RUN_ID"

RUN=$(gh api "repos/$HUB/actions/runs/$RUN_ID")
[ "$(python3 -I -c 'import json,sys; print(json.load(sys.stdin)[\"name\"])' <<<"$RUN")" = "Stage Preprod" ] \
    || { echo "run $RUN_ID is not Stage Preprod" >&2; exit 1; }
[ "$(python3 -I -c 'import json,sys; print(json.load(sys.stdin)[\"conclusion\"])' <<<"$RUN")" = "success" ] \
    || { echo "run $RUN_ID is not green" >&2; exit 1; }
[ "$(python3 -I -c 'import json,sys; print(json.load(sys.stdin)[\"path\"])' <<<"$RUN")" = ".github/workflows/stage-preprod.yml" ] \
    || { echo "run $RUN_ID used an unexpected workflow" >&2; exit 1; }
[ "$(python3 -I -c 'import json,sys; print(json.load(sys.stdin)[\"event\"])' <<<"$RUN")" = "workflow_dispatch" ] \
    || { echo "run $RUN_ID was not manually dispatched" >&2; exit 1; }
[ "$(python3 -I -c 'import json,sys; print(json.load(sys.stdin)[\"head_branch\"])' <<<"$RUN")" = "main" ] \
    || { echo "run $RUN_ID did not execute from main" >&2; exit 1; }
[ "$(python3 -I -c 'import json,sys; print(json.load(sys.stdin)[\"repository\"][\"full_name\"])' <<<"$RUN")" = "$HUB" ] \
    || { echo "run $RUN_ID belongs to another repository" >&2; exit 1; }
HEAD_SHA=$(python3 -I -c 'import json,sys; print(json.load(sys.stdin)["head_sha"])' <<<"$RUN")
[[ "$HEAD_SHA" =~ ^[0-9a-f]{40}$ ]] \
    || { echo "run $RUN_ID has an invalid head SHA" >&2; exit 1; }
JOBS=$(gh api "repos/$HUB/actions/runs/$RUN_ID/jobs?per_page=100")
[ "$(python3 -I -c '
import json, sys
jobs = json.load(sys.stdin).get("jobs", [])
print(sum(job.get("name") == "seal" and job.get("conclusion") == "success" for job in jobs))
' <<<"$JOBS")" = "1" ] \
    || { echo "run $RUN_ID has no successful protected seal job" >&2; exit 1; }

mkdir -p "$DEST"
gh run download "$RUN_ID" -R "$HUB" -p 'seaworthy-preprod-*' -D "$DEST"

MANIFEST=$(find "$DEST" -name seaworthy.json -type f)
ARTIFACT=$(find "$DEST" -name 'rapp-preprod-*.tar.gz' -type f)
SOAK=$(find "$DEST" -name soak-evidence.json -type f)
HISTORY=$(find "$DEST" -name brainstem-history -type d)
for pair in "manifest:$MANIFEST" "artifact:$ARTIFACT" "soak evidence:$SOAK" "Brainstem history:$HISTORY"; do
    label=${pair%%:*}
    path=${pair#*:}
    [ -n "$path" ] && [ "$(printf '%s\n' "$path" | sed '/^$/d' | wc -l | tr -d ' ')" = "1" ] \
        || { echo "expected exactly one $label" >&2; exit 1; }
done
MANIFEST_RUN_ID=$(python3 -I -c \
    'import json,sys; print(json.load(open(sys.argv[1]))["evidence"]["preprod"]["run_id"])' \
    "$MANIFEST")
MANIFEST_RUN_URL=$(python3 -I -c \
    'import json,sys; print(json.load(open(sys.argv[1]))["evidence"]["preprod"]["url"])' \
    "$MANIFEST")
MANIFEST_CONTROL_SHA=$(python3 -I -c \
    'import json,sys; print(json.load(open(sys.argv[1]))["evidence"]["control_plane"]["commit"])' \
    "$MANIFEST")
RUN_URL=$(python3 -I -c 'import json,sys; print(json.load(sys.stdin)["html_url"])' <<<"$RUN")
[ "$MANIFEST_RUN_ID" = "$RUN_ID" ] \
    || { echo "sealed manifest belongs to another run" >&2; exit 1; }
[ "$MANIFEST_RUN_URL" = "$RUN_URL" ] \
    || { echo "sealed manifest run URL does not match" >&2; exit 1; }
[ "$MANIFEST_CONTROL_SHA" = "$HEAD_SHA" ] \
    || { echo "sealed manifest control-plane commit does not match the run" >&2; exit 1; }
ROLLBACK_REF=$(python3 -I -c \
    'import json,sys; print(json.load(open(sys.argv[1]))["rollback"]["ref"])' \
    "$MANIFEST")
[[ "$ROLLBACK_REF" =~ ^brainstem-v[0-9]+\.[0-9]+\.[0-9]+$ ]] \
    || { echo "invalid rollback ref in sealed manifest" >&2; exit 1; }
ROLLBACK="$HISTORY/$ROLLBACK_REF.json"
[ -f "$ROLLBACK" ] || { echo "selected rollback frame is missing" >&2; exit 1; }
SOAK_SHA=$(python3 -I -c \
    'import json,sys; print(json.load(open(sys.argv[1]))["evidence"]["soak"]["sha256"])' \
    "$MANIFEST")
ACTUAL_SOAK_SHA=$(python3 -I -c \
    'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest())' \
    "$SOAK")
[ "$ACTUAL_SOAK_SHA" = "$SOAK_SHA" ] \
    || { echo "archived soak evidence does not match sealed readiness" >&2; exit 1; }
EXPECTED_HISTORY_SHA=$(python3 -I -c \
    'import json,sys; print(json.load(open(sys.argv[1]))["rollback"]["history_sha256"])' \
    "$MANIFEST")
ACTUAL_HISTORY_SHA=$(python3 -I "$HERE/brainstem_history.py" digest \
    --directory "$HISTORY")
[ "$ACTUAL_HISTORY_SHA" = "$EXPECTED_HISTORY_SHA" ] \
    || { echo "archived Brainstem history does not match sealed readiness" >&2; exit 1; }

MATERIAL_ARGS=()
while IFS= read -r material; do
    [ -n "$material" ] || continue
    name=$(basename "$material" .tar.gz)
    MATERIAL_ARGS+=(--material "$name=$material")
done < <(find "$DEST" -name 'dependency-material-*.tar.gz' -type f | sort)
[ "${#MATERIAL_ARGS[@]}" -gt 0 ] || {
    echo "sealed Preprod artifact has no dependency materials" >&2
    exit 1
}

python3 -I "$HERE/preprod_gate.py" verify \
    --artifact "$ARTIFACT" \
    --manifest "$MANIFEST" \
    --verify-provenance \
    "${MATERIAL_ARGS[@]}"

VERIFY_REPO=$(mktemp -d "${TMPDIR:-/tmp}/preprod-history-XXXXXX")
trap 'rm -rf "$VERIFY_REPO"' EXIT
git clone --quiet https://github.com/kody-w/rapp-installer.git "$VERIFY_REPO"
python3 -I "$HERE/brainstem_history.py" verify-chain \
    --repo "$VERIFY_REPO" \
    --directory "$HISTORY"
python3 -I "$HERE/brainstem_history.py" verify \
    --repo "$VERIFY_REPO" \
    --frame "$ROLLBACK"

printf '{\n  "run_id": "%s",\n  "url": "%s",\n  "archived_at": "%s"\n}\n' \
    "$RUN_ID" "$RUN_URL" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$DEST/RUN.json"
git -C "$RING_DIR/.." add "$DEST"
rm -rf "$VERIFY_REPO"
trap - EXIT
echo "✓ archived sealed Preprod evidence to .ring/preprod/run-$RUN_ID (staged)"
