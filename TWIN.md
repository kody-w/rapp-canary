# TWIN — a twin in residence for any repo

`.twin/` is to a repo what `.claude/` is: project-scoped identity, agents, and
memory that live WITH the project. The difference: a twin is not config for
one tool — it is a running, project-specialized brainstem that ANY AI (Copilot
CLI, Claude, anything that can POST JSON) invokes over the same `/chat`
contract as the daily driver.

## Layout

```
.twin/
  twin.json        manifest: name, description, port          (public, committed)
  soul.md          the twin's identity + project knowledge    (public, committed)
  agents/          *_agent.py specific to THIS repo           (public, committed)
  memories/        seed knowledge notes                       (public, committed)
  private/         runtime engine, logs, MEMORY DATA          (gitignored, on-device)
```

Public = the twin's portable brain, versioned with the project. Private = what
it learns and holds on THIS device (`.twin/private/` is gitignored — memories
never leave the machine).

## Run it

```bash
rapp_brainstem/twin.sh init            # scaffold .twin/ into any repo
rapp_brainstem/twin.sh up              # launch (own port, isolated memory)
rapp_brainstem/twin.sh status
rapp_brainstem/twin.sh down
```

Isolation trick: the engine is symlinked into `.twin/private/engine/`.
Brainstem anchors its data dir at its own script path without resolving
symlinks, so the twin's `.brainstem_data/` (memories) lands inside
`.twin/private/engine/` — never in the daily driver's. The twin gets the
repo's own `rapp_brainstem/` payload if present, else the daily driver's
(`~/.brainstem/src/rapp_brainstem`). Auth is borrowed read-only (a COPY of
`.copilot_token`, so token refresh never races the daily driver).

Project agents in `.twin/agents/` are overlaid on the engine's agent set at
launch (same filename wins), so a twin keeps ManageMemory/ContextMemory plus
its own specialists. Agents can locate their repo via `TWIN_REPO_ROOT` env.

## Universal invocation — any AI, no SDK

```bash
curl -s -X POST http://localhost:7091/chat \
  -H 'Content-Type: application/json' \
  -d '{"user_input": "what is the state of the train?"}'
```

That is the entire integration surface. Tell Copilot "the project twin is on
:7091, POST /chat" and it can converse with the resident expert for this repo.

## The global brainstem can drive twins

`twin_connector_agent.py` (TwinConnector) gives the daily driver — and
therefore every AI already talking to it — the verbs `discover`, `up`,
`chat`, `down` over any repo's twin. "Spin up the canary twin and ask it
whether the train is diverged" becomes a single tool call.

## Per-repo, per-ring shaping

Every repo shapes its own twin (soul + agents + seed memories), the way
different projects shape `.claude/` differently. Ring repos can carry
ring-specialized twins — rapp-canary's twin (this repo, `.twin/`) knows the
train, the five verbs, and the freeze rules; a beta twin would answer for
what beta currently holds.

## v0 limits (this flight)

- `twin.sh` is bash (macOS/Linux); Windows launcher is future work.
- Seed memories ship in `soul.md` / `memories/` notes, not pre-hydrated
  `.brainstem_data` (the memory format is engine-internal).
- One twin per repo, one port each (`twin.json`).
