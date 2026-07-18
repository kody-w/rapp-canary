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

### Real-Windows device leg (the battlestation)

GitHub's windows-latest VMs are clean-room; the battlestation (Tailscale:
`kodysbattlestation.tail99115f.ts.net`) is real hardware, real home network,
real Defender. One-time onboarding (mints ~1h tokens, re-run freely):

```bash
.ring/tools/register_battlestation_runner.sh        # canary only (default)
.ring/tools/register_battlestation_runner.sh all    # every pre-grail ring
```

Paste the printed block into an ADMIN PowerShell on the battlestation (VNC
over Tailscale). `windows-device-test.yml` then runs the REAL advertised
install path on the device on every canary main push, plus on demand:

```bash
gh workflow run windows-device-test.yml -R kody-w/rapp-canary --ref main
```

It never triggers on pull_request (fork code can never reach the machine),
sandboxes USERPROFILE so the device's own `~/.brainstem` is untouched, and is
advisory: battlestation offline ⇒ the run queues (auto-cancel in 24h) and
never blocks promotion. Canary is where the leg lives — everything enters at
canary, and later rings receive attested copies of the same payload.

### Any device, on demand, reported into an issue

The battlestation generalizes to a fleet: register any tailnet machine
(`--os macos`/`linux` prints a bash block instead of PowerShell):

```bash
.ring/tools/register_device_runner.sh --device rappter-one --os macos
.ring/tools/register_device_runner.sh --device battlestation --os windows all
```

Then ANY agent (Copilot, Claude) or human fires a device test with one
command — this line is the whole interface an agent needs to know:

```bash
gh workflow run device-test.yml -R kody-w/rapp-canary \
  -f device=rappter-one -f report_issue=42    # report_issue optional — reporting is OFF by default
```

or, from inside a GitHub issue (collaborators only — the run rocket-reacts to
ack, then posts the verdict back to the same thread):

```
/device-test rappter-one
```

`device-test.yml` resolves the device label, runs the real installer sandboxed
on that machine (PowerShell 5.1 path or unix path per `runner.os`), tears the
sandbox down, and — only when an issue is in play — comments the verdict + run
link. It never triggers on pull_request. The workflow is ring-owned: copy the
file to another ring repo to give that ring its own fleet surface.

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
