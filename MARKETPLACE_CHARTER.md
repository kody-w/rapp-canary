# `rapp@x` Marketplace Charter

RAPP Installer follows
[RAR Constitution Article XXV](https://github.com/kody-w/RAR/blob/main/CONSTITUTION.md#article-xxv--the-rappx-marketplace-identity).
This charter applies the constitutional marketplace identity without changing
the Brainstem kernel or its Grail.

## Ratified identity

```text
rapp@brainstem
```

This plugin installs and verifies the local Brainstem, then registers the RAR
marketplace and installs:

```text
rapp@rar
```

## Invariants

1. The plugin name remains `rapp`; the marketplace name identifies the layer.
2. Installation is additive and requires normal host permission.
3. Existing Brainstem agents, soul, configuration, authentication, and local
   data are preserved by the installer.
4. `rapp_brainstem/brainstem.py` and the RAPP/1 Grail are never patched to make
   a marketplace integration work.
5. Executable bootstrap artifacts are pinned and verified where supported.
6. The same marketplace manifests are valid for GitHub Copilot CLI and Claude
   Code; Microsoft Scout consumes them through its Copilot plugin support.
7. Public plugin identities and repository paths are not repurposed.

The plugin is `rapp`. The marketplace tells you where it lives.
