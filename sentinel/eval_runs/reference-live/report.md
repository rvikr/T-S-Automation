# Sentinel golden-set evaluation (live)

Generated: 20260731T054539Z — 18 cases

> Live mode scores the golden set's text cases (the image/audio/video entries are
> labeled text placeholders); pass `--live-all` to force every modality through.

## Headline metrics

- Overall outcome accuracy: **88.9%**
- Tier-1 recall (must be 1.0): **100.0%**
- Benign false-positive rate: **0.0%**
- Escalation rate: **55.6%**
- Latency per case: mean **6846 ms**, p95 **12405 ms**
- Total tokens: **92,426**
- Estimated cost: **$0.0404** total, **$0.0022** mean per case (published per-token rates)

## Per-outcome precision / recall / F1

| Outcome | Support | Precision | Recall | F1 |
|---|---|---|---|---|
| allow | 2 | 1.000 | 1.000 | 1.000 |
| reject | 8 | 1.000 | 0.750 | 0.857 |
| escalate | 8 | 0.800 | 1.000 | 0.889 |

## Per-modality

| Modality | Cases | Accuracy | Escalation rate | Mean latency (ms) | p95 latency (ms) | Mean tokens |
|---|---|---|---|---|---|---|
| text | 18 | 88.9% | 55.6% | 6846 | 12405 | 5135 |

## Confusion matrix (rows = expected, columns = predicted)

| expected \ predicted | allow | reject | escalate |
|---|---|---|---|
| allow | 2 | 0 | 0 |
| reject | 0 | 6 | 2 |
| escalate | 0 | 0 | 8 |

## Misses (2)

| Case | Category | Expected | Predicted | Final decision | Reviewer |
|---|---|---|---|---|---|
| txt-sexual-001 | Romantic/Sexual Content | reject | escalate | reject | senior |
| txt-advertising-001 | Advertising | reject | escalate | reject | senior |
