#!/bin/bash
# archive_attestations.sh <run-id> — copy a green qualification run's
# attestation chain out of the expiring CI artifact store and into
# ring-owned git history (.ring/attestations/), so the evidence that
# justified a Grail merge can still be produced years later.
set -euo pipefail

HUB="kody-w/rapp-canary"
RUN_ID="${1:-}"
[ -n "$RUN_ID" ] || { echo "usage: archive_attestations.sh <run-id>" >&2; exit 2; }
HERE="$(cd "$(dirname "$0")" && pwd)"
RING_DIR="$(dirname "$HERE")"

CONCLUSION=$(gh api "repos/$HUB/actions/runs/$RUN_ID" -q .conclusion)
NAME=$(gh api "repos/$HUB/actions/runs/$RUN_ID" -q .name)
URL=$(gh api "repos/$HUB/actions/runs/$RUN_ID" -q .html_url)
[ "$NAME" = "Test Pre-Grail Rings" ] || { echo "✗ run $RUN_ID is '$NAME'" >&2; exit 1; }
[ "$CONCLUSION" = "success" ] || { echo "✗ run $RUN_ID concluded '$CONCLUSION'" >&2; exit 1; }

DEST="$RING_DIR/attestations/run-$RUN_ID"
mkdir -p "$DEST"
gh run download "$RUN_ID" -R "$HUB" -p 'pre-grail-attestations-*' -D "$DEST"
printf '{\n  "run_id": "%s",\n  "url": "%s",\n  "archived_at": "%s"\n}\n' \
    "$RUN_ID" "$URL" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$DEST/RUN.json"
git -C "$RING_DIR/.." add "$DEST"
echo "✓ archived to .ring/attestations/run-$RUN_ID (staged — commit on canary main)"
