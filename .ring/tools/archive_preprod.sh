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
[ "$(python3 -c 'import json,sys; print(json.load(sys.stdin)[\"name\"])' <<<"$RUN")" = "Stage Preprod" ] \
    || { echo "run $RUN_ID is not Stage Preprod" >&2; exit 1; }
[ "$(python3 -c 'import json,sys; print(json.load(sys.stdin)[\"conclusion\"])' <<<"$RUN")" = "success" ] \
    || { echo "run $RUN_ID is not green" >&2; exit 1; }

mkdir -p "$DEST"
gh run download "$RUN_ID" -R "$HUB" -p 'seaworthy-preprod-*' -D "$DEST"

MANIFEST=$(find "$DEST" -name seaworthy.json -type f)
ARTIFACT=$(find "$DEST" -name 'rapp-preprod-*.tar.gz' -type f)
ROLLBACK=$(find "$DEST" -name rollback-brainstem.json -type f)
for pair in "manifest:$MANIFEST" "artifact:$ARTIFACT" "rollback frame:$ROLLBACK"; do
    label=${pair%%:*}
    path=${pair#*:}
    [ -n "$path" ] && [ "$(printf '%s\n' "$path" | sed '/^$/d' | wc -l | tr -d ' ')" = "1" ] \
        || { echo "expected exactly one $label" >&2; exit 1; }
done

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

python3 "$HERE/preprod_gate.py" verify \
    --artifact "$ARTIFACT" \
    --manifest "$MANIFEST" \
    "${MATERIAL_ARGS[@]}"

RUN_URL=$(python3 -c 'import json,sys; print(json.load(sys.stdin)["html_url"])' <<<"$RUN")
printf '{\n  "run_id": "%s",\n  "url": "%s",\n  "archived_at": "%s"\n}\n' \
    "$RUN_ID" "$RUN_URL" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$DEST/RUN.json"
git -C "$RING_DIR/.." add "$DEST"
echo "✓ archived sealed Preprod evidence to .ring/preprod/run-$RUN_ID (staged)"
