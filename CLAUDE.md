# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

RAPP Installer is the single entry point for the RAPP (Rapid Agent Prototype Platform) — a progressive AI agent platform that teaches the Microsoft AI stack through three tiers. The repo contains the brainstem server, install scripts, Azure deployment templates, a Power Platform solution, and a landing page.

Philosophy: "engine, not experience" — this is infrastructure, not a consumer product. See `CONSTITUTION.md` for scope rules and governance.

## Repository Layout

- `rapp_brainstem/` — The core brainstem server (see `rapp_brainstem/CLAUDE.md` for deep internals)
- `install.sh`, `install.ps1`, `install.cmd` — One-liner installers (Brainstem path). **These are sacred** — any change must be tested end-to-end on a fresh machine.
- `community_rapp/` — Hippocampus (Tier 2) installer scripts. Parallel path, no dependency on brainstem.
- `azuredeploy.json`, `deploy.sh`, `deploy.ps1` — Azure ARM deployment (Tier 2 cloud)
- `MSFTAIBASMultiAgentCopilot_*.zip` — Power Platform solution for Copilot Studio (Tier 3)
- `index.html` — Landing page served at kody-w.github.io/rapp-installer
- `docs/` — Tutorial and docs pages for the landing site
- `skill.md` — Moltbook-pattern onboarding skill (YAML frontmatter, autonomous steps, pause points)

## Commands

```bash
# Start brainstem server (creates venv, installs deps, launches on port 7071)
cd rapp_brainstem && ./start.sh

# Direct run (assumes deps installed)
cd rapp_brainstem && python brainstem.py

# Run all tests
cd rapp_brainstem && python3 -m pytest test_local_agents.py -v

# Run a single test
cd rapp_brainstem && python3 -m pytest test_local_agents.py::TestLocalStorage::test_write_and_read -v

# Run a single test class
cd rapp_brainstem && python3 -m pytest test_local_agents.py::TestShimRegistration -v

# Health check (server must be running)
curl -s localhost:7071/health | python3 -m json.tool

# Test installer (bash)
bash tests/test_installer.sh
```

No linter, formatter, or type checker is configured.

## Architecture: Three Tiers

| Tier | Name | What | Key Files |
|------|------|------|-----------|
| 1 | **Brainstem** (local) | Flask server + GitHub Copilot API | `rapp_brainstem/brainstem.py` |
| 2 | **Spinal Cord** (Azure) | Azure Functions + Azure OpenAI | `azuredeploy.json`, `deploy.sh` |
| 3 | **Nervous System** (M365) | Copilot Studio + Teams | `MSFTAIBASMultiAgentCopilot_*.zip` |

Each tier is self-contained. Users advance when they choose to.

## Brainstem Server (rapp_brainstem/)

**Single-file server**: All logic lives in `brainstem.py` (~2,000 lines) — auth, routing, LLM calls, agent orchestration. Keep it that way.

**Request flow (POST /chat)**: Load soul.md -> discover agents from `agents/*_agent.py` -> call Copilot API with tools -> execute tool calls via agent `.perform()` -> loop up to 3 rounds -> return response.

**Agent system**: Files matching `agents/*_agent.py` are auto-discovered (flat directory only, `experimental/` excluded). Each extends `BasicAgent` with `metadata` (OpenAI function schema) and `perform(**kwargs)`. Agents reload from disk every request — no restart needed.

**Auth chain** (priority order): `GITHUB_TOKEN` env var -> `.copilot_token` file -> `gh auth token` CLI -> device code OAuth via `/login`. Copilot API tokens are short-lived with auto-refresh.

**Import shims**: `_register_shims()` injects `sys.modules` so agents written for CommunityRAPP (cloud) work locally — `utils.azure_file_storage` maps to `local_storage.py`.

**Memory agents**: `ManageMemory` and `ContextMemory` get special handling — `user_guid` arg is stripped, and `/chat` auto-injects `<memory>` context if ContextMemory is loaded.

## Branching and Release Model

**`main` is production.** The install one-liners (`curl ... install.sh | bash`) pull from `main`. Users get whatever is on `main`.

**Development happens on feature/fix branches.** Commits accumulate on the working branch (e.g., `3-device-code-auth-gets-stuck-...`). Multiple fixes and features can stack up before merging.

**Promotion path:**
1. Commit to feature branch (where active development happens)
2. When ready to release, merge to `main` with a `release: vX.Y.Z` commit
3. Bump `rapp_brainstem/VERSION` as part of the release commit

**Do not push directly to `main`** except via a merge at release time. The one-liner install is sacred — `main` must always be in a working state.

## Key Conventions

- **Python 3.11** target; venv at `~/.brainstem/venv`
- **No API keys** for local dev — GitHub Copilot token exchange handles auth
- **Config via `.env`** in `rapp_brainstem/` — `GITHUB_TOKEN`, `GITHUB_MODEL` (default `auto` — auto-selects the highest Claude Haiku the account can use — fastest responses — else the highest Sonnet, else `gpt-4o`; UI picks persist to `.brainstem_model`), `SOUL_PATH`, `AGENTS_PATH`, `PORT` (default 7071)
- Two install paths exist and must never cross-contaminate: brainstem (`install.sh`) and hippocampus (`community_rapp/install.sh`)
- The landing page (`index.html`) and `docs/` are static HTML — no build step
