# RAPP Brainstem

A local-first AI agent server. One dependency: a GitHub account with Copilot access.

The brainstem runs on your machine, uses GitHub Copilot as the LLM, auto-discovers agents from Python files, and exposes a chat API + web UI on `localhost:7071`. Core chat needs no provider API key or cloud setup beyond GitHub Copilot; optional Azure Speech and ElevenLabs voice integrations use their own credentials.

---

## Install

### One-liner (recommended)

**macOS / Linux:**
```bash
curl -fsSL https://kody-w.github.io/rapp-installer/install.sh | bash
```

**Windows (PowerShell):**
```powershell
irm https://raw.githubusercontent.com/kody-w/rapp-installer/main/install.ps1 | iex
```

The installer handles Python 3.11, Git, cloning, pip deps, and the `brainstem` CLI command. Re-running the same one-liner auto-upgrades if a newer version is available.

### Manual

```bash
git clone https://github.com/kody-w/rapp-installer.git ~/.brainstem/src
cd ~/.brainstem/src/rapp_brainstem
pip3 install -r requirements.txt
```

---

## Quickstart

```bash
# 1. Start the brainstem
brainstem            # or: cd rapp_brainstem && ./start.sh

# 2. Open the UI and sign in with GitHub when prompted
open http://localhost:7071
```

If `gh` is not installed, the web UI at `localhost:7071` walks you through GitHub device-code login automatically.

Normal startup uses `launch.py` to compose explicitly enabled provider plugins
around the unchanged kernel. The Responses adapter supports eligible models
such as GPT-6 Astra in the existing model picker with the same Copilot
authentication. Direct `python brainstem.py` remains a legacy, kernel-only path.
See [ProviderTransport v1](PROVIDERS.md) for the plugin contract and activation.

---

## API Reference

### `POST /chat`

The main conversation endpoint. Sends user input through the LLM with tool-calling support. Up to 3 rounds of agent calls per request.

**Request:**
```json
{
  "user_input": "What's on Hacker News today?",
  "conversation_history": [],
  "session_id": "optional-session-id"
}
```

**Response:**
```json
{
  "response": "Here are today's top stories...",
  "session_id": "abc-123",
  "agent_logs": "[HackerNewsAgent] Fetched 10 stories"
}
```

| Field | Type | Description |
|-------|------|-------------|
| `user_input` | string | **Required.** The user's message. |
| `conversation_history` | array | Optional. Previous messages (`role` + `content`). |
| `session_id` | string | Optional correlation identifier returned in every response. Clients must resend `conversation_history`; the server does not persist conversations by ID. |

### `POST /chat/stream`

Streaming counterpart to `/chat`. Accepts the same JSON body and returns
server-sent events. It is POST-only because a request may invoke agents and
therefore is not a safe, side-effect-free GET.

### `GET /health`

Returns server status, loaded agents, model, and auth state.

```json
{
  "status": "ok",
  "version": "0.1.0",
  "model": "gpt-4o",
  "soul": "./soul.md",
  "agents": ["HelloAgent", "HackerNewsAgent"],
  "copilot": "✓",
  "endpoint": "https://api.individual.githubcopilot.com"
}
```

Returns `"status": "unauthenticated"` (still 200) if the Copilot token is missing — the web UI detects this and shows the login overlay.

### `GET /version`

```json
{ "version": "0.1.0" }
```

### `GET /models`

Lists available models and the current selection.

```json
{
  "models": [
    {"id": "gpt-4.1", "name": "GPT-4.1"},
    {"id": "gpt-4o", "name": "GPT-4o"},
    {"id": "gpt-4o-mini", "name": "GPT-4o Mini"},
    {"id": "claude-sonnet-4", "name": "Claude Sonnet 4"}
  ],
  "current": "gpt-4o"
}
```

### `POST /models/set`

Switch the active model at runtime.

```json
{ "model": "gpt-4o-mini" }
```

### `POST /login`

Starts GitHub device-code OAuth. Returns a `user_code` and `verification_uri` for the user to enter at github.com/login/device.

### `POST /login/poll`

Polls for completed device-code authorization. Returns `{"status": "pending"}` until the user completes login, then `{"status": "ok"}`.

### `GET /login/status`

Returns current authentication status.

### `GET /`

Serves the built-in chat web UI.

---

## Configuration

All config is via environment variables in `.env` (auto-created from `.env.example` on first run).

| Variable | Default | Description |
|----------|---------|-------------|
| `GITHUB_TOKEN` | *auto-detected* | GitHub PAT or Copilot token. Auto-detected from `gh auth token` if blank. |
| `GITHUB_MODEL` | `auto` | `auto` picks the highest Claude Haiku your account can use — fastest responses (falling back to the highest Sonnet, then `gpt-4o`), or pin a specific id. A model picked in the web UI is remembered (`.brainstem_model`) and overrides this. Changeable at runtime via `/models/set` (`"model": "auto"` re-selects). |
| `BRAINSTEM_PROVIDER_PLUGINS` | *runtime profile* | Optional comma-separated provider IDs; `none` disables provider plugins. Restart after changing it. Installed third-party plugins are never activated implicitly. |
| `SOUL_PATH` | `./soul.md` | Path to the system prompt file. |
| `AGENTS_PATH` | `./agents` | Directory to discover `*_agent.py` files from. |
| `SKILLS_PATH` | `./skills` | Directory to discover Markdown skill files from. |
| `PORT` | `7071` | Server port. |
| `BRAINSTEM_LAN_MODE` | `false` | Set `true` to bind all interfaces. Non-loopback capability routes require the per-install secret. |
| `BRAINSTEM_ALLOWED_HOSTS` | *(empty)* | Optional comma-separated LAN hostnames. Loopback and private IP literals are handled automatically. |
| `VOICE_ZIP_PASSWORD` | *(empty)* | Optional password used to unlock the encrypted `voice.zip` config for Azure Speech or ElevenLabs at startup. |

### LAN access

The secure default is `127.0.0.1`; the brainstem is not reachable from other
machines. To opt in, set `BRAINSTEM_LAN_MODE=true` and restart. The server prints
the per-install secret stored in `.brainstem_secret`. Non-loopback API clients
must send it on capability-bearing routes:

```bash
curl http://192.168.1.20:7071/health \
  -H "X-Brainstem-Secret: $(cat .brainstem_secret)"
```

Private IP hosts are accepted automatically in LAN mode. If clients use a named
host, add it to `BRAINSTEM_ALLOWED_HOSTS`. The bundled browser UI remains
zero-configuration on the same machine; authenticated LAN automation should use
the API header.

---

## Authentication

The brainstem uses GitHub Copilot's API — no OpenAI keys needed. It resolves a GitHub token through this chain (first match wins):

1. **`GITHUB_TOKEN` env var** — set in `.env` or your shell
2. **`.copilot_token` file** — saved automatically after device-code login
3. **`gh auth token` CLI** — only when it returns a Copilot-compatible token; the common `gho_` token is ignored

The GitHub token is exchanged for a short-lived Copilot API token (auto-refreshed, cached to disk across restarts). If the token expires and a refresh token is available, it auto-refreshes without user interaction.

**Device-code login (no `gh` needed):**

Open `localhost:7071` in a browser. If not authenticated, the UI shows a login overlay. Click "Sign in" → enter the code at `github.com/login/device` → done. The token persists across restarts.

---

## Writing Agents

Agents are Python files named `*_agent.py` in the `AGENTS_PATH` directory. Each agent extends `BasicAgent`, declares metadata (OpenAI function-calling schema), and implements `perform()`.

### Minimal example

```python
# agents/greeting_agent.py
from basic_agent import BasicAgent

class GreetingAgent(BasicAgent):
    def __init__(self):
        self.name = "GreetingAgent"
        self.metadata = {
            "name": self.name,
            "description": "Greets a user by name.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "The person's name"}
                },
                "required": ["name"]
            }
        }
        super().__init__()

    def perform(self, name="", **kwargs):
        return f"Hello, {name}! Welcome to the brainstem."
```

### How discovery works

1. On every `/chat` request, the brainstem scans `AGENTS_PATH` for `*_agent.py` files (top-level only — subdirectories are excluded).
2. Each file is loaded and inspected for classes with a `perform()` method.
3. Matching classes are instantiated and registered as OpenAI function-calling tools.
4. The LLM decides when to call them based on the `description` in `metadata`.

**Stateless by design:** Agents load fresh every request. Edit a file, hit the endpoint, see the change. No restart needed.

### Hot-loading Markdown skills

Drag a `SKILL.md`, `skill.md`, `skills.md`, or another `.md` skill file into the
chat window, exactly like a `.py` agent. The same drop control routes Markdown
to the skills adapter and Python to the existing agent importer.
**LearnNew is included out of the box.**

**Store as Markdown (default):** `.md` files go into `skills/`, parallel to
Python files in `agents/`. You can also copy a Markdown file directly into
`SKILLS_PATH`. Top-level `.md` files are discovered fresh, so additions, edits,
and deletions take effect without a restart. Skills survive refresh, new
conversations, and server restarts; clearing a chat does not delete them.

The model sees the installed skills' names and descriptions and uses LearnNew's
`use` action to read a relevant skill's current instructions. Markdown stays
Markdown; it is not automatically compiled, executed as Python, or copied into
a browser-side skill store.

**Convert to agent (explicit):** Choose **Convert to agent** in the Agents panel
or ask LearnNew to convert a stored skill. The complete Markdown becomes its
specification for generating a real `*_agent.py`, through the existing Python
hot-import path. The original Markdown remains in `skills/`.

Conversion uses Brainstem's authenticated model connection. No separate Copilot
CLI or API key is required. Storing Markdown and importing Python do not make a
code-generation model call.

Optional YAML frontmatter supplies the skill's name and description:

```markdown
---
name: release-notes
description: Write release notes from a list of changes.
---

Group the supplied changes into Features, Fixes, and Breaking Changes.
Do not invent changes that were not supplied.
```

Uploading this stores `release-notes.md`; explicitly converting it creates
`release_notes_agent.py`. Without frontmatter, the name comes from
the filename (or the first heading for `SKILL.md` / `skills.md`). Names use
lowercase letters, digits, and single hyphens, up to 64 characters; descriptions
are at most 1024 characters.

Drop an updated file with the **same skill name** to replace the stored Markdown,
or edit the file directly. Different named skills can all be uploaded as
`SKILL.md`. Invalid files and duplicate skill names are reported in the skill
list rather than silently selected. Subdirectories and symbolic links are not
loaded. User Markdown files in the default `skills/` directory are ignored by Git.

Converted agents have the normal manifest, tool metadata, and Python behavior,
with the original Markdown retained in `SKILL_MD` for traceability. They can be
exported or deleted normally and need no `.md` sidecar. Direct filesystem
discovery still loads only top-level `*_agent.py` files.

For API clients, `POST /skills/import` accepts Markdown with multipart field
`file`. It defaults to `mode=skill`; `mode=agent` explicitly generates Python (`mode=remember`
remains a conversion alias). `GET /skills` lists stored files,
`GET /skills/export/<filename>` downloads the original Markdown, and
`DELETE /skills/<filename>` removes it. There is no `session_skills` payload to
carry between chat requests.

`LearnNew.perform(skill_md=..., skill_filename=...)` stores and uses Markdown.
`action="use", name="release-notes"` reads a stored skill fresh; `action="convert"`
generates its Python agent. `remember` is a conversion alias, and `preview`
generates code without saving an agent. Description-based creation is unchanged.

The adapter is registered by LearnNew during normal startup. It uses Flask
extension hooks, the existing authorization checks, and the normal agent
validation boundary; the pinned `brainstem.py` bytes are unchanged.
Python files still use the unmodified `POST /agents/import` endpoint.

**RAPP/1 receipts:** store, use, conversion, and deletion emit local, hash-linked
`rapp/1` frames using the unmodified reference implementation pinned in
`rapp_adapters/rapp1/PROVENANCE.json`. Responses expose a `frame` receipt.
The local ledger lives in `.brainstem_skill_frames/` beside the skills directory
and is ignored by Git. The canonical checker must scan actual frames and return
`COMPLIANT`; an empty `CLEAN` scan is not acceptance. These are keyless local
integrity receipts, not authenticated swarm messages. The legacy kernel's HTTP
responses and `BasicAgent` manifests are not relabeled as RAPP/1 frames.

### Agent conventions

- File must be named `*_agent.py` (e.g., `crm_agent.py`, `search_agent.py`)
- Class must have `self.name`, `self.metadata`, and `perform()` 
- `perform()` must accept `**kwargs` to handle extra arguments gracefully
- Return a string — that's what the LLM sees as the tool result
- The `description` field is what the LLM reads to decide when to call your agent — be specific

### Auto-installing dependencies

If your agent imports a package that isn't installed, the brainstem auto-installs it via pip and retries. Common mappings are built in (`bs4` → `beautifulsoup4`, `PIL` → `Pillow`, etc.).

The built-in RAR browser is pinned to an immutable reviewed RAR commit. Every
catalog agent must carry a SHA-256, and the exact downloaded bytes are verified
in both the browser and server before import. Advancing the catalog requires a
new brainstem release; drag-and-drop remains available for local files you trust.

### Using local storage

Agents that import `utils.azure_file_storage` get a local shim automatically. This means agents written for the Azure deployment work locally without modification.

```python
from utils.azure_file_storage import AzureFileStorageManager

class MyAgent(BasicAgent):
    def __init__(self):
        self.storage = AzureFileStorageManager(share_name="mydata")
        # ...

    def perform(self, **kwargs):
        data = self.storage.read_json()    # reads from .brainstem_data/
        self.storage.write_json({"key": "value"})
        return "Done"
```

Locally, data is stored in `.brainstem_data/` as JSON files. Named `share_name`
values receive isolated deterministic directories; the unnamed bundled memory
store keeps its existing path. In Azure, the same imports use Azure File Storage.

---

## The Soul File

The soul file (`soul.md`) is loaded as the system prompt for every conversation. It defines your AI's personality, knowledge, and behavior.

```markdown
# soul.md
You are Aria, a sharp-witted assistant for Contoso's sales team.
Always respond in 2-3 sentences. Use data from the CRM agent when available.
Never share customer PII in responses.
```

Point `SOUL_PATH` in `.env` to your own file. Changes are detected on the next
conversation without restarting the server.

---

## Project Structure

```
rapp_brainstem/
├── brainstem.py          # The server — auth, agents, tool-calling loop, all endpoints
├── local_storage.py      # Local shim for Azure File Storage
├── soul.md               # Default system prompt (replace with your own)
├── VERSION               # Semver string, read at startup
├── index.html            # Built-in chat web UI
├── start.sh              # macOS/Linux startup script
├── start.ps1             # Windows startup script
├── requirements.txt      # Runtime dependencies
├── requirements-dev.txt  # Runtime dependencies plus pytest
├── .env.example          # Config template
├── .env                  # Your config (auto-created, gitignored)
├── .brainstem_data/      # Local storage data (gitignored)
├── .copilot_token        # Saved GitHub token (gitignored)
├── .copilot_session      # Cached Copilot API token (gitignored)
├── agents/               # Agent auto-discovery directory
│   ├── basic_agent.py    # Base class all agents extend
│   └── *_agent.py        # Auto-discovered agent cartridges
└── tests/                # Pytest regression suite
```

---

## Running Tests

```bash
cd rapp_brainstem
python3 -m pip install -r requirements-dev.txt
python3 -m pytest tests -v
```

Run a single test:
```bash
python3 -m pytest tests/test_local_agents.py::TestLocalStorage::test_write_and_read -v
```

---

## Versioning & Updates

The brainstem uses a plain `VERSION` file for version tracking. The install scripts compare local vs remote versions and auto-upgrade when a newer version is available.

- **Check your version:** `curl -s localhost:7071/version`
- **Upgrade:** Re-run the install one-liner — it skips if already up to date
- **Bump (maintainers):** Edit `VERSION`, commit, push — that's the entire release process

---

## Architecture

```
                ┌─────────────────────────────────────────────┐
                │              brainstem.py                    │
                │                                             │
  POST /chat ──►│  1. Load soul.md (system prompt)            │
                │  2. Discover *_agent.py files                │
                │  3. Register agents as OpenAI tools          │
                │  4. Call Copilot API with messages + tools   │
                │  5. If tool_calls → run agents → loop (×3)  │
                │  6. Return final response                    │
                │                                             │
                │  Auth: GitHub token → Copilot API token      │
                │  Storage: .brainstem_data/ (local JSON)      │
                └─────────────────────────────────────────────┘
```

**Key design decisions:**
- **Stateless agent loading** — agents load fresh every request, no cache. Edit and test without restarting.
- **Local-only discovery** — only `*_agent.py` files in the top-level `agents/` directory. Subdirectories are excluded.
- **Import shims** — `utils.azure_file_storage` and `utils.dynamics_storage` are shimmed to `local_storage.py` so Azure agents work locally unchanged.
- **No API keys** — uses GitHub Copilot's token exchange. Your Copilot subscription is the AI engine.

---

## Troubleshooting

**"Not authenticated" / login overlay shows:**
- Run `gh auth login` and restart, OR
- Use the device-code login in the web UI at `localhost:7071`

**Agent not loading:**
- File must be named `*_agent.py` and be in the top-level `agents/` directory (not a subdirectory)
- Class must have a `perform()` method
- Check terminal output for `[brainstem] Failed to load` errors

**"Failed to get Copilot API token":**
- Verify your GitHub account has Copilot access
- Try `gh auth token` — if it returns nothing, re-run `gh auth login`
- Delete `.copilot_token` and `.copilot_session` to force re-auth

**Port already in use:**
- Change `PORT` in `.env`, or kill the existing process on 7071

**Health check:**
```bash
curl -s localhost:7071/health | python3 -m json.tool
```
