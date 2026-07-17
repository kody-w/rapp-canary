# Autonomous pre-Grail test report

- Baseline Grail: `5fbde1776a72715935c3d597a9ddfce28a04032b`
- Evidence mode: **evidence-only**
- Qualification requires the separate candidate test job to pass.
- Successful feature scenarios: **5**
- Expected failure scenarios blocked: **3**

## Features

| Scenario | Result | Shared digest |
|---|---|---|
| backend-route | passed | `9183c848671845b5` |
| ui-meta | passed | `690cc7f213b58a45` |
| agent-addition | passed | `278afdc321c49b77` |
| installer-parity | passed | `d1772f05bc578330` |
| tree-shape | passed | `0878b50e2ec8e883` |

## Failure cases

- `rewrite-count-drift`: **blocked**
- `shared-payload-divergence`: **blocked**
- `human-grail-guard`: **blocked**

## Rollback

All four ring `main` branches remained unchanged during the run.
Grail remained at its independently sampled baseline SHA.
Candidate processes used an isolated HOME/config with no explicit GitHub tokens.
The hosted workflow repeats this on fresh read-only runners.
