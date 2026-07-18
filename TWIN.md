# TWIN — a twin in residence for any repo

`.twin/` is to a repo what `.claude/` is: project-scoped identity, agents, and
memory that live WITH the project. The difference: a twin is not config for
one tool — it is a running, project-specialized brainstem that ANY AI (Copilot
CLI, Claude, anything that can POST JSON) invokes over the same `/chat`
contract as the daily driver.

## Layout — two layers: PUBLIC travels, PRIVATE stays on device

```
.twin/
  twin.json          manifest: name, description, port        (public, committed)
  soul.md            identity + project knowledge, safe to share (public, committed)
  agents/            *_agent.py safe to share                 (public, committed)
  memories/          seed knowledge notes                     (public, committed)
  private/           ── ON-DEVICE ONLY, gitignored, NEVER travels ──
    soul.md          sensitive knowledge, real names, secrets → appended to public soul
    agents/          private specialist agents → override public on same filename
    engine/          runtime + MEMORY DATA (what the twin learns) + logs
```

**Public layer** = the twin's portable brain, versioned with the project, safe to
push. The whole `.twin/` directory can travel in a public repo carrying only this.

**Private layer** (`.twin/private/`, gitignored) = everything sensitive that must
stay on THIS machine. At launch `twin.sh` layers private OVER public:
`private/soul.md` is appended to the public soul, `private/agents/*.py` override
public agents of the same name, and learned memory lives in `private/engine/`.
So a repo can ship a useful public twin while its secrets, private specialists,
and real memory never leave the device.

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
