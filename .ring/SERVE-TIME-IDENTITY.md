# Serve-time identity

The pattern for anything ring-specific. Read this before adding a URL, a
banner, a check, or any behavior that differs between canary, nightly, alpha,
beta, and grail.

## The invariant

**Every byte in git says grail. Every byte a human or an AI is told to open
says ring.** Identity is applied once, at serve time, by the renderer. It is
never written into the shared payload.

That is why promotion and pull-down are byte copies. A ring never edits the
payload to become itself, so nothing has to be edited back when the payload
moves up or down the train.

```text
shared payload (grail identity)  --render_ring.py + this ring's .ring/ring.json-->  served tree (ring identity)
```

The served tree is what Pages publishes and what `flight.sh` runs. The payload
is what git stores and what promotion copies.

## Consequence: raw URLs are never entry points

`raw.githubusercontent.com/kody-w/<ring>/main/<file>` returns the payload
bytes, which carry grail identity no matter which ring's repository you fetch
them from. A reader (or an AI coach) following such a link will install or
describe grail, not the ring.

So:

- entry points are Pages URLs: `https://kody-w.github.io/<ring>/install.sh`,
  `.../skill.md`, `.../skills/rapp-bootstrap/SKILL.md`, and the landing page;
- in the grail payload, links between entry points use the grail Pages host
  (`kody-w.github.io/rapp-installer/...`), so the rewrite rule turns them into
  the ring's own Pages host on every ring;
- a raw URL is fine as the thing the renderer or a workflow reads. It is not
  fine as the thing a page tells a person to open.

This is how the installers always worked. In 2026-09 the AI onboarding entry
(`skill.md`) was linked raw from the landing page, so the canary site sent an
AI to a grail-identity playbook. That is the bug this document exists to stop
from recurring.

## The three mechanisms

| Mechanism | Lives in | What it is for |
|---|---|---|
| **Rewrites** (`rewrites[]` in `ring.json`) | ring-owned | Replace grail identifiers with ring identifiers across the served tree. Each rule carries `expected_count`, the drift oracle. |
| **Protected paths** (`protected_paths[]`) | ring-owned | Files that are the ring's own and never promoted or rendered: `.ring/`, `.github/workflows/`. Ring workflows, tools, tests, docs go here. |
| **Advisories** (`advisories[]`) | ring-owned | Ring-specific prose inserted into a served file at render time, after its front matter. For example, canary's "use the flight sandbox, not the production installer" notice at the top of the served `skill.md`. Grail declares none, so grail renders byte-identical to its payload. |

If a ring needs something that none of these covers, add a new `ring.json`
field and teach `render_ring.py` on the hub to honor it. Do not put the
ring-specific thing in the payload behind a conditional. Non-hub rings fetch
the renderer from the hub at publish time, so a renderer feature reaches every
ring without a payload change.

## The deployment gate

`.ring/tools/check_served_identity.py` proves the served artifacts point at
the right ring. `publish-pages` runs it twice:

1. **pre-deploy**, against the assembled site directory, so a wrong identity
   never leaves the runner;
2. **post-deploy**, against the live Pages origin, with retries for CDN
   propagation, so a stale or half-published site is caught as a red run.

It checks every entry point a reader is told to open: no grail identifier
survives, identity files name the ring's repository, and every `skill.md` link
on the landing page is this ring's Pages URL and never a raw blob. Extend
`ENTRY_POINTS` when a new thing becomes something readers are told to fetch.

## Adding a ring-specific thing: the checklist

1. Is it a URL or identifier? Write the grail form in the payload and rely on
   an existing rewrite rule. Confirm the rendered tree shows the ring form.
2. Is it prose only one ring should show? Put it in that ring's
   `advisories[]`. Never in the payload.
3. Is it a workflow, tool, test, or doc about the ring? Put it under `.ring/`
   or `.github/workflows/`. It is protected, so it does not ride promotion.
   If every ring needs it, copy it to each ring's overlay in the same cycle.
4. Is it something readers fetch? Serve it from Pages (add it to the site
   assembly step) and add it to the gate's `ENTRY_POINTS`.
5. Did the payload change add or remove grail identifiers? Recount. The
   oracle will refuse until `expected_count` matches, in **all four** rings'
   `ring.json`, because counts are payload-wide (see `RUNBOOK.md`).

## What rides promotion, what does not

| Path | Rides promotion | Why |
|---|---|---|
| everything outside protected paths | yes, byte-for-byte | shared payload, grail identity |
| `.ring/ring.json` | no | the ring's identity and rewrite counts |
| `.ring/tools/`, `.ring/tests/`, `.ring/*.md` | no | ring machinery; hub carries the renderer and gate, other rings fetch them |
| `.github/workflows/` | no | ring workflows; edit each ring's copy deliberately |

## Recount procedure

```bash
python3 .ring/tools/render_ring.py --repo . --config .ring/ring.json --output "$(mktemp -d)"
```

A clean worktree is required. On drift the renderer prints the needle, the
expected count, and the found count. Bump `expected_count` to the found value
only after confirming every new occurrence is intentional grail identity in
the payload, then repeat for the other rings when the payload reaches them.

## The two traps this came from

- **Raw skill link.** Fixed by linking `skill.md` through the grail Pages host
  in the payload, serving the rendered `skill.md` and bootstrap router at the
  site root, and gating deploys on served identity.
- **Ring installer over a production Brainstem.** A rendered ring installer
  still installs into `~/.brainstem`. On a machine with an existing Brainstem
  that is an in-place switch of production, and at an equal version it is a
  silent no-op that launches on an occupied port. Canary's advisory tells an
  AI coach to use the flight sandbox (`~/.rapp-flight/<ring>`, its own port and
  HOME) instead. The installer itself is grail payload and is not changed here.
