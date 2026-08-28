# Contributing

RAPP Installer accepts contributions through pull requests. It follows the
same repository-level marketplace pattern used by Microsoft Power CAT Skills:
plugin metadata under `.claude-plugin/`, skills under `skills/`, structured
issue forms, and explicit conduct, security, and support policies.

RAPP Installer is an independent community project and does not imply
Microsoft affiliation or endorsement.

## Contribution paths

| Contribution | Path |
|---|---|
| Installer, Brainstem framework, docs, plugin metadata, or tests | Pull request |
| New reusable `agent.py` | Submit through [kody-w/RAR](https://github.com/kody-w/RAR/blob/main/CONTRIBUTING.md) |
| Bug | [Bug report](https://github.com/kody-w/rapp-installer/issues/new?template=bug_report.yml) |
| Feature | [Feature request](https://github.com/kody-w/rapp-installer/issues/new?template=feature_request.yml) |
| Vulnerability | [Private security advisory](https://github.com/kody-w/rapp-installer/security/advisories/new) |

Read [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md), [SECURITY.md](SECURITY.md), and
[SUPPORT.md](SUPPORT.md) before participating. Marketplace contributions must
also follow [MARKETPLACE_CHARTER.md](MARKETPLACE_CHARTER.md).

## Development workflow

1. Fork the repository and create a focused branch.
2. Preserve the progressive Brainstem -> Azure -> Copilot Studio architecture.
3. Do not combine the Brainstem and Hippocampus install paths.
4. Keep `rapp_brainstem/brainstem.py` single-file and avoid product-specific
   workflows in the kernel.
5. Add regression coverage for changed behavior.
6. Run:

   ```bash
   cd rapp_brainstem
   python3 -m pytest tests -q
   cd ..
   bash tests/test_installer.sh
   ```

7. Open a pull request describing the user problem, behavior change, and exact
   validation performed.

## Installer changes

The one-line installers are production entrypoints. Changes to `install.sh`,
`install.ps1`, or `install.cmd` require a real fresh-install or upgrade-path
proof on the affected platform. Preserve user agents, soul, `.env`, tokens,
and `.brainstem_data`.

## Plugin marketplace

Install the bootstrap plugin with:

```bash
copilot plugin marketplace add kody-w/rapp-installer
copilot plugin install rapp@brainstem
```

Claude Code uses the same identity:

```bash
claude plugin marketplace add kody-w/rapp-installer
claude plugin install rapp@brainstem
```

Marketplace and skill changes must keep these files consistent:

- `.claude-plugin/marketplace.json`
- `.claude-plugin/plugin.json`
- `skills/rapp-bootstrap/SKILL.md`
