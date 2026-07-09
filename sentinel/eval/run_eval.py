"""Score the moderation pipeline against the labeled golden set.

Offline (default): deterministic synthetic mode, no network, fresh DB per
case so ordering never affects scores.

Live (--live): production mode with real agent runs. Text cases only by
default because the golden set's media placeholders are text files; use
--live-all to force every modality through anyway.

Usage:
    python -m sentinel.eval.run_eval [--live] [--live-all] [--output-dir DIR]

Outcome model (what a T&S team actually measures):
    expected outcome  = escalate  for Tier-1 and ambiguous-labeled cases,
                        otherwise the labeled allow/reject decision
    predicted outcome = escalate  when a ticket exists or a senior/human
                        reviewer was involved, otherwise the final decision
"""

from __future__ import annotations

import argparse
import json
import tempfile
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from sentinel.agents.orchestrator import run_case
from sentinel.config import EVAL_RUNS_DIR
from sentinel.models import Case, CaseResult
from sentinel.tools.media_utils import load_synthetic_cases
from sentinel.tools.policy_retrieval import TIER1_CATEGORIES
from sentinel.ui_uploads import estimate_cost_usd

OUTCOMES = ("allow", "reject", "escalate")
DEFAULT_OUTPUT_DIR = EVAL_RUNS_DIR


@dataclass(frozen=True)
class CaseScore:
    case_id: str
    asset_type: str
    category: str
    expected_outcome: str
    predicted_outcome: str
    final_decision: str
    reviewer: str
    ticket_id: str | None
    quarantined: bool
    correct: bool
    latency_ms: int = 0
    total_tokens: int = 0
    est_cost_usd: float = 0.0


def expected_outcome(case: Case) -> str:
    category = str(case.metadata.get("expected_category", ""))
    if category in TIER1_CATEGORIES:
        return "escalate"
    expected = str(case.metadata.get("expected_decision", "allow"))
    return "escalate" if expected == "ambiguous" else expected


def predicted_outcome(result: CaseResult) -> str:
    if result.ticket is not None or result.verdict.reviewer in {"senior", "human"}:
        return "escalate"
    if result.verdict.decision in {"allow", "reject"}:
        return result.verdict.decision
    return "escalate"


def score_case(case: Case, result: CaseResult) -> CaseScore:
    expected = expected_outcome(case)
    predicted = predicted_outcome(result)
    return CaseScore(
        case_id=case.id,
        asset_type=case.asset_type,
        category=str(case.metadata.get("expected_category", "")),
        expected_outcome=expected,
        predicted_outcome=predicted,
        final_decision=result.verdict.decision,
        reviewer=result.verdict.reviewer,
        ticket_id=result.ticket.id if result.ticket else None,
        quarantined=result.quarantined,
        correct=expected == predicted,
        # result.case is the case that actually ran (a label-free copy in live mode).
        latency_ms=int(result.case.metadata.get("latency_ms", 0) or 0),
        total_tokens=int((result.case.metadata.get("token_usage") or {}).get("total_tokens", 0) or 0),
        est_cost_usd=round(estimate_cost_usd(result.case.metadata.get("token_usage")) or 0.0, 6),
    )


def _live_case(case: Case) -> Case:
    # Production mode must not see golden labels; the agent reads the asset itself.
    return Case(
        id=case.id,
        asset_type=case.asset_type,
        asset_path=case.asset_path,
        metadata={"analysis_mode": "production"},
    )


def run_golden_set(live: bool = False, live_all: bool = False) -> list[CaseScore]:
    cases = load_synthetic_cases()
    scores: list[CaseScore] = []
    for golden in cases:
        if live and golden.asset_type != "text" and not live_all:
            continue
        case = _live_case(golden) if live else golden
        with tempfile.TemporaryDirectory() as tmp:
            result = run_case(case, db_path=Path(tmp) / "audit.sqlite")
        scores.append(score_case(golden, result))
    return scores


def compute_metrics(scores: list[CaseScore]) -> dict:
    confusion: dict[str, dict[str, int]] = {e: {p: 0 for p in OUTCOMES} for e in OUTCOMES}
    for score in scores:
        confusion[score.expected_outcome][score.predicted_outcome] += 1

    per_class = {}
    for outcome in OUTCOMES:
        tp = confusion[outcome][outcome]
        fp = sum(confusion[e][outcome] for e in OUTCOMES if e != outcome)
        fn = sum(confusion[outcome][p] for p in OUTCOMES if p != outcome)
        precision = tp / (tp + fp) if tp + fp else None
        recall = tp / (tp + fn) if tp + fn else None
        f1 = (2 * precision * recall / (precision + recall)) if precision and recall else (0.0 if precision is not None or recall is not None else None)
        per_class[outcome] = {
            "support": tp + fn,
            "precision": round(precision, 4) if precision is not None else None,
            "recall": round(recall, 4) if recall is not None else None,
            "f1": round(f1, 4) if f1 is not None else None,
        }

    tier1 = [s for s in scores if s.category in TIER1_CATEGORIES]
    tier1_handled = [s for s in tier1 if s.predicted_outcome == "escalate" and s.ticket_id and s.quarantined]
    benign = [s for s in scores if s.category == "No Violation"]
    benign_rejected = [s for s in benign if s.predicted_outcome == "reject"]
    escalated = [s for s in scores if s.predicted_outcome == "escalate"]

    return {
        "cases": len(scores),
        "accuracy": round(sum(s.correct for s in scores) / len(scores), 4) if scores else None,
        "tier1_recall": round(len(tier1_handled) / len(tier1), 4) if tier1 else None,
        "benign_false_positive_rate": round(len(benign_rejected) / len(benign), 4) if benign else None,
        "escalation_rate": round(len(escalated) / len(scores), 4) if scores else None,
        "latency_ms": _latency_stats(scores),
        "total_tokens": sum(s.total_tokens for s in scores),
        "est_cost_usd_total": round(sum(s.est_cost_usd for s in scores), 4),
        "est_cost_usd_mean": round(sum(s.est_cost_usd for s in scores) / len(scores), 6) if scores else None,
        "per_class": per_class,
        "confusion_matrix": confusion,
        "per_modality": _per_modality(scores),
    }


def _latency_stats(scores: list[CaseScore]) -> dict | None:
    if not scores:
        return None
    latencies = sorted(s.latency_ms for s in scores)
    p95_index = min(len(latencies) - 1, max(0, round(0.95 * len(latencies)) - 1))
    return {
        "mean": round(sum(latencies) / len(latencies)),
        "p95": latencies[p95_index],
    }


def _per_modality(scores: list[CaseScore]) -> dict:
    by_modality: dict[str, list[CaseScore]] = defaultdict(list)
    for score in scores:
        by_modality[score.asset_type].append(score)
    per_modality: dict[str, dict] = {}
    for asset_type in sorted(by_modality):
        group = by_modality[asset_type]
        stats = _latency_stats(group) or {}
        per_modality[asset_type] = {
            "cases": len(group),
            "accuracy": round(sum(s.correct for s in group) / len(group), 4),
            "escalation_rate": round(sum(1 for s in group if s.predicted_outcome == "escalate") / len(group), 4),
            "mean_latency_ms": stats.get("mean", 0),
            "p95_latency_ms": stats.get("p95", 0),
            "mean_tokens": round(sum(s.total_tokens for s in group) / len(group)),
        }
    return per_modality


def write_report(scores: list[CaseScore], metrics: dict, output_dir: Path, mode: str) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = output_dir / f"{timestamp}-{mode}"
    run_dir.mkdir(parents=True, exist_ok=True)

    (run_dir / "results.json").write_text(
        json.dumps(
            {
                "mode": mode,
                "generated_at": timestamp,
                "metrics": metrics,
                "cases": [asdict(score) for score in scores],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    lines = [
        f"# Sentinel golden-set evaluation ({mode})",
        "",
        f"Generated: {timestamp} — {metrics['cases']} cases",
    ]
    if mode == "live":
        lines += [
            "",
            "> Live mode scores the golden set's text cases (the image/audio/video entries are",
            "> labeled text placeholders); pass `--live-all` to force every modality through.",
        ]
    lines += [
        "",
        "## Headline metrics",
        "",
        f"- Overall outcome accuracy: **{metrics['accuracy']:.1%}**",
        f"- Tier-1 recall (must be 1.0): **{metrics['tier1_recall']:.1%}**" if metrics["tier1_recall"] is not None else "- Tier-1 recall: n/a",
        f"- Benign false-positive rate: **{metrics['benign_false_positive_rate']:.1%}**" if metrics["benign_false_positive_rate"] is not None else "- Benign false-positive rate: n/a",
        f"- Escalation rate: **{metrics['escalation_rate']:.1%}**",
    ]
    latency = metrics.get("latency_ms")
    if latency:
        lines.append(f"- Latency per case: mean **{latency['mean']} ms**, p95 **{latency['p95']} ms**")
    if metrics.get("total_tokens"):
        lines.append(f"- Total tokens: **{metrics['total_tokens']:,}**")
    if metrics.get("est_cost_usd_total"):
        lines.append(
            f"- Estimated cost: **${metrics['est_cost_usd_total']:.4f}** total, "
            f"**${metrics['est_cost_usd_mean']:.4f}** mean per case (published per-token rates)"
        )
    lines += [
        "",
        "## Per-outcome precision / recall / F1",
        "",
        "| Outcome | Support | Precision | Recall | F1 |",
        "|---|---|---|---|---|",
    ]
    for outcome in OUTCOMES:
        row = metrics["per_class"][outcome]
        fmt = lambda v: f"{v:.3f}" if isinstance(v, float) else "n/a"
        lines.append(f"| {outcome} | {row['support']} | {fmt(row['precision'])} | {fmt(row['recall'])} | {fmt(row['f1'])} |")

    per_modality = metrics.get("per_modality") or {}
    if per_modality:
        lines += [
            "",
            "## Per-modality",
            "",
            "| Modality | Cases | Accuracy | Escalation rate | Mean latency (ms) | p95 latency (ms) | Mean tokens |",
            "|---|---|---|---|---|---|---|",
        ]
        for asset_type, row in per_modality.items():
            lines.append(
                f"| {asset_type} | {row['cases']} | {row['accuracy']:.1%} | {row['escalation_rate']:.1%} "
                f"| {row['mean_latency_ms']} | {row['p95_latency_ms']} | {row['mean_tokens']} |"
            )

    lines += ["", "## Confusion matrix (rows = expected, columns = predicted)", ""]
    lines.append("| expected \\ predicted | " + " | ".join(OUTCOMES) + " |")
    lines.append("|---|" + "---|" * len(OUTCOMES))
    for expected in OUTCOMES:
        lines.append(f"| {expected} | " + " | ".join(str(metrics["confusion_matrix"][expected][p]) for p in OUTCOMES) + " |")

    misses = [s for s in scores if not s.correct]
    lines += ["", f"## Misses ({len(misses)})", ""]
    if misses:
        lines.append("| Case | Category | Expected | Predicted | Final decision | Reviewer |")
        lines.append("|---|---|---|---|---|---|")
        for s in misses:
            lines.append(
                f"| {s.case_id} | {s.category} | {s.expected_outcome} | {s.predicted_outcome} | {s.final_decision} | {s.reviewer} |"
            )
    else:
        lines.append("None.")

    (run_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return run_dir


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate Sentinel against the golden set.")
    parser.add_argument("--live", action="store_true", help="Run production mode with real agent calls (text cases only).")
    parser.add_argument("--live-all", action="store_true", help="With --live: include non-text cases too.")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()

    mode = "live" if args.live else "offline"
    scores = run_golden_set(live=args.live, live_all=args.live_all)
    metrics = compute_metrics(scores)
    run_dir = write_report(scores, metrics, args.output_dir, mode)

    print(f"Evaluated {metrics['cases']} cases ({mode})")
    print(f"  accuracy={metrics['accuracy']:.1%} tier1_recall={metrics['tier1_recall']:.1%} "
          f"benign_fpr={metrics['benign_false_positive_rate']:.1%} escalation_rate={metrics['escalation_rate']:.1%}")
    print(f"Report: {run_dir / 'report.md'}")


if __name__ == "__main__":
    main()
