#!/bin/bash
# register_device_runner.sh — turn ANY machine on the tailnet into a ring test
# device. Mints fresh repo-scoped runner registration tokens and prints the
# exact block to paste on the device (PowerShell for windows, bash for
# macos/linux). Tokens expire in ~1 hour; re-run freely.
#
#   register_device_runner.sh --device battlestation --os windows          # canary only
#   register_device_runner.sh --device rappter-one --os macos all          # every pre-grail ring
#   register_device_runner.sh --device lab-box --os linux --arch x64 rapp-beta
#
# The device name becomes the runner label device-test.yml targets
# (runs-on: [self-hosted, <device>]). Runners are repo-scoped on personal
# accounts, so "all" means one runner service per ring, each in its own
# directory. Grail is refused — frozen production takes no ring machinery.
set -euo pipefail

DEVICE="" OS="" ARCH=""
REPOS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --device) DEVICE="$2"; shift 2 ;;
    --os)     OS="$2"; shift 2 ;;
    --arch)   ARCH="$2"; shift 2 ;;
    all)      REPOS+=(rapp-canary rapp-nightly rapp-alpha rapp-beta); shift ;;
    *)        REPOS+=("$1"); shift ;;
  esac
done
if [ -z "$DEVICE" ] || [ -z "$OS" ]; then
  echo "usage: $0 --device <label> --os <windows|macos|linux> [--arch x64|arm64] [all|repo ...]" >&2
  exit 2
fi
[ ${#REPOS[@]} -gt 0 ] || REPOS=(rapp-canary)
case "$OS" in
  windows) PKG=win;   ARCH=${ARCH:-x64} ;;
  macos)   PKG=osx;   ARCH=${ARCH:-arm64} ;;
  linux)   PKG=linux; ARCH=${ARCH:-x64} ;;
  *) echo "unknown --os '$OS' (windows|macos|linux)" >&2; exit 2 ;;
esac
SVC="./svc.sh"; [ "$OS" = linux ] && SVC="sudo ./svc.sh"
for r in "${REPOS[@]}"; do
  [ "$r" = "rapp-installer" ] && { echo "refusing: grail is frozen production — no runner there" >&2; exit 1; }
done

VER=$(gh api repos/actions/runner/releases/latest -q .tag_name | tr -d v)

echo "──────────────────────────────────────────────────────────────────────"
if [ "$OS" = windows ]; then
  echo "Paste into an ADMIN PowerShell on '$DEVICE' (tokens expire in ~1h)."
  echo "NOTE: the service runs as NETWORK SERVICE — python and git must be"
  echo "all-users installs on the machine PATH, or repoint the service at"
  echo "your account in services.msc."
else
  echo "Paste into a terminal on '$DEVICE' (tokens expire in ~1h). svc.sh"
  echo "installs the runner as a service so it survives reboots."
fi
echo "──────────────────────────────────────────────────────────────────────"
echo
for r in "${REPOS[@]}"; do
  REPO="kody-w/$r"
  TOKEN=$(gh api -X POST "repos/$REPO/actions/runners/registration-token" -q .token)
  echo "# ── $REPO ──"
  if [ "$OS" = windows ]; then
    cat <<EOF
mkdir C:\\actions-runner-$r ; cd C:\\actions-runner-$r
Invoke-WebRequest -Uri https://github.com/actions/runner/releases/download/v$VER/actions-runner-win-$ARCH-$VER.zip -OutFile runner.zip
Add-Type -AssemblyName System.IO.Compression.FileSystem ; [System.IO.Compression.ZipFile]::ExtractToDirectory("\$PWD\\runner.zip", "\$PWD")
.\\config.cmd --url https://github.com/$REPO --token $TOKEN --name $DEVICE --labels self-hosted,$OS,$DEVICE --runasservice --unattended

EOF
  else
    cat <<EOF
mkdir -p ~/actions-runner-$r && cd ~/actions-runner-$r
curl -sL -o runner.tar.gz https://github.com/actions/runner/releases/download/v$VER/actions-runner-$PKG-$ARCH-$VER.tar.gz
tar xzf runner.tar.gz
./config.sh --url https://github.com/$REPO --token $TOKEN --name $DEVICE --labels self-hosted,$OS,$DEVICE --unattended
$SVC install && $SVC start

EOF
  fi
done
echo "──────────────────────────────────────────────────────────────────────"
echo "Then tell Claude \"runner is up\" — verification per repo is:"
for r in "${REPOS[@]}"; do
  echo "  gh api repos/kody-w/$r/actions/runners -q '.runners[] | .name + \" \" + .status'"
done
echo "──────────────────────────────────────────────────────────────────────"
