# Canary overlay

This directory belongs to the Canary ring and is never promoted as shared
payload. Canary-specific URLs, deployment settings, and future patches live
here. The Grail-derived files outside `.ring/` remain the promotable payload.

`train.json` defines:

```text
Canary -> Nightly -> Alpha -> Beta -> human-only Grail
```

`tools/` creates deterministic attestations, promotes only shared Git blobs,
preserves each target's `.ring/` overlay, and renders checked ring-specific URLs.
The pre-Grail workflow has read-only credentials and cannot write to Grail.
