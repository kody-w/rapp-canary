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

**Semantics of `automated_promotion: true`**: it means `promote_ring.py` MAY
write that edge when an operator runs it. There is NO scheduled automation and
no workflow holds write credentials to any ring; the Grail edge additionally
refuses the tool (`human_merge_required`). Day-to-day operation lives in
`RUNBOOK.md`; the last gate before a Grail merge is `tools/grail_gate.py`.
