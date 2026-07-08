# Sentinel golden-set evaluation (offline)

Generated: 20260708T044011Z — 36 cases

## Headline metrics

- Overall outcome accuracy: **100.0%**
- Tier-1 recall (must be 1.0): **100.0%**
- Benign false-positive rate: **0.0%**
- Escalation rate: **36.1%**

## Per-outcome precision / recall / F1

| Outcome | Support | Precision | Recall | F1 |
|---|---|---|---|---|
| allow | 8 | 1.000 | 1.000 | 1.000 |
| reject | 15 | 1.000 | 1.000 | 1.000 |
| escalate | 13 | 1.000 | 1.000 | 1.000 |

## Confusion matrix (rows = expected, columns = predicted)

| expected \ predicted | allow | reject | escalate |
|---|---|---|---|
| allow | 8 | 0 | 0 |
| reject | 0 | 15 | 0 |
| escalate | 0 | 0 | 13 |

## Misses (0)

None.
