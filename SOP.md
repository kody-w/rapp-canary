# RAPP Release Train — Standard Operating Procedures

The single playbook for every way change moves (or is deliberately kept from
moving) through the train. This file is **payload**: it rides promotions so
every ring carries the same SOPs. Ring-local mechanics live in
`.ring/RUNBOOK.md` (canary); release mechanics for Grail live in
`RELEASING.md`. When those documents and this one disagree, fix the
disagreement in the same change — never leave them split.

The train: **Canary → Nightly → Alpha → Beta → Grail (human-only)**.
Everything enters at Canary. Rings only ever receive promotions.

**Grail is frozen production.** Real users run Grail in real time; it stays on
its stable released version. The pre-grail rings exist precisely so
development can be agile, experimental, and even deliberately broken (war
games) without EVER risking a Grail user. Nothing in this playbook flows to
Grail: routine promotion sweeps stop at Beta. A Grail release is a separate,
rare, human-only act (`RELEASING.md`) that the maintainer initiates
deliberately — never a side effect of anything below.

---

## §1 Mainline waves — improvements only

A **wave** is a batch of changes developed on Canary and intended for the
whole train.

**The mainline rule: NO NEW FEATURES ON THE TRAIN.** Waves may contain only:

- bug fixes and security fixes
- hardening (error paths, retries, gates, scrubbing)
- parity backports (`.sh` ↔ `.ps1`, local shim ↔ cloud contract)
- improvements to behavior that already exists (e.g. making the existing LAN
  mode actually usable is an improvement; adding a new endpoint is not)
- tests, and docs that correct factual drift

Anything that adds a new endpoint, UI surface, or capability is a **feature**
and belongs in an experimental flight (§4) until it has earned a deliberate,
human-approved graduation (§4.4). When unsure which side of the line a change
is on, treat it as a feature.

**Wave mechanics** (per `.ring/RUNBOOK.md` §1):

1. Branch off canary `main` (`fix/…`, `harden/…`, `ops/…` — never `flight/…`).
2. Push; preflight must go green (8 jobs: static + fresh/upgrade e2e on
   macOS/Ubuntu/Windows + fresh-nopip).
3. Dual review gate (§5) for any non-trivial wave.
4. Merge `--no-ff` to canary main; main preflight green.
5. Promote edge-by-edge, qualify the whole train, archive evidence
   (RUNBOOK §2–3) — unless the wave is deliberately held on Canary (§2).

Every wave commit message records what proved it: suite counts, preflight run
ids, live-verification notes.

---

## §2 Divergence — deliberately holding a wave on Canary

Sometimes Canary intentionally runs ahead (testing the train itself, staging
a risky batch, waiting on soak). Divergence is a **declared state**, not an
accident:

1. **Declare it**: note in the wave's merge commit that it will not promote
   ("canary-only until <condition>").
2. **Freeze the outer rings**: while diverged, nothing lands on
   Nightly/Alpha/Beta — including `.ring/` housekeeping. A moved Beta tip
   invalidates the last green qualification, which mid-divergence cannot be
   re-earned.
3. **Expect qualification red**: whole-train qualification compares payload
   digests across rings; a diverged canary makes it structurally red. That is
   the correct signal, not a failure to fix.
4. **Ending divergence** is a normal promotion sweep (RUNBOOK §2) followed by
   qualification + evidence archive. Promote the accumulated delta as ONE
   sweep; do not interleave other work mid-sweep.

While diverged, the last green qualification run (recorded in
`.ring/attestations/`) is the only releasable evidence — protect it (§2.2).

---

## §3 Hotfix lanes — shipping a fix when the train is diverged

The scenario: a production-severity bug lives in the payload the outer rings
hold, and Canary has moved on. Two sanctioned lanes:

### §3.1 Backport lane (fix must reach Beta; Grail can wait)

1. Find the last-promoted canary commit: `source.commit` in the target ring's
   `.ring/upstream.lock.json`.
2. `git checkout -b hotfix/<name> <that commit>` on canary; apply the
   **minimal** fix; push (preflight runs on the branch).
3. Dual review (§5) — hotfixes are exactly when review discipline pays.
4. Promote from the hotfix checkout edge-by-edge (RUNBOOK §2). The promotion
   tool syncs trees and does not require the source to be canary main.
5. Mark every promotion commit as a backport:
   `promote: canary -> nightly (BACKPORT hotfix/<name>, canary main diverged)`.
6. Verify the fix is live on the outer ring (curl the soaked/local build).
7. **Re-enter at Canary immediately**: cherry-pick or re-apply the fix onto
   canary main in the same working session — otherwise the next full
   promotion silently reverts it (the oldest ring bug there is).

### §3.2 Grail-direct lane (production emergency)

Per `RELEASING.md`: hotfix Grail on a release branch, full checks, human
merge, tag. Then **immediately** run the re-seed ritual (RUNBOOK §1) merging
grail main back into canary main. Mid-divergence this WILL conflict with the
unpromoted canary work — resolve keeping BOTH the hotfix and the canary
changes, and treat the incoming Grail `VERSION` bump as authoritative.

**Never** land a fix by committing directly to Nightly/Alpha/Beta. Ring mains
only receive promotions; anything else is silently overwritten by the next
promotion's tree sync, with no warning.

---

## §4 Experimental flights — new features, fully isolated

A **flight** is a new-feature experiment that must be runnable on the
maintainer's device but can never reach the train, no matter what else is
being pushed.

### §4.1 Isolation model

- A flight is a branch on canary named `flight/<name>`, never merged.
- Every flight carries **`FLIGHT.json`** at the repo root — the marker is the
  isolation mechanism:
  - **Gate 1 — preflight**: on `main`, preflight fails if `FLIGHT.json`
    exists (a flight merged by mistake turns main red within minutes).
  - **Gate 2 — promotion**: `promote_ring.py` refuses any source tree
    containing `FLIGHT.json`, so even a red-main mistake cannot ride an edge.
- Flights still get CI: preflight runs on every `flight/*` push, so an
  experiment is continuously install-tested without touching the train.

`FLIGHT.json` schema:

```json
{
  "schema": "rapp-flight/1",
  "name": "voice-first",
  "port": 7081,
  "description": "one line: what this experiment explores",
  "env": {"VOICE_MODE": "true"},
  "base_commit": "<canary main commit the flight branched from>"
}
```

### §4.2 Running a flight on-device

Operators run flights from the canary checkout with the ring tool:

```bash
.ring/tools/flight.sh list                 # flights on origin
.ring/tools/flight.sh start voice-first    # worktree + venv + launch
.ring/tools/flight.sh status voice-first   # health + port + log tail
.ring/tools/flight.sh stop voice-first
```

Isolation on device: each flight gets its own home
(`~/.brainstem-flights/<name>/`), its own venv, its own data dir, and its own
port (from `FLIGHT.json`) — it never touches `~/.brainstem` (the daily
driver) or port 7071. Auth is borrowed read-only: the runner copies the daily
driver's `.copilot_token` into the flight home if present.

### §4.3 Flight hygiene

- Rebase a long-lived flight onto canary main periodically; a flight that no
  longer rebases cleanly is a signal to graduate or retire it.
- Flights are experiments: v0 quality is acceptable, but preflight must stay
  green and the flight must actually run.

### §4.4 Graduation (the only way flight work reaches the train)

Wholesale merge of a flight is forbidden (and gated). To adopt proven flight
work: extract the specific changes into a normal wave branch, **reframed as
the smallest mainline-shaped change**, with the maintainer's explicit
approval that it is no longer experimental. The flight branch is then retired
(deleted or archived with a `retired/` prefix).

---

## §5 Dual-model review gate — "done" requires two signatures

For every non-trivial wave, hotfix, or SOP change:

1. **Build** (Claude / Fable 5 in this train's practice): implement, test,
   live-verify. Verification means exercising the artifact, never inferring
   from a green build.
2. **Independent review** (GitHub Copilot CLI, GPT-5.6 Sol, maximum context):
   the full diff plus the claim list ("what this change asserts about
   itself") goes to the reviewer with instructions to refute, not affirm.
3. **Reconcile**: every reviewer finding is either fixed or answered with
   evidence. Rounds continue until BOTH models explicitly agree everything is
   satisfied — neither can outvote the other; disagreement means another
   round.
4. **Record**: the merge commit (or review file for larger waves) carries
   both verdicts: `Reviewed-by: Copilot CLI GPT-5.6 Sol (agreed round N)` and
   the builder's sign-off.
5. **Only then notify the maintainer** that the work is done. A single-model
   "done" is not done.

---

## §6 War-gaming the train

Divergence and hotfix lanes are rehearsed deliberately (the playbook lives
with the wave that runs it). Conduct rules:

- Declare the game window; freeze the outer rings for its duration (§2).
- Record every operator move and both shared-digest states before/after in
  an append-only ledger committed with the evidence.
- A game that "fails" (tooling breaks, evidence dies, operators get confused)
  is a SUCCESSFUL game — file the gap it exposed before fixing anything.
- End every game by re-converging the train (§2.4) and writing what the
  runbooks should now say.

---

## §7 Cadence and growth — rings are audiences, not calendar slots

The instinct to add weekly/monthly/yearly rings is answered here so it isn't
re-litigated: **no new rings without a new audience.** A ring earns its
existence only when a distinct group of consumers needs a distinct promise
(Chrome runs four channels with thousands of engineers; Rust runs three).
More rings for one maintainer means more repos to freeze, more edges to
qualify, more divergence surface — less resilience, not more.

What weekly/monthly/yearly actually encode at Node/Linux scale is **cadence
and support**, which the existing train expresses without new
infrastructure:

- **Cadence (schedule, not repos)**: Canary moves continuously; promote
  Canary→Nightly at most daily; sweep through Beta roughly weekly when not
  deliberately diverged; Grail moves only when the maintainer decides a
  soaked, qualified payload has earned it.
- **Support (tags, not repos)**: every Grail release is a permanent tag;
  users pin back with `BRAINSTEM_VERSION`. When real users need long-lived
  versions, designate a Grail tag as LTS and service it via the backport
  lane (§3.1) — that is Linux-style longevity with zero new rings.
- **Growth rule**: when the project has genuinely distinct consumer cohorts
  (e.g. an enterprise cohort that wants monthly), add ONE ring for that
  audience, driven by their demand — never speculatively.

## §8 Evidence and notification

- Preflight run ids, qualification run ids, and attestation archives are the
  currency of trust — cite them in commits; never claim green without one.
- The maintainer is notified when: a wave completes the dual gate and its
  train cycle; a hotfix is verified live on its target ring; a flight is
  ready to try; or any gate goes red for a reason the runbooks don't cover.
