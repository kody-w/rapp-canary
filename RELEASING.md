# Releasing — how changes reach production without breaking it

> `main` **is** production. The install one-liners pull from it the moment you push.
> There is no staging environment between you and every user's machine — so we built
> one: the **preflight gate**. Nothing merges to `main` without passing it.
>
> Treat this repo like a kernel: userspace never breaks. "Userspace" here means
> the `/chat` request/response contract, the installer one-liners, the on-disk
> layouts (`~/.brainstem`, `.env`, `.copilot_token`, `.brainstem_data/`), and the
> agent contract (`*_agent.py` + `BasicAgent` + `perform()`). A release may fix,
> harden, or polish — it may never change what users and agents already rely on.

## The pipeline at a glance

```
branch  →  local checks  →  local preflight  →  push branch  →  CI preflight  →  release
 (never    (pytest, syntax)  (real install      (never main)    (7 fresh VMs:     (tag + merge
  main)                       in a sandbox)                      win/mac/linux
                                                                 × fresh/upgrade)   to main)
```

Every stage runs the **real, unmodified installers** — the same bytes users run —
against the candidate branch masquerading as `main` (a local bare repo + a
`git url.insteadOf` rewrite). The sacred one-liners are never edited for testing.

## 1. Branch

```bash
git checkout -b fix/whatever origin/main
```

Never commit to `main`. Never push to `main` except the release merge (step 6).

## 2. Local checks (seconds)

```bash
cd rapp_brainstem
~/.brainstem/venv/bin/python -m pytest test_local_agents.py test_model_selection.py -q
bash -n ../install.sh && bash ../tests/test_installer.sh
```

If you touched a `.ps1`, parse it (any pwsh, or let CI's PS 5.1 analyzer catch it):

```powershell
[System.Management.Automation.Language.Parser]::ParseFile("install.ps1",[ref]$null,[ref]$e); $e
```

## 3. Local preflight (~3 minutes)

```bash
bash tests/preflight_local.sh fresh            # factory-machine install of this checkout
bash tests/preflight_local.sh upgrade          # production user upgrading to this checkout
bash tests/preflight_local.sh upgrade --auth   # + a REAL authenticated /chat round-trip
```

This installs the current checkout through the real `install.sh` inside a throwaway
`$HOME` in `/tmp`, on port 7091. It cannot touch your real `~/.brainstem` and cannot
kill a server on 7071 (the installer's `lsof` is shimmed out inside the sandbox).
It asserts: server boots, `/health` reports the candidate version and bundled agents,
the web UI serves, `/chat` fails as JSON (never a crash), and — in `upgrade` — that a
custom agent, an edited `soul.md`, and an edited `.env` all **survive the upgrade**.

`--auth` copies your real Copilot token into the sandbox for one true end-to-end
`/chat` answer. The token never leaves the sandbox; the sandbox is disposable.

## 4. Push the branch → CI preflight (~10 minutes, 7 real machines)

```bash
git push -u origin fix/whatever
gh run watch   # or watch the "preflight" workflow in the Actions tab
```

`.github/workflows/preflight.yml` runs automatically on every non-main push:

| Job | What it proves |
|-----|----------------|
| `static` | bash + PowerShell syntax, **PS 5.1 compatibility** (what Windows users actually run), py_compile, full pytest suite |
| `e2e` win/mac/linux × fresh | The one-liner takes a **factory VM** all the way to a serving brainstem |
| `e2e` win/mac/linux × upgrade | An **existing production install** upgrades cleanly; user agents/soul/.env survive |

The e2e jobs run `install.ps1` under **Windows PowerShell 5.1** (not pwsh) because
that is what `irm | iex` uses on a stock Windows machine. GitHub auth endpoints are
black-holed in the VM's hosts file, which also proves the installer degrades
gracefully with no network to GitHub auth (it must skip to launch, never hang or die).

**All 7 jobs green = the branch is releasable.** Any red = fix on the branch, push
again. `main` was never at risk.

## 5. Optional: manual wild check

For risky changes, before merging:

- Drive the candidate's web UI by hand. `bash tests/preflight_local.sh fresh` keeps
  the sandbox **files** on disk (its path is printed at the end) but stops the
  server on exit — relaunch it, then click around:

  ```bash
  S=/tmp/brainstem-preflight-XXXXXX/home   # printed by the preflight run
  HOME="$S" PORT=7091 "$S/.brainstem/venv/bin/python" "$S/.brainstem/src/rapp_brainstem/brainstem.py"
  ```

  Open `http://localhost:7091` — chat, switch models, open the panels.
- Re-run the Windows leg on demand: `gh workflow run preflight --ref fix/whatever`.

## 6. Release (the only push to main)

```bash
VERSION=X.Y.Z   # bump rapp_brainstem/VERSION in a release commit on the branch
echo "$VERSION" > rapp_brainstem/VERSION
cp install.sh docs/install.sh              # GitHub Pages serves docs/ — the advertised
cp install.ps1 docs/install.ps1            # one-liners pull THESE mirrors, so a release
cp install.cmd docs/install.cmd            # touching an installer must sync them
cp install.command docs/install.command
git commit -am "release: v$VERSION"
git push                                   # CI preflight runs once more on the final bytes

git checkout main && git pull origin main
git merge --no-ff fix/whatever -m "release: v$VERSION"
git tag "brainstem-v$VERSION"              # tags are immutable rollback points
git push origin main --tags
```

Rules:
- The release commit bumps `rapp_brainstem/VERSION` — installed machines discover
  the upgrade by comparing this file.
- Every release gets a `brainstem-vX.Y.Z` tag. Tags are never moved or deleted.
- Merge only a branch whose **final commit** passed the full preflight.
- The advertised one-liner (`kody-w.github.io/rapp-installer/install.sh`) is GitHub
  Pages serving `docs/install.sh` — a byte-for-byte mirror of the root installer.
  If the mirrors drift, users install different bytes than the repo tests.

## 7. Post-release smoke (~3 minutes)

Immediately after pushing, verify production the way a user experiences it:

```bash
git checkout main
bash tests/preflight_local.sh fresh    # this checkout == production now
```

and confirm the served one-liner is the new bytes:

```bash
curl -fsSL https://raw.githubusercontent.com/kody-w/rapp-installer/main/rapp_brainstem/VERSION
```

(The Pages copy at kody-w.github.io can lag raw.githubusercontent by a few minutes.)

## 8. If production breaks anyway — rollback

Two independent levers, use either or both:

**Roll main back (fixes all future installs/upgrades):**

```bash
git checkout main
git revert -m 1 <merge-commit>     # or: git reset --hard brainstem-vPREV && git push --force-with-lease
git push origin main
```

Prefer `revert` — history stays honest and no force-push is needed.

**Pin an affected user to a known-good version (fixes one machine now):**

```bash
curl -fsSL https://kody-w.github.io/rapp-installer/install.sh | bash -s -- --version vX.Y.Z
```

Tags make every past release reinstallable forever. That is the safety net that
makes shipping polish low-fear: the worst bad push costs one `git revert` plus a
few minutes, not the product.

## Why this works

- **The test artifact is the production artifact.** Preflight never tests a copy of
  the logic — it executes `install.sh` / `install.ps1` byte-for-byte as users will.
- **Both user populations are covered.** `fresh` = the next new user; `upgrade` = every
  existing user. The upgrade leg asserts their files survive, which is the promise
  that matters most.
- **Windows is first-class.** PS 5.1 syntax gating + a real PS 5.1 end-to-end install
  on every push, because most users are on the PowerShell path.
- **Failure modes are rehearsed.** Auth endpoints are deliberately unreachable in CI;
  a candidate that hangs or dies without GitHub auth fails preflight.
