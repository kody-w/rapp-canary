#!/bin/bash
# register_battlestation_runner.sh — mint a fresh runner registration token for
# kody-w/rapp-canary and print the exact PowerShell to paste into the Windows
# battlestation (over VNC/Tailscale). Tokens expire in ~1 hour; re-run freely.
#
# The runner becomes the train's REAL-WINDOWS preflight leg: labels
# [self-hosted, windows, battlestation], installed as a service so it survives
# reboots. The device workflow that consumes it never triggers on pull_request,
# so fork code can never reach this machine.
set -euo pipefail

REPO="kody-w/rapp-canary"
TOKEN=$(gh api -X POST "repos/$REPO/actions/runners/registration-token" -q .token)
VER=$(gh api repos/actions/runner/releases/latest -q .tag_name | tr -d v)

cat <<EOF
──────────────────────────────────────────────────────────────────────
Paste this into an ADMIN PowerShell on the battlestation (token expires
in ~1 hour — re-run this script for a fresh one):
──────────────────────────────────────────────────────────────────────

mkdir C:\\actions-runner-rapp-canary ; cd C:\\actions-runner-rapp-canary
Invoke-WebRequest -Uri https://github.com/actions/runner/releases/download/v$VER/actions-runner-win-x64-$VER.zip -OutFile runner.zip
Add-Type -AssemblyName System.IO.Compression.FileSystem ; [System.IO.Compression.ZipFile]::ExtractToDirectory("\$PWD\\runner.zip", "\$PWD")
.\\config.cmd --url https://github.com/$REPO --token $TOKEN --name battlestation --labels self-hosted,windows,battlestation --runasservice --unattended

──────────────────────────────────────────────────────────────────────
Then tell Claude "runner is up" — verification is:
  gh api repos/$REPO/actions/runners -q '.runners[] | .name + " " + .status'
──────────────────────────────────────────────────────────────────────
EOF
