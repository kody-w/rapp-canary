# Autonomous pre-Grail test report

- Baseline Grail: `5fbde1776a72715935c3d597a9ddfce28a04032b`
- Evidence mode: **isolated-tests**
- Qualification requires the separate candidate test job to pass.
- Successful feature scenarios: **9**
- Expected failure scenarios blocked: **4**

## Features

| Scenario | Result | Shared digest |
|---|---|---|
| backend-route | passed | `9183c848671845b5` |
| ui-meta | passed | `690cc7f213b58a45` |
| agent-addition | passed | `278afdc321c49b77` |
| installer-parity | passed | `d1772f05bc578330` |
| tree-shape | passed | `0878b50e2ec8e883` |
| config-default | passed | `5d9bb19de3057e6e` |
| storage-api | passed | `4b22789da6528f4b` |
| binary-asset | passed | `1035f9dfd58ef18e` |
| file-deletion | passed | `7804e13232dc5209` |

## Failure cases

- `rewrite-count-drift`: **blocked**
- `shared-payload-divergence`: **blocked**
- `required-file-deletion`: **blocked**
- `human-grail-guard`: **blocked**

## Rollback

All four ring `main` branches remained unchanged during the run.
Grail remained at its independently sampled baseline SHA.
Candidate processes used an isolated HOME/config with no explicit GitHub tokens.
The hosted workflow repeats this on fresh read-only runners.
