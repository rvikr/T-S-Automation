# Sentinel golden-set evaluation (live)

Generated: 20260708T044326Z — 18 cases

## Headline metrics

- Overall outcome accuracy: **83.3%**
- Tier-1 recall (must be 1.0): **100.0%**
- Benign false-positive rate: **0.0%**
- Escalation rate: **50.0%**

## Per-outcome precision / recall / F1

| Outcome | Support | Precision | Recall | F1 |
|---|---|---|---|---|
| allow | 2 | 0.667 | 1.000 | 0.800 |
| reject | 8 | 1.000 | 0.750 | 0.857 |
| escalate | 8 | 0.778 | 0.875 | 0.824 |

## Confusion matrix (rows = expected, columns = predicted)

| expected \ predicted | allow | reject | escalate |
|---|---|---|---|
| allow | 2 | 0 | 0 |
| reject | 0 | 6 | 2 |
| escalate | 1 | 0 | 7 |

## Misses (3)

| Case | Category | Expected | Predicted | Final decision | Reviewer |
|---|---|---|---|---|---|
| txt-sexual-001 | Romantic/Sexual Content | reject | escalate | reject | senior |
| txt-harass-amb-002 | Harassment & Discrimination | escalate | allow | allow | text-specialist |
| txt-advertising-001 | Advertising | reject | escalate | reject | senior |
