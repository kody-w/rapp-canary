---
name: rapp-bootstrap
description: Install or repair a local RAPP Brainstem, verify its health, and install the unified RAR plugin marketplace for Microsoft Scout or GitHub Copilot CLI.
allowed-tools: Bash, Read
---

# RAPP Bootstrap

Use this skill when the user wants a local RAPP Brainstem, the RAR agent and
skill marketplace, or both.

## Safety boundary

- Explain that installation writes under `~/.brainstem`, `~/.copilot`, and
  the user's Copilot plugin configuration.
- Obtain user approval before running a fresh installer or replacing an
  existing installation.
- Never modify `rapp_brainstem/brainstem.py` or the RAPP/1 Grail directly.
- Preserve user agents, soul, configuration, and local data through the
  installer's backup/update path.
- Treat a failed health check as a failure. Do not claim installation worked.

## Procedure

1. Check whether Brainstem already answers:

   ```bash
   curl --silent --show-error --fail http://localhost:7071/health
   ```

2. If it is unavailable and the user approved installation, run the matching
   platform installer.

   macOS or Linux:

   ```bash
   curl -fsSL https://kody-w.github.io/rapp-installer/install.sh | bash
   ```

   Windows PowerShell:

   ```powershell
   irm https://raw.githubusercontent.com/kody-w/rapp-installer/main/install.ps1 | iex
   ```

3. Poll `http://localhost:7071/health` until it returns JSON with
   `status: "ok"`. If authentication is required, direct the user to the
   Brainstem login flow instead of inventing success.

4. Register and install the unified RAR plugin:

   ```bash
   copilot plugin marketplace add kody-w/RAR
   copilot plugin install rapp@rar
   ```

5. Start a new Scout or Copilot CLI conversation so `rapp-skills` is
   discovered.

6. Use the installed `rapp-skills` capability to:

   - inspect/start Brainstem with `status` or `ensure`;
   - browse and sync RAR channels;
   - run `bootstrap_callback` for the optional external-AI collaboration loop;
   - generate `manual_export` HTML packages for Scout, Copilot Studio, or
     Microsoft Copilot Cowork.

## Acceptance checks

- Brainstem `/health` is reachable.
- The response identifies the installed Brainstem version and loaded agents.
- `copilot plugin marketplace list` includes `kody-w/RAR`.
- `copilot plugin list` includes `rapp@rar`.
- A new conversation discovers `rapp-skills`.
