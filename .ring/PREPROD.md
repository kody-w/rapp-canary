# Preprod — the Seaworthiness Gate

Preprod is a protected GitHub deployment environment between Beta and Grail.
It is a **gate, not a ring**: it has no independently evolving payload, no
customer audience, and no branch that can drift away from the candidate.
All control-plane implementation remains in Canary. It never modifies the
immutable Grail `rapp_brainstem/brainstem.py`.

The governing document is
[`SEAWORTHINESS-CONSTITUTION.md`](SEAWORTHINESS-CONSTITUTION.md).

The reference repository's `preprod` environment requires an explicit owner
approval and accepts deployments only from `main`. Enterprise organizations
must replace that owner reviewer with an independent release team and enable
prevent-self-review to enforce separation of duties.

## Invariants

1. Preprod starts from a green whole-train qualification run.
2. The qualification's Beta commit must still be current.
3. Beta's own main preflight must be green for that exact commit.
4. `grail_gate.py` exports the qualified shared payload into a clean
   Grail-shaped checkout.
5. The qualified payload's `rapp_brainstem/brainstem.py` must already equal the
   immutable `brainstem-v0.6.16` Grail pin. A mismatch is blocking
   `kernel-drift`; the gate never swaps bytes after testing.
6. `preprod_gate.py` packages that tree once. Every later action uses its SHA-256.
7. Platform jobs accept registry-only requirements, resolve separate runtime
   and test-tool wheelhouses without executing package code, and seal their
   locks, SBOM, vulnerability report, license report, and hashes.
   The vulnerability scanner itself runs in a disposable venv installed from
   the reviewed hash-locked `.ring/tooling/pip-audit.lock`.
8. A protected `preprod` environment provides the human approval boundary.
9. Approval seals `seaworthy.json`; it cannot change the artifact or materials.
10. Grail imports the sealed artifact with `preprod_gate.py export`.
11. Enterprise deployment installs only from the sealed platform wheelhouse
    with `--no-index`; live dependency resolution is not seaworthy.
12. Preprod control files remain under `.ring/` and `.github/workflows/`; they
   cannot enter the Grail payload.
13. The final Beta version is already in the artifact. Grail never edits
    `VERSION` or `brainstem.py` after Preprod.

The governing RAPP/1 authority is rev-10 §11.1 and Protocol Constitution
Article 15 at the exact commit and hashes recorded in `preprod-policy.json`.
The release scope is `https://github.com/kody-w/rapp-canary`; candidate content
cannot select a different `grail_id`.

This gate does not normalize drift by copying the Grail file over a tested ring
artifact. Any ring carrying different Brainstem bytes must move that behavior
outside the kernel and re-run the train before Preprod can begin.

## Stage a candidate

```bash
gh workflow run stage-preprod.yml -R kody-w/rapp-canary --ref main \
  -f qualification_run_id=<green-pre-grail-run> \
  -f beta_preflight_run_id=<green-beta-main-preflight-run> \
  -f rollback_ref=brainstem-vX.Y.Z \
  -f soak_evidence_url=https://raw.githubusercontent.com/kody-w/rapp-canary/<commit>/.ring/soak/<frame>.json \
  -f soak_evidence_sha256=<sha256-of-frame> \
  -f owner=<accountable-team> \
  -f model_id=<explicit-qualified-model>
```

Create the soak frame only after the qualified payload has completed its real
authenticated soak:

```bash
SOAK_REF=<qualification-canary-commit> GITHUB_MODEL=<explicit-qualified-model> \
  .ring/tools/soak.sh start
.ring/tools/soak.sh evidence \
  --beta-commit <qualified-beta-commit> \
  --qualification-run <green-pre-grail-run> \
  --model-id <explicit-qualified-model> \
  --output ".ring/soak/<beta-commit>-<model>.json"
git add .ring/soak && git commit -m "evidence: record qualified soak" && git push
shasum -a 256 ".ring/soak/<beta-commit>-<model>.json"
```

The URL must name that file at its full 40-character Canary commit. The frame
binds authenticated start/end chats plus periodic health/model probes across
the complete interval and contains no credential or model response content.
The model is an explicit, attested qualification observation—not a claim that
the frozen Grail prevents a later authorized `/models/set` operation.

The workflow:

1. validates qualification, Beta, rollback, and hash-pinned soak evidence
2. exports exact qualified bytes into a Grail-shaped candidate
3. publishes and verifies the complete `rapp/1:brainstem` rollback chain
4. packages a deterministic artifact and `rapp/1:readiness` manifest
5. safely extracts and verifies the artifact on Windows, macOS, and Linux
6. constructs sealed runtime and test-tool wheelhouses on each platform and
   exercises the same offline runtime preparation used by enterprise deployment
7. pauses at the protected `preprod` environment
8. seals the approved manifest and publishes build provenance with the exact
   workflow certificate identity, source ref, and source digest

After success:

```bash
.ring/tools/archive_preprod.sh <run-id>
git commit -m "ring: archive Preprod evidence for run <run-id>"
git push origin main
```

## Release the sealed artifact to Grail

Download the `seaworthy-preprod-*` artifact from the approved run, then:

```bash
git clone https://github.com/kody-w/rapp-installer.git /tmp/grail-release
git -C /tmp/grail-release checkout -b release/vX.Y.Z

python3 -I <canary-checkout>/.ring/tools/preprod_gate.py verify \
  --artifact /path/to/rapp-preprod-<sha>.tar.gz \
  --manifest /path/to/seaworthy.json \
  --material dependency-material-linux=/path/to/dependency-material-linux.tar.gz \
  --material dependency-material-macos=/path/to/dependency-material-macos.tar.gz \
  --material dependency-material-windows=/path/to/dependency-material-windows.tar.gz

python3 -I <canary-checkout>/.ring/tools/preprod_gate.py export \
  --artifact /path/to/rapp-preprod-<sha>.tar.gz \
  --manifest /path/to/seaworthy.json \
  --rollback-frame /path/to/brainstem-history/brainstem-vX.Y.Z.json \
  --target /tmp/grail-release \
  --material dependency-material-linux=/path/to/dependency-material-linux.tar.gz \
  --material dependency-material-macos=/path/to/dependency-material-macos.tar.gz \
  --material dependency-material-windows=/path/to/dependency-material-windows.tar.gz

# Run Grail tests, then prove no test or operator changed the staged tree:
python3 -I <canary-checkout>/.ring/tools/preprod_gate.py verify-staged-tree \
  --artifact /path/to/rapp-preprod-<sha>.tar.gz \
  --manifest /path/to/seaworthy.json \
  --target /tmp/grail-release \
  --material dependency-material-linux=/path/to/dependency-material-linux.tar.gz \
  --material dependency-material-macos=/path/to/dependency-material-macos.tar.gz \
  --material dependency-material-windows=/path/to/dependency-material-windows.tar.gz

git -C /tmp/grail-release commit -m "release: vX.Y.Z"
RELEASE_COMMIT=$(git -C /tmp/grail-release rev-parse HEAD)
python3 -I <canary-checkout>/.ring/tools/preprod_gate.py verify-release-commit \
  --artifact /path/to/rapp-preprod-<sha>.tar.gz \
  --manifest /path/to/seaworthy.json \
  --target /tmp/grail-release \
  --material dependency-material-linux=/path/to/dependency-material-linux.tar.gz \
  --material dependency-material-macos=/path/to/dependency-material-macos.tar.gz \
  --material dependency-material-windows=/path/to/dependency-material-windows.tar.gz

git -C /tmp/grail-release checkout main
git -C /tmp/grail-release pull --ff-only
git -C /tmp/grail-release merge --no-ff release/vX.Y.Z -m "release: vX.Y.Z"
python3 -I <canary-checkout>/.ring/tools/preprod_gate.py verify-final-merge \
  --artifact /path/to/rapp-preprod-<sha>.tar.gz \
  --manifest /path/to/seaworthy.json \
  --target /tmp/grail-release \
  --release-commit "$RELEASE_COMMIT" \
  --material dependency-material-linux=/path/to/dependency-material-linux.tar.gz \
  --material dependency-material-macos=/path/to/dependency-material-macos.tar.gz \
  --material dependency-material-windows=/path/to/dependency-material-windows.tar.gz
```

Run the final verification before pushing or tagging. It requires the sealed
base and release commit to be the merge parents and the final merge tree to
equal the sealed tree byte-for-byte.
Enterprise deployment also consumes the sealed platform dependency bundle;
the public one-liner remains unchanged.

For an enterprise-managed runtime, unpack the matching dependency material and
prepare the entire runtime without consulting a package index:

```bash
python3 -I <canary-checkout>/.ring/tools/preprod_gate.py prepare-runtime \
  --artifact /path/to/rapp-preprod-<sha>.tar.gz \
  --manifest /path/to/seaworthy.json \
  --destination /opt/rapp/releases/<sha> \
  --state-dir /var/lib/rapp \
  --material dependency-material-linux=/path/to/dependency-material-linux.tar.gz \
  --material dependency-material-macos=/path/to/dependency-material-macos.tar.gz \
  --material dependency-material-windows=/path/to/dependency-material-windows.tar.gz

PORT=7071 python3 -I <canary-checkout>/.ring/tools/preprod_gate.py \
  launch-runtime \
  --artifact /path/to/rapp-preprod-<sha>.tar.gz \
  --manifest /path/to/seaworthy.json \
  --state-dir /var/lib/rapp \
  --evidence /var/log/rapp/kernel-launch.json \
  --material dependency-material-linux=/path/to/dependency-material-linux.tar.gz \
  --material dependency-material-macos=/path/to/dependency-material-macos.tar.gz \
  --material dependency-material-windows=/path/to/dependency-material-windows.tar.gz
```

The command verifies provenance for the source, manifest, and every platform
material; selects the current platform; extracts into a new release directory;
checks the Python minor version and CPU architecture; and installs only from the
sealed wheelhouse with `--no-index`. `launch-runtime` reads and verifies the
sealed source, manifest, and dependency materials again; rejects conflicting
persisted model state; reconstructs a fresh ephemeral runtime; reads and
verifies the kernel once; then streams those exact in-memory bytes to the
isolated runtime interpreter. It never trusts a previously prepared mutable
tree or performs a vulnerable hash-then-path reopen.

## Unknown-unknown strategy

Unknowns are controlled by detection and containment rather than confidence:

| Failure class | Detection | Containment |
|---|---|---|
| Artifact or dependency drift | SHA-256, provenance, critical-file hashes | Reject and rebuild qualification |
| Environment drift | Cross-platform artifact verification | Separate Preprod environment |
| State migration defects | Upgrade/repair/live-writer tests | Atomic migration and rollback frame |
| Concurrency races | Multi-process and multi-thread tests | Leases, ownership, bounded retries |
| Identity/provider outages | Offline, 401/403/429 paths | Preserve valid state; fail explicitly |
| Bad automation | Exact commit/path checks, adversarial review | No Grail credentials in automation |
| Hidden runtime degradation | Soak evidence and SLOs | Degrade/revoke readiness |
| Human operational error | Protected environment and immutable digest | Separation of duties and rollback |
| Unknown or unmeasured behavior | Explicit missing-evidence state | Candidate cannot be sealed |

Preprod does not make experimentation risk-free. It makes failures observable,
contained, and reversible before they reach Grail.

## Preserve every known-good Grail Brainstem

After a Grail release tag is created, append its frame to
`.ring/brainstem-history/`:

```bash
python3 -I .ring/tools/brainstem_history.py record \
  --repo /path/to/rapp-installer \
  --release-ref brainstem-vX.Y.Z \
  --parent .ring/brainstem-history/brainstem-vPREVIOUS.json \
  --frame .ring/brainstem-history/brainstem-vX.Y.Z.json

python3 -I .ring/tools/brainstem_history.py verify \
  --repo /path/to/rapp-installer \
  --frame .ring/brainstem-history/brainstem-vX.Y.Z.json
```

Commit that frame to Canary. Preprod publishes the complete chain, and rollback
uses the selected frame's exact commit rather than resolving a mutable tag at
incident time. The frame proves which exact Brainstem the human-readable tag
contains and links it to the previous known-good version.
