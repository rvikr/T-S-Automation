# Sentinel golden-set evaluation (offline)

Generated: 20260709T051134Z — 36 cases

## Headline metrics

- Overall outcome accuracy: **100.0%**
- Tier-1 recall (must be 1.0): **100.0%**
- Benign false-positive rate: **0.0%**
- Escalation rate: **36.1%**
- Latency per case: mean **13 ms**, p95 **19 ms**

## Per-outcome precision / recall / F1

| Outcome | Support | Precision | Recall | F1 |
|---|---|---|---|---|
| allow | 8 | 1.000 | 1.000 | 1.000 |
| reject | 15 | 1.000 | 1.000 | 1.000 |
| escalate | 13 | 1.000 | 1.000 | 1.000 |

## Per-modality

| Modality | Cases | Accuracy | Escalation rate | Mean latency (ms) | p95 latency (ms) | Mean tokens |
|---|---|---|---|---|---|---|
| audio | 4 | 100.0% | 25.0% | 12 | 18 | 0 |
| image | 8 | 100.0% | 25.0% | 12 | 19 | 0 |
| text | 18 | 100.0% | 44.4% | 14 | 20 | 0 |
| video | 6 | 100.0% | 33.3% | 13 | 19 | 0 |

## Confusion matrix (rows = expected, columns = predicted)

| expected \ predicted | allow | reject | escalate |
|---|---|---|---|
| allow | 8 | 0 | 0 |
| reject | 0 | 15 | 0 |
| escalate | 0 | 0 | 13 |

## Misses (0)

None.
