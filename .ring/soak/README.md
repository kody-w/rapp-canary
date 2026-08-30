# Authenticated soak evidence

This directory stores immutable `rapp/1:soak` frames. Each frame is produced
from a running isolated Canary soak after the whole-train qualification is
known:

```bash
.ring/tools/soak.sh evidence \
  --beta-commit <qualified-beta-commit> \
  --qualification-run <pre-grail-run-id> \
  --model-id <explicit-model-id> \
  --output ".ring/soak/<beta-commit>-<model-id>.json"
```

The soak starts with an authenticated `/chat`, records health/model probes at a
bounded interval, and ends with another authenticated `/chat`. Evidence requires
the policy-defined duration, complete healthy probe coverage, the named model,
an isolated runtime path, and no critical events in the soak log. A `--no-auth`
session can never emit real-auth evidence. The frame contains no credential or
model response content.

Commit the frame to Canary `main`. Stage Preprod with its commit-pinned raw URL
and SHA-256. The gate rejects evidence for a different qualification commit,
Beta commit, model, duration, or freshness window.
