# 🧠 RAPP Brainstem

> **Start with AI: [guided setup and learning](skill.md)**
> Prefer to work manually? The [one-liner option](#manual-one-liner-option) is still available.

A local-first AI agent server powered by GitHub Copilot. Core chat needs a
GitHub account with Copilot access, not a separate model-provider API key.

## Use your agent

Attach [skill.md](skill.md) to an approved AI assistant that can operate on your
computer, or have it read the linked repository file. Then say:

```text
Read the attached skill.md.
Help me get my Brainstem set up and learn to use it before office hours.
Handle the technical work, explain one step at a time, and ask before making changes.
Then guide me from a small business problem to a tested, reviewable local MVP.
Do not deploy to the cloud, publish, or send anything without separate approval.
```

The AI handles environment discovery, approved setup, diagnosis, agent/data
preparation, and testing. You choose the outcome, sign in personally, approve
changes, and judge the result. The guide follows a practical loop:
**brief -> bounded MVP -> agents and synthetic data -> tests -> Brainstem ->
evidence and feedback -> rehearsal**.
Use the agent you already prefer; this path does not require a new runtime or
an agent-specific plugin.

If your assistant is chat-only or runs in a cloud sandbox, the skill makes that
limitation explicit and guides the handoff instead of pretending it installed
anything on your computer.

## Manual one-liner option

The manual path remains supported.

**macOS / Linux:**
```bash
curl -fsSL https://kody-w.github.io/rapp-installer/install.sh | bash
```

**Windows PowerShell:**
```powershell
irm https://raw.githubusercontent.com/kody-w/rapp-installer/main/install.ps1 | iex
```
The installer handles the local setup and opens the chat UI. Complete the
Brainstem's GitHub sign-in flow when prompted, then return to the
[baseline and practice checkpoints](skill.md#checkpoint-2---connect-and-establish-a-real-baseline).
For later starts, ask your AI to open the existing instance or use the installed
`brainstem` command.

---

## Optional: existing plugin entry points

RAPP Installer follows the same plugin-marketplace pattern used by Microsoft
Power CAT Skills:

| Plugin identity | Purpose |
|---|---|
| `rapp@brainstem` | Install and verify Brainstem, then install RAR |
| `rapp@rar` | Operate RAR, skills, exports, and callback bootstrap |

```bash
copilot plugin marketplace add kody-w/rapp-installer
copilot plugin install rapp@brainstem
```

Claude Code uses the same marketplace:

```bash
claude plugin marketplace add kody-w/rapp-installer
claude plugin install rapp@brainstem
```

Or ask Scout:

```text
Add the kody-w/rapp-installer marketplace and install
rapp@brainstem. Then install my local Brainstem and RAR.
```

The `rapp-bootstrap` entry follows the same [end-to-end coaching skill](skill.md),
including approval, real readiness checks, and a small working practice loop.
On a supported plugin host, approved setup also registers `kody-w/RAR` and
installs `rapp@rar`. Start a new conversation afterward so the host discovers
the RAR skill manager.

The `rapp@x` identity is governed by
[MARKETPLACE_CHARTER.md](MARKETPLACE_CHARTER.md) and RAR Constitution Article
XXV. The same manifests are also loadable by Claude Code.

---

## Or: Start with the Cloud Backend (Hippocampus)

Want persistent memory, Azure Functions, and a path to Copilot Studio? Skip the brainstem and go straight to Tier 2:

**Mac / Linux:**
```bash
curl -fsSL https://raw.githubusercontent.com/kody-w/rapp-installer/main/community_rapp/install.sh | bash
```

**Windows:**
```powershell
irm https://raw.githubusercontent.com/kody-w/rapp-installer/main/community_rapp/install.ps1 | iex
```

Creates `~/rapp-projects/my-project/` — isolated project with its own venv, agents, and local storage. Auth happens through the chat UI (GitHub device code flow). No API keys needed to start.

[Onboarding guide](https://kody-w.github.io/CommunityRAPP/onboard.html) | [CommunityRAPP repo](https://github.com/kody-w/CommunityRAPP)

---

## How It Works

The brainstem is a Flask server that connects to GitHub Copilot's API for LLM inference. You define a **soul** (system prompt) and drop in **agents** (Python tools the LLM can call). That's it.

```
~/.brainstem/src/rapp_brainstem/
├── brainstem.py       # the server
├── soul.md            # personality (system prompt)
├── agents/            # auto-discovered tools
│   └── hello_agent.py
├── local_storage.py   # local-first storage shim
└── .env               # config (model, paths, port)
```

### Write an Agent

Any `*_agent.py` file in your agents directory gets auto-discovered and registered as a tool:

```python
from basic_agent import BasicAgent

class WeatherAgent(BasicAgent):
    def __init__(self):
        self.name = "Weather"
        self.metadata = {
            "name": self.name,
            "description": "Gets the weather for a city.",
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {"type": "string", "description": "City name"}
                },
                "required": ["city"]
            }
        }
        super().__init__()

    def perform(self, city="", **kwargs):
        return f"It's sunny in {city}!"
```

### Connect Remote Agent Repos

The chat UI has a **Sources** panel — paste any GitHub repo URL with an `agents/` folder and the brainstem hot-loads them. Missing pip dependencies are auto-installed.

---

## The Stack: Brainstem → Azure → Copilot Studio

RAPP teaches you the Microsoft AI stack one layer at a time. Start with the brainstem locally, then layer up when you're ready.

### 🧠 Tier 1: The Brainstem (local)

The survival basics. The brainstem runs the core agent loop — soul, tool-calling, conversation. Your GitHub Copilot subscription is the AI engine.

**What you learn:** Python agents, function-calling, prompt engineering, local-first development.

### ☁️ Tier 2: The Spinal Cord (Azure)

Give your brainstem a cloud body. Deploy to Azure so it's always-on with persistent storage, monitoring, and Azure OpenAI.

```bash
# Deploy via script
curl -fsSL https://raw.githubusercontent.com/kody-w/rapp-installer/main/deploy.sh | bash
```

Or click: [![Deploy to Azure](https://aka.ms/deploytoazurebutton)](https://portal.azure.com/#create/Microsoft.Template/uri/https%3A%2F%2Fraw.githubusercontent.com%2Fkody-w%2Frapp-installer%2Fmain%2Fazuredeploy.json)

Creates: Function App (Python 3.11), Azure OpenAI (GPT-4o), Storage Account, Application Insights. All Entra ID auth — no API keys.

**What you learn:** ARM templates, Azure Functions, managed identity, RBAC, Azure OpenAI.

### 🤖 Tier 3: The Nervous System (Copilot Studio)

Connect your agent to Teams and M365 Copilot. Import the included Power Platform solution (`MSFTAIBASMultiAgentCopilot_*.zip`) into Copilot Studio, point it at your Azure Function, and publish.

The same agent logic you tested locally now answers in Microsoft Teams and M365 Copilot across your organization.

**What you learn:** Copilot Studio, declarative agents, Power Platform solutions, Teams integration, enterprise AI.

---

## Configuration

All config via `.env` (see `.env.example`):

| Variable | Default | Description |
|----------|---------|-------------|
| `GITHUB_TOKEN` | auto-detected via `gh` | GitHub PAT or Copilot token |
| `GITHUB_MODEL` | `gpt-4o` | Model ([GitHub Models](https://github.com/marketplace/models)) |
| `SOUL_PATH` | `./soul.md` | Path to your soul file |
| `AGENTS_PATH` | `./agents` | Path to your agents directory |
| `PORT` | `7071` | Server port |

## API

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/chat` | POST | `{"user_input": "...", "conversation_history": [], "session_id": "..."}` |
| `/health` | GET | Status, model, loaded agents, token state |
| `/login` | POST | Start GitHub device code OAuth flow |
| `/models` | GET | List available models |
| `/repos` | GET | List connected agent repos |

## Requirements

- **Python 3.11+**
- **Git**
- **GitHub account** with Copilot access

## Updating

```bash
cd ~/.brainstem/src && git pull
```

## Uninstalling

```bash
rm -rf ~/.brainstem ~/.local/bin/brainstem
```
