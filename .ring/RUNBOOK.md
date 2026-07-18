# Ring RUNBOOK — the five verbs

The train: **Canary → Nightly → Alpha → Beta → Grail (human-only)**.
Grail is `kody-w/rapp-installer`; its `main` is production and NOTHING here
pushes to it. Two rules prevent the classic staged-train failures:

1. **Everything enters at Canary.** No change lands on Nightly/Alpha/Beta
   directly; they only ever receive promotions.
2. **A Grail hotfix re-seeds into Canary immediately** (see DEVELOP), or the
   next promotion will silently revert the fix — the oldest ring bug there is.

`automated_promotion: true` in `train.json` means `promote_ring.py` MAY write
that edge **when an operator runs it** — there is no scheduled automation and
no workflow holds write credentials. The Grail edge refuses the tool entirely.

## 1. DEVELOP (all work starts here)

```bash
cd ~/Documents/GitHub/rapp-canary
git checkout -b fix/whatever origin/main
# ...work, then:
git push -u origin fix/whatever          # preflight runs on every push
gh run watch                             # green -> merge to canary main
git checkout main && git pull && git merge --no-ff fix/whatever && git push
```

**If your change adds/removes grail-URL occurrences** (`kody-w/rapp-installer`,
its Pages host, or `kody-w/rapp-support`), the render oracle will refuse with
`rewrite count drift`. That is deliberate — recount and bump `expected_count`
in **all four** rings' `.ring/ring.json` in the same cycle (the counts are
payload-wide, so they match across rings).

**Grail hotfix re-seed** (run the moment a hotfix lands on grail main):

```bash
cd ~/Documents/GitHub/rapp-canary && git checkout main
git fetch https://github.com/kody-w/rapp-installer.git main
git merge FETCH_HEAD -m "reseed: grail hotfix" && git push origin main
```

## 2. PROMOTE (one edge at a time, operator-run)

```bash
cd ~/Documents/GitHub
SRC=rapp-canary; DST=rapp-nightly                 # then nightly->alpha, alpha->beta
for r in $SRC $DST; do git -C $r checkout main && git pull -q; done
python3 rapp-canary/.ring/tools/promote_ring.py \
  --source $SRC --target $DST \
  --source-ring ${SRC#rapp-} --target-ring ${DST#rapp-} \
  --source-commit $(git -C $SRC rev-parse HEAD) \
  --target-commit $(git -C $DST rev-parse HEAD)
git -C $DST commit -m "promote: ${SRC#rapp-} -> ${DST#rapp-}" && git -C $DST push
```

Each ring's preflight runs on the main push — a broken promotion is a red X
within minutes.

## 3. QUALIFY (whole-train, read-only credentials)

```bash
cd ~/Documents/GitHub
gh workflow run test-pre-grail-rings.yml -R kody-w/rapp-canary --ref main \
  -f canary_commit=$(git -C rapp-canary rev-parse HEAD) \
  -f nightly_commit=$(git -C rapp-nightly rev-parse HEAD) \
  -f alpha_commit=$(git -C rapp-alpha rev-parse HEAD) \
  -f beta_commit=$(git -C rapp-beta rev-parse HEAD)
gh run watch -R kody-w/rapp-canary                # all four rings + attestation chain
.ring/tools/archive_attestations.sh <run-id>     # evidence into git, outlives CI GC
```

## 4. SOAK (real machines, real auth — the honest crash signal)

```bash
.ring/tools/soak.sh start      # renders canary main, serves it on :7073 with real Copilot auth
.ring/tools/soak.sh status     # health + version + uptime + log tail
.ring/tools/soak.sh refresh    # pull latest canary main and relaunch
```

Soak = days of the maintainer's own usage on ring bytes. A release earns the
Grail gate by surviving here, not by a green dashboard alone.

### The Flight Deck (any machine on Earth, no LAN, no signup)

**https://kody-w.github.io/rapp-canary/flights.html** — the public deck.
Every ring's Pages serves its RENDERED identity (`publish-pages.yml`,
ring-owned, redeploys on every main push, drift-oracle gated), so real
devices join rings through advertised one-liners:

```bash
curl -fsSL https://kody-w.github.io/rapp-<ring>/install.sh | bash    # join (real install)
irm  https://kody-w.github.io/rapp-<ring>/install.ps1 | iex          # join (Windows)
curl -fsSL https://kody-w.github.io/rapp-canary/flight.sh | bash -s -- <ring>            # sandboxed test flight
curl -fsSL https://kody-w.github.io/rapp-canary/flight.sh | bash -s -- canary <branch>   # fly one feature
```

Test flights live under `~/.rapp-flight/` on port 7075 and never touch an
existing `~/.brainstem`. IMPORTANT: never point anyone at a ring repo's
`raw.githubusercontent.com` installer — raw main carries GRAIL identity by
design and would install the wrong repo; the Pages copies are the only
correct ring one-liners.

### Any device, zero-config, reported into an issue

Real-device testing is PULL-based: no runner registration, no tailnet
linkage, nothing to configure. On ANY machine with internet — a fresh VM, a
spare laptop, the battlestation — one line does the whole job:

```bash
curl -sL https://raw.githubusercontent.com/kody-w/rapp-canary/main/.ring/tools/device_probe.sh \
  | bash -s -- --ring canary --report-issue 42
```

That line is the entire interface: hand it to Copilot (or any agent) on the
device and it needs no other context. The probe pulls the ring, runs the REAL
installer inside a throwaway sandbox HOME, runs the full test suite plus live
health/chat asserts, prints a findings report, and — only when
`--report-issue` is given — posts it to that issue on the ring repo with
`gh`. Reporting is OFF by default. `--ring nightly|alpha|beta` picks the
ring; `--ref flight/<name>` tests a flight; `--keep` preserves the sandbox.
It REFUSES to run when :7071 is busy (the installer kills existing
listeners — a probe must never execute that against a real brainstem).

Blow the VM away afterwards — nothing outside `/tmp` is touched. (A
self-hosted-runner "push mode" was built and retired in favor of this: rings
are installable everywhere via the Flight Deck one-liners, so devices pull
tests; nothing needs to be enrolled. Windows devices use the Flight Deck's
`irm | iex` one-liners; a PowerShell probe twin is future work.)

## 5. RELEASE TO GRAIL (the only human-gated step)

```bash
# 1. verify the qualification run AND stage the exact qualified bytes:
git clone https://github.com/kody-w/rapp-installer.git /tmp/grail-release
git -C /tmp/grail-release checkout -b release/vX.Y.Z
python3 .ring/tools/grail_gate.py verify --run-id <run-id> --export-to /tmp/grail-release

# 2. inspect, test, version, commit (embed the qualification run URL):
cd /tmp/grail-release && bash tests/test_installer.sh
echo "X.Y.Z" > rapp_brainstem/VERSION
for m in install.sh install.ps1 install.cmd install.command; do cp $m docs/$m; done
git commit -am "release: vX.Y.Z (ring-qualified: <run-url>)"
git push -u origin release/vX.Y.Z            # grail preflight: full 7-VM matrix
# 3. after ALL checks green — the merge itself, per grail RELEASING.md §6:
git checkout main && git pull && git merge --no-ff release/vX.Y.Z -m "release: vX.Y.Z"
git tag -a "brainstem-vX.Y.Z" -m "ring-qualified: <run-url>"
git push origin main --tags
# 4. post-release: RELEASING.md §7 smoke + bump kody-w/RAPP KERNEL_PIN or record a skip.
```

The daily-driver checkouts (`~/.brainstem/src`, the m365 vendored copy) have
their push URLs set to `DISABLED-...` on purpose. Releasing from a fresh
`/tmp` clone (above) is the intended path; re-enabling a daily checkout is a
conscious act:

```bash
git remote set-url --push origin https://github.com/kody-w/rapp-installer.git   # enable
git remote set-url --push origin DISABLED-push-to-grail-is-a-conscious-release-act-see-rapp-canary-.ring-RUNBOOK   # re-neuter
```

**Rollback**: grail RELEASING.md §8 — `git revert` on main (protection blocks
force-pushes; revert needs none), and users pin back with
`BRAINSTEM_VERSION=X.Y.Z curl ... | bash`. Rehearse the downgrade once per
release in the preflight sandbox.
