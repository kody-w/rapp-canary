#!/bin/bash
# register_battlestation_runner.sh — mint fresh runner registration tokens and
# print the exact PowerShell to paste into the Windows battlestation (over
# VNC/Tailscale). Tokens expire in ~1 hour; re-run freely.
#
#   register_battlestation_runner.sh              # canary only (the default —
#                                                 # everything enters at canary)
#   register_battlestation_runner.sh all          # every pre-grail ring
#   register_battlestation_runner.sh rapp-beta    # explicit ring repo(s)
#
# The runner becomes the train's REAL-WINDOWS preflight leg: labels
# [self-hosted, windows, battlestation], installed as a service so it survives
# reboots. GitHub runners are repo-scoped on personal accounts, so "all" means
# one runner service per ring, each in its own C:\actions-runner-<repo> dir.
# The device workflow that consumes them never triggers on pull_request, so
# fork code can never reach this machine. Grail is excluded by design — it is
# frozen production and takes no ring machinery.
set -euo pipefail

case "${1:-}" in
  all)  REPOS=(rapp-canary rapp-nightly rapp-alpha rapp-beta) ;;
  "")   REPOS=(rapp-canary) ;;
  *)    REPOS=("$@") ;;
esac
for r in "${REPOS[@]}"; do
  [ "$r" = "rapp-installer" ] && { echo "refusing: grail is frozen production — no runner there" >&2; exit 1; }
done

VER=$(gh api repos/actions/runner/releases/latest -q .tag_name | tr -d v)

echo "──────────────────────────────────────────────────────────────────────"
echo "Paste this into an ADMIN PowerShell on the battlestation (tokens expire"
echo "in ~1 hour — re-run this script for fresh ones). NOTE: the service runs"
echo "as NETWORK SERVICE — python and git must be all-users installs on the"
echo "machine PATH, or repoint the service at your account in services.msc."
echo "──────────────────────────────────────────────────────────────────────"
echo
for r in "${REPOS[@]}"; do
  REPO="kody-w/$r"
  TOKEN=$(gh api -X POST "repos/$REPO/actions/runners/registration-token" -q .token)
  cat <<EOF
# ── $REPO ──
mkdir C:\\actions-runner-$r ; cd C:\\actions-runner-$r
Invoke-WebRequest -Uri https://github.com/actions/runner/releases/download/v$VER/actions-runner-win-x64-$VER.zip -OutFile runner.zip
Add-Type -AssemblyName System.IO.Compression.FileSystem ; [System.IO.Compression.ZipFile]::ExtractToDirectory("\$PWD\\runner.zip", "\$PWD")
.\\config.cmd --url https://github.com/$REPO --token $TOKEN --name battlestation --labels self-hosted,windows,battlestation --runasservice --unattended

EOF
done
echo "──────────────────────────────────────────────────────────────────────"
echo "Then tell Claude \"runner is up\" — verification per repo is:"
for r in "${REPOS[@]}"; do
  echo "  gh api repos/kody-w/$r/actions/runners -q '.runners[] | .name + \" \" + .status'"
done
echo "──────────────────────────────────────────────────────────────────────"
