#!/bin/bash
# sync-to-aibast.sh — push a rapp-installer release downstream into a
# microsoft/aibast-agents-library checkout, without clobbering anything
# the downstream repo owns.
#
# Usage:
#   git checkout brainstem-vX.Y.Z          # the pre-release channel ships tags, not tip
#   tools/sync-to-aibast.sh /path/to/aibast-agents-library-checkout
#
# What it does (driven entirely by tools/aibast.manifest):
#   1. verbatim/binary paths are copied from this checkout into the target
#      (directories sync with --delete so upstream removals flow too)
#   2. every synced text file gets the mechanical URL rewrite
#      (kody-w/rapp-installer -> microsoft/aibast-agents-library)
#   3. docs/ Pages mirrors are regenerated in the target from its root installers
#   4. downstream patches in <target>/.sync/patches/*.patch are re-applied
#      (this is how aibast carries deltas the rewrite can't express,
#       e.g. Tier-2 installers cloning rapp_ai/ instead of CommunityRAPP)
#   5. downstream-owned files (report lines) are diffed and REPORTED, never touched
#
# The script stages nothing and commits nothing — inspect `git status` in the
# target, run its tests, then commit on a sync/brainstem-vX.Y.Z branch.

set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
say()  { echo -e "$1"; }
die()  { say "${RED}✗ $1${NC}"; exit 1; }

SRC="$(cd "$(dirname "$0")/.." && pwd)"
MANIFEST="$SRC/tools/aibast.manifest"
TARGET="${1:-}"

[ -n "$TARGET" ] || die "usage: tools/sync-to-aibast.sh <aibast-checkout>"
TARGET="$(cd "$TARGET" && pwd)" || die "target not found: $1"
[ -f "$MANIFEST" ] || die "manifest missing: $MANIFEST"
[ -f "$SRC/rapp_brainstem/VERSION" ] || die "$SRC does not look like rapp-installer"
[ -d "$TARGET/.git" ] || die "$TARGET is not a git checkout"
git -C "$TARGET" remote -v | grep -q "aibast-agents-library" \
    || die "$TARGET does not look like an aibast-agents-library checkout"

# The downstream channel ships from the grail, never from a ring or a fork.
# Rehearsing a sync from elsewhere is an explicit act: RAPP_SYNC_ALLOW_NONGRAIL=1.
SRC_ORIGIN="$(git -C "$SRC" remote get-url origin 2>/dev/null || echo none)"
if ! echo "$SRC_ORIGIN" | grep -qi "github\.com[:/]kody-w/rapp-installer"; then
    [ "${RAPP_SYNC_ALLOW_NONGRAIL:-0}" = "1" ] \
        || die "source origin is $SRC_ORIGIN, not kody-w/rapp-installer — set RAPP_SYNC_ALLOW_NONGRAIL=1 only for a rehearsal, never a real sync"
    say "${YELLOW}⚠ REHEARSAL: syncing from $SRC_ORIGIN, not the grail${NC}"
fi

# Downstream deltas the rewrite can't express live in <target>/.sync/patches/.
# A target without that directory is either brand-new or silently missing its
# patches — make the operator say so out loud.
if ! compgen -G "$TARGET/.sync/patches/*.patch" > /dev/null; then
    [ "${RAPP_SYNC_ALLOW_NO_PATCHES:-0}" = "1" ] \
        || die "$TARGET has no .sync/patches/*.patch — if this target genuinely carries no downstream patches, re-run with RAPP_SYNC_ALLOW_NO_PATCHES=1"
    say "${YELLOW}⚠ no downstream patches present — proceeding on explicit override${NC}"
fi

# Upstream must be clean and should be a tagged release — the downstream
# channel consumes releases, not work in progress.
[ -z "$(git -C "$SRC" status --porcelain)" ] || die "upstream checkout is dirty — sync from a clean tagged release"
VERSION="$(cat "$SRC/rapp_brainstem/VERSION")"
if ! git -C "$SRC" describe --tags --exact-match HEAD 2>/dev/null | grep -q .; then
    say "${YELLOW}⚠ HEAD is not on a tag — the pre-release channel normally ships brainstem-v$VERSION${NC}"
fi

say "Syncing rapp-installer v$VERSION -> $TARGET"

# ---- parse manifest ----
SYNCED_TEXT=()   # files eligible for rewrite
REWRITES=()      # from|to pairs, in manifest order
REPORTS=()

is_text() { case "$1" in *.zip|*.png|*.jpg|*.gif|*.ico|*.pyc) return 1;; *) return 0;; esac; }

# Build artifacts never sync — they can carry pre-rewrite strings (a compiled
# .pyc holds brainstem.py's source URLs) and would trip the leak guard.
RSYNC_EXCLUDES=(--exclude='.git' --exclude='__pycache__' --exclude='*.pyc' --exclude='.pytest_cache' --exclude='venv' --exclude='.venv')

collect_dir_files() { # record every file rsynced from a dir, for the rewrite pass
    local dir="$1"
    while IFS= read -r -d '' f; do
        local rel="${f#"$SRC"/}"
        case "$rel" in */__pycache__/*|*.pyc|*/.pytest_cache/*) continue;; esac
        is_text "$rel" && SYNCED_TEXT+=("$rel")
    done < <(find "$SRC/$dir" -type f -print0)
}

while read -r directive a b; do
    case "$directive" in
        ''|'#'*) continue ;;
        verbatim)
            if [ -d "$SRC/$a" ]; then
                mkdir -p "$TARGET/$a"
                rsync -a --delete "${RSYNC_EXCLUDES[@]}" "$SRC/$a/" "$TARGET/$a/"
                collect_dir_files "${a%/}"
                say "  ${GREEN}✓${NC} verbatim ${a} (dir)"
            elif [ -f "$SRC/$a" ]; then
                mkdir -p "$TARGET/$(dirname "$a")"
                cp "$SRC/$a" "$TARGET/$a"
                is_text "$a" && SYNCED_TEXT+=("$a")
                say "  ${GREEN}✓${NC} verbatim ${a}"
            else
                die "manifest names missing path: $a"
            fi ;;
        binary)
            cp "$SRC/$a" "$TARGET/$a"
            say "  ${GREEN}✓${NC} binary   ${a}" ;;
        mirror)
            # regenerated from the target's own (already synced+rewritten) root file,
            # so mirrors are byte-identical to what the target repo tests
            MIRRORS+=("$a|$b") ;;
        rewrite) REWRITES+=("$a|$b") ;;
        report)  REPORTS+=("$a") ;;
        *) die "unknown manifest directive: $directive" ;;
    esac
done < "$MANIFEST"

# ---- mechanical rewrite over every synced text file ----
for rel in "${SYNCED_TEXT[@]}"; do
    f="$TARGET/$rel"
    [ -f "$f" ] || continue
    for pair in "${REWRITES[@]}"; do
        from="${pair%%|*}"; to="${pair##*|}"
        # portable in-place sed (BSD + GNU); delimiter ~ never appears in the URLs
        sed -i.rappsync "s~${from}~${to}~g" "$f" && rm -f "$f.rappsync"
    done
done
say "  ${GREEN}✓${NC} rewrite applied to ${#SYNCED_TEXT[@]} synced text files"

# ---- regenerate Pages mirrors inside the target ----
for pair in "${MIRRORS[@]:-}"; do
    [ -n "$pair" ] || continue
    dst="${pair%%|*}"; srcfile="${pair##*|}"
    mkdir -p "$TARGET/$(dirname "$dst")"
    cp "$TARGET/$srcfile" "$TARGET/$dst"
    say "  ${GREEN}✓${NC} mirror   ${dst} <- ${srcfile}"
done

# ---- re-apply downstream patches ----
if compgen -G "$TARGET/.sync/patches/*.patch" > /dev/null; then
    for p in "$TARGET"/.sync/patches/*.patch; do
        if git -C "$TARGET" apply --whitespace=nowarn "$p"; then
            say "  ${GREEN}✓${NC} patch    $(basename "$p")"
        else
            die "downstream patch no longer applies: $(basename "$p") — rebase it against v$VERSION"
        fi
    done
fi

# ---- guard: no stray upstream identity left in synced files ----
LEAKS=$(cd "$TARGET" && grep -l "kody-w/rapp-installer\|kody-w.github.io/rapp-installer" \
        "${SYNCED_TEXT[@]}" 2>/dev/null || true)
[ -z "$LEAKS" ] || die "rewrite missed upstream references in: $LEAKS"

# ---- guard: ring-owned files must never reach a downstream ----
[ ! -e "$TARGET/.ring" ] || die "ring overlay leaked downstream: $TARGET/.ring"
for wf in autonomous-pre-grail.yml test-pre-grail-rings.yml; do
    [ ! -e "$TARGET/.github/workflows/$wf" ] \
        || die "ring workflow leaked downstream: .github/workflows/$wf"
done

# ---- informational: deliberate content-repo refs that stay kody-w ----
# (CommunityRAPP / AI-Agent-Templates / RAR are dependencies, not identity;
#  the downstream scrub reviews them by hand — this just makes them visible)
CONTENT_REFS=$(cd "$TARGET" && grep -l "kody-w/CommunityRAPP\|kody-w/AI-Agent-Templates\|kody-w/RAR" \
        "${SYNCED_TEXT[@]}" 2>/dev/null | wc -l | tr -d ' ')
say "  ${YELLOW}•${NC} $CONTENT_REFS synced files carry deliberate kody-w content-repo refs (review, don't rewrite)"

# ---- report drift in downstream-owned files ----
say ""
say "Downstream-owned files (not touched) vs upstream counterparts:"
for rel in "${REPORTS[@]}"; do
    if [ -f "$SRC/$rel" ] && [ -f "$TARGET/$rel" ]; then
        n=$(diff "$SRC/$rel" "$TARGET/$rel" | grep -c '^[<>]' || true)
        say "  ${YELLOW}•${NC} $rel — $n differing lines (review manually if upstream changed)"
    fi
done

say ""
say "Result in $TARGET:"
git -C "$TARGET" status --short | sed 's/^/  /'
say ""
say "Next steps:"
say "  cd $TARGET"
say "  git checkout -b sync/brainstem-v$VERSION"
say "  bash tests/test_installer.sh && (cd rapp_brainstem && python3 -m pytest -q)"
say "  git commit -am 'sync: brainstem v$VERSION from rapp-installer' && git push -u origin sync/brainstem-v$VERSION"
say "  open a PR against microsoft/aibast-agents-library"
