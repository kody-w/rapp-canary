# Grail Brainstem History

This directory is the append-only frame chain for known-good Grail
`rapp_brainstem/brainstem.py` releases.

Each `brainstem-vX.Y.Z.json` file is a deterministic `rapp/1:brainstem` frame
binding:

- immutable Grail release tag
- release commit and tree
- Git blob identity
- SHA-256 and byte length of `brainstem.py`
- the previous known-good frame hash

Verify the chain against a Grail checkout:

```bash
python3 -I .ring/tools/brainstem_history.py verify-all \
  --repo /path/to/rapp-installer \
  --directory .ring/brainstem-history
```

Preprod publishes this complete chain and rehearses rollback using the selected
frame's exact commit. The release tag is a verified human-readable alias; the
frame proves which exact Brainstem it names and is not permission to restore
`brainstem.py` alone into a mismatched release.
