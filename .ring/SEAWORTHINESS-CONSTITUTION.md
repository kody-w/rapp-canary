# RAPP/1 Seaworthiness Constitution

> The product may be inventive, fast, and autonomous. It may not be unsafe to
> operate. Seaworthiness is the highest release invariant: if it is not proven,
> the product does not sail.

## Preamble

RAPP products are built from frames that become durable capabilities and visible
tiles. Those products may coordinate agents globally and act in real systems.
Their usefulness therefore depends on more than whether a feature works once.
They must preserve identity, data, authority, compatibility, observability, and
recovery under the conditions in which enterprises actually operate.

This constitution governs the release control plane. It is Canary-owned under
`.ring/` and `.github/workflows/`; those paths are excluded from shared payload
promotion and Grail export. It treats the pinned Grail
`rapp_brainstem/brainstem.py` bytes as permanently immutable.

## Article I — Seaworthiness Is Supreme

1. A release is not production-ready because it builds, demos well, or passes a
   happy-path test.
2. A release is **seaworthy** only when current, machine-verifiable evidence
   proves every mandatory control.
3. A failed, missing, stale, ambiguous, or unmeasured control is a failure.
4. Schedule pressure, feature value, token cost, and executive preference cannot
   waive a blocking safety control.
5. Every readiness claim expires. Changed code, dependencies, models, policy,
   configuration, environment, or artifact bytes require requalification.

## Article II — The Grail Kernel Never Changes

1. `kody-w/rapp-installer` is the global Grail. Its Brainstem shape is a
   compatibility contract depended on by downstream products.
2. `rapp_brainstem/brainstem.py` is permanently pinned by repository, release
   scope, immutable ref, commit, Git blob, raw SHA-256, byte length, and the
   RAPP/1 domain-separated `grail_id`. It may not change under that identity.
3. Canary-owned control-plane files remain under `.ring/` and
   `.github/workflows/`, which promotion and Grail export exclude.
4. Every ring may experiment, but qualification and Preprod reject
   `kernel-drift`; they may not certify or promote a candidate whose Brainstem
   differs from the constitutional pin.
5. A release pipeline may not test one Brainstem and substitute the Grail bytes
   afterward. The exact Grail-shaped artifact containing the immutable kernel
   must pass Preprod.
6. New behavior belongs in agents, adapters, configuration, frames, data, and
   external control planes. If the frozen kernel cannot express it, that is an
   explicit incompatibility—not permission to edit the Grail.
7. A successor kernel requires a new Grail identity and lineage; it cannot
   overwrite, retag, or silently redefine the existing Grail.
8. Every known-good Grail release has an immutable `rapp/1:brainstem` frame
   binding its release tag, commit, tree, `brainstem.py` blob, file hash, version,
   and predecessor.
9. RAPP/1 §11.1 and Protocol Constitution Article 15 are the normative source
   for this invariant. The exact authority bytes are pinned in
   `preprod-policy.json`; moving documentation cannot weaken the gate.

## Article III — One Artifact Crosses the Harbor

1. Qualification selects one exact Beta payload.
2. Preprod packages that payload once into a deterministic artifact.
3. The source artifact and platform dependency bundles are content-addressed by
   SHA-256 and receive build provenance.
4. Every Preprod test runs against that sealed release set.
5. Grail and enterprise deployment consume the same source artifact and
   dependency materials. Rebuilding, patching, regenerating, or manually copying
   equivalent-looking files is forbidden.
6. Any byte difference creates a new candidate and restarts qualification.

## Article IV — Stage Duties and Side Effects

| Stage | Purpose | Allowed side effects | Required evidence | Forbidden |
|---|---|---|---|---|
| Flight / Dev | Discover and prototype | Isolated worktrees, disposable state, synthetic data, deliberate failure | Reproducible experiment record | Train promotion, shared credentials, customer data |
| Canary | Integrate and attack changes | Canary-only deployment, real auth in isolated soak, diagnostics | Full CI, adversarial review, live proof, rollback plan | Silent divergence, unreviewed main merge |
| Nightly | Repeatability | Automated promotion of exact shared blobs | Green ring CI and parent lock | Direct feature commits, rebuilds |
| Alpha | Broad compatibility | Integration and policy testing | Green ring CI and intact attestation ancestry | Payload mutation |
| Beta | Frozen release candidate | Release-candidate testing and soak | Green Beta CI, exact shared digest, current main commit | New features, opportunistic fixes |
| Preprod | Production rehearsal | Isolated production-like deployment using synthetic or approved test data | Signed readiness manifest, cross-platform artifact verification, rollback target, human approval | Rebuilds, mutable inputs, production customer data by default |
| Grail | Production | Human-approved release of the sealed artifact | Seaworthy manifest, immutable tag, provenance, post-release smoke | Automatic promotion, untagged releases, control-plane leakage |

Side effects must become more constrained as a candidate moves right. A stage may
observe more realistic conditions, but it may not gain broader mutation authority.

## Article V — Unknown and Unmeasured Are First-Class States

Unknown unknowns cannot be eliminated by listing them. They are managed by
systems that expose surprises before production:

1. Differential testing across operating systems, clean installs, upgrades,
   destructive repair, and rollback.
2. Fault injection for network loss, rate limits, expired credentials, corrupt
   state, permissions, disk exhaustion, clock skew, and process crashes.
3. Concurrency tests for duplicate workers, stale leases, retries, and partial
   completion.
4. Long-running soak with real authentication and representative workloads.
5. Independent adversarial review whose mandate is to refute release claims.
6. Runtime telemetry that reports absence and uncertainty rather than converting
   them into success-shaped defaults.
7. A visible `unknown` readiness state that blocks sealing.

## Article VI — State, Identity, and Tenant Safety

1. User state must live outside replaceable code.
2. Migrations are atomic, idempotent, and rollback-compatible.
3. Authentication failures distinguish invalid credentials from unavailable
   networks, missing entitlement, rate limits, and provider incidents.
4. Preprod uses isolated state, secrets, accounts, storage, and endpoints.
5. Production data is prohibited in Preprod unless an approved policy explicitly
   permits sanitized or synthetic equivalents.
6. Tenant identity and authorization boundaries are tested independently from
   functional correctness.

## Article VII — CI/CD Is an Enforcement System

CI/CD must:

1. Verify the exact commit and artifact under test.
2. Reject stale mirrors, mutable references, moved tags, and digest drift.
3. Execute native Windows PowerShell and Unix installers.
4. Test fresh install, normal upgrade, destructive repair, and rollback.
5. Prove live writers are quiesced without terminating unrelated processes.
6. Produce durable evidence: run URLs, attestations, artifact hashes, logs, and
   approval records.
7. Fail closed when a required tool, environment, credential, or observation is
   unavailable.
8. Never hold credentials capable of automatically promoting to Grail.
9. Run trusted control-plane Python in isolated mode and extract archives only
   through path-normalizing, cross-platform-safe code.
10. Accept only registry package requirements; keep runtime dependencies and
    test tooling in separate hash-bound wheelhouses.
11. Verify the staged tree, release commit tree, and final merge tree against
    the same sealed Git tree before Grail is pushed or tagged.
12. Execution gates must consume the exact verified kernel bytes from an
    immutable snapshot; hashing one path and later reopening it is insufficient.

## Article VIII — Brainstem Frames and Rollback

1. Every Grail tag is immutable.
2. Every tag receives a deterministic `rapp/1:brainstem` frame.
3. Frames form an append-only chain through the previous known-good frame hash.
4. A frame is invalid if its tag moves, commit changes, blob differs, hash fails,
   or parent link breaks.
5. The Preprod release set carries the complete verified frame chain and names
   one rollback frame.
6. Rollback is rehearsed by the frame's exact commit before release, not
   improvised through mutable name resolution during an incident.
7. Recovery restores the complete tagged release; `brainstem.py` is never
   restored alone when its surrounding contract differs.

## Article IX — Human Authority

1. Automation may build, test, attest, and stage Preprod.
2. A protected `preprod` environment records the approval that seals a candidate.
3. Grail remains human-only.
4. The approver receives artifact identity, evidence, known risks, owner,
   expiration, and rollback target.
5. Approval applies only to the displayed artifact digest.
6. Enterprise installations must assign an independent reviewer or team and
   prevent self-review. A personal reference repository may use owner approval,
   but that is not separation of duties.

## Article X — Operational Readiness

A seaworthy product has:

- a named owner and escalation path
- measurable availability, latency, correctness, and recovery objectives
- bounded retries, leases, timeouts, and resource consumption
- a kill switch and tested rollback
- tamper-evident audit history
- secret rotation and least privilege
- dependency and model provenance
- platform-specific, hash-bound dependency bundles installed without an index
- incident detection, diagnosis, and communication procedures

Passing functional tests without these controls is not seaworthiness.

## Article XI — The Seaworthy Frame

The release candidate is represented by `rapp/1:readiness`:

- exact Beta commit and immutable artifact digest
- expected Grail base commit and resulting Git tree
- critical `brainstem.py` hash and sealed dependency materials
- qualification and Beta preflight evidence
- hash-pinned soak evidence bound to the qualification commit, Beta commit,
  explicit model, elapsed duration, and accountable owner
- required control set
- issuance and expiry
- rollback frame
- Preprod run and human approver

Before approval its status is `preprod-candidate`. After every gate passes and
the protected environment is approved, it becomes `seaworthy`. A failed or stale
control changes the state to `degraded` or `revoked`; it never silently remains
green.

## Article XII — Amendment

This constitution may become stricter as new failure modes are discovered. It
may not be weakened to make a release pass. Any amendment must remain
control-plane-only, receive independent review, and include tests proving that it
cannot alter the Grail runtime payload.

---

*A ship that cannot prove it will return does not leave the harbor.*
