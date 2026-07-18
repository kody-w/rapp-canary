---
name: Install or upgrade failure
about: The one-liner install or an upgrade did not leave you with a working brainstem
labels: install
---

**OS and how you installed** (macOS/Linux `curl | bash`, Windows `irm | iex`, upgrade of an existing install):

**What the installer printed last** (paste the final ~20 lines):

```
```

**Version the site serves right now** (paste the output of):

```bash
curl -fsSL https://raw.githubusercontent.com/kody-w/rapp-installer/main/rapp_brainstem/VERSION
```

**If a server got as far as starting** — the log tail:

```bash
tail -30 ~/.brainstem/src/rapp_brainstem/nohup.out 2>/dev/null || tail -30 ~/.brainstem/*.log 2>/dev/null
```

**Escape hatch while we fix it** — pin the previous release:

```bash
BRAINSTEM_VERSION=<previous-version> bash -c "$(curl -fsSL https://kody-w.github.io/rapp-installer/install.sh)"
```
