# Preprod — the Seaworthiness Gate

Preprod is a protected GitHub deployment environment between Beta and Grail.
It is a **gate, not a ring**: it has no independently evolving payload, no
customer audience, and no branch that can drift away from the candidate.

The governing document is
[`SEAWORTHINESS-CONSTITUTION.md`](SEAWORTHINESS-CONSTITUTION.md).

## Invariants

1. Preprod starts from a green whole-train qualification run.
2. The qualification's Beta commit must still be current.
3. Beta's own main preflight must be green for that exact commit.
4. `grail_gate.py` exports the qualified shared payload into a clean
   Grail-shaped checkout.
5. `preprod_gate.py` packages that tree once. Every later action uses its SHA-256.
6. Platform jobs resolve wheels without executing package code and seal their
   lock, SBOM, and hashes as deployment materials.
7. A protected `preprod` environment provides the human approval boundary.
8. Approval seals `seaworthy.json`; it cannot change the artifact or materials.
9. Grail imports the sealed artifact with `preprod_gate.py export`.
10. Enterprise deployment installs only from the sealed platform wheelhouse
    with `--no-index`; live dependency resolution is not seaworthy.
11. Preprod control files remain under `.ring/` and `.github/workflows/`; they
   cannot enter the Grail payload.
12. The final Beta version is already in the artifact. Grail never edits
    `VERSION` or `brainstem.py` after Preprod.

## Stage a candidate

```bash
gh workflow run stage-preprod.yml -R kody-w/rapp-canary --ref main \
  -f qualification_run_id=<green-pre-grail-run> \
  -f beta_preflight_run_id=<green-beta-main-preflight-run> \
  -f rollback_ref=brainstem-vX.Y.Z \
  -f soak_evidence_url=https://github.com/<org>/<repo>/issues/<evidence> \
  -f owner=<accountable-team> \
  -f model_id=<explicit-production-model>
```

The workflow:

1. validates qualification, Beta, rollback, and soak evidence
2. exports exact qualified bytes into a Grail-shaped candidate
3. records the rollback release's `rapp/1:brainstem` frame
4. packages a deterministic artifact and `rapp/1:readiness` manifest
5. verifies the artifact on Windows, macOS, and Linux
6. pauses at the protected `preprod` environment
7. seals the approved manifest and publishes build provenance

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

python3 <canary-checkout>/.ring/tools/preprod_gate.py verify \
  --artifact /path/to/rapp-preprod-<sha>.tar.gz \
  --manifest /path/to/seaworthy.json \
  --material dependency-material-linux=/path/to/dependency-material-linux.tar.gz \
  --material dependency-material-macos=/path/to/dependency-material-macos.tar.gz \
  --material dependency-material-windows=/path/to/dependency-material-windows.tar.gz

python3 <canary-checkout>/.ring/tools/preprod_gate.py export \
  --artifact /path/to/rapp-preprod-<sha>.tar.gz \
  --manifest /path/to/seaworthy.json \
  --rollback-frame /path/to/rollback-brainstem.json \
  --target /tmp/grail-release \
  --material dependency-material-linux=/path/to/dependency-material-linux.tar.gz \
  --material dependency-material-macos=/path/to/dependency-material-macos.tar.gz \
  --material dependency-material-windows=/path/to/dependency-material-windows.tar.gz
```

Inspect the staged tree, run Grail preflight, commit without modifying the
artifact-derived files, merge with human approval, and tag the release.
Enterprise deployment also consumes the sealed platform dependency bundle;
the public one-liner remains unchanged.

For an enterprise-managed runtime, unpack the matching dependency material and
prepare the entire runtime without consulting a package index:

```bash
python3 <canary-checkout>/.ring/tools/preprod_gate.py prepare-runtime \
  --artifact /path/to/rapp-preprod-<sha>.tar.gz \
  --manifest /path/to/seaworthy.json \
  --destination /opt/rapp/releases/<sha> \
  --state-dir /var/lib/rapp \
  --material dependency-material-linux=/path/to/dependency-material-linux.tar.gz \
  --material dependency-material-macos=/path/to/dependency-material-macos.tar.gz \
  --material dependency-material-windows=/path/to/dependency-material-windows.tar.gz
```

The command verifies provenance for the source, manifest, and every platform
material; selects the current platform; extracts into a new release directory;
and installs only from the sealed wheelhouse with `--no-index`.

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
python3 .ring/tools/brainstem_history.py record \
  --repo /path/to/rapp-installer \
  --release-ref brainstem-vX.Y.Z \
  --parent .ring/brainstem-history/brainstem-vPREVIOUS.json \
  --frame .ring/brainstem-history/brainstem-vX.Y.Z.json

python3 .ring/tools/brainstem_history.py verify \
  --repo /path/to/rapp-installer \
  --frame .ring/brainstem-history/brainstem-vX.Y.Z.json
```

Commit that frame to Canary. The tag restores the complete release; the frame
proves the exact `brainstem.py` contained by it and links it to the previous
known-good version.
