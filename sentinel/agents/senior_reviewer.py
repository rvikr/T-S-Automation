from __future__ import annotations

import logging
from pathlib import Path

from sentinel.models import Case, Verdict, build_verdict
from sentinel.tools.policy_retrieval import POLICY_CLAUSES, TIER1_CATEGORIES, get_clause_for_category
from sentinel.tools.precedent_memory import write_precedent

logger = logging.getLogger(__name__)


def review_case(case: Case, initial_verdict: Verdict, db_path: str | Path) -> Verdict:
    if case.metadata.get("analysis_mode") == "production" and _agent_runtime_ready():
        return _production_senior_review(case, initial_verdict, db_path)
    return _synthetic_senior_review(case, initial_verdict, db_path)


def _agent_runtime_ready() -> bool:
    try:
        from sentinel.agents.runtime import runtime_available
    except ImportError:
        return False
    return runtime_available()


def _production_senior_review(case: Case, initial_verdict: Verdict, db_path: str | Path) -> Verdict:
    from sentinel.agents.runtime import run_senior_case
    from sentinel.tools.production_analysis import _record_agent_metadata

    try:
        assessment = run_senior_case(case, initial_verdict, db_path)
    except Exception as exc:
        logger.exception("Senior agent runtime failed for case %s: %s", case.id, exc)
        return Verdict(
            case_id=case.id,
            decision="ambiguous",
            severity_tier=initial_verdict.severity_tier,
            category=initial_verdict.category,
            policy_clause=initial_verdict.policy_clause,
            confidence=0.0,
            rationale=f"Senior agent runtime failed ({type(exc).__name__}); failing closed to human review.",
            reviewer="senior",
        )

    category = assessment.category if assessment.category in POLICY_CLAUSES else initial_verdict.category
    clause = get_clause_for_category(category)
    _record_agent_metadata(case, assessment)

    if clause.tier == 1 or category in TIER1_CATEGORIES:
        decision = "ambiguous"
        confidence = max(assessment.confidence, 0.95)
        rationale = "Tier-1 signal surfaced during senior review; automated decision bypassed for human review."
    else:
        decision = assessment.decision
        confidence = assessment.confidence
        rationale = assessment.rationale or f"Senior review resolved the case against {clause.citation}."

    verdict = build_verdict(
        case_id=case.id,
        decision=decision,
        category=category,
        confidence=confidence,
        rationale=rationale,
        reviewer="senior",
    )
    write_precedent(case, verdict, db_path)
    return verdict


def _synthetic_senior_review(case: Case, initial_verdict: Verdict, db_path: str | Path) -> Verdict:
    clause = get_clause_for_category(initial_verdict.category)
    label = str(case.metadata.get("synthetic_label", "")).lower()
    if clause.tier == 1:
        decision = "ambiguous"
        confidence = 1.0
        rationale = "Tier-1 synthetic stand-in remains human-only."
    elif any(token in label for token in ["friends", "playful", "context", "borderline"]):
        decision = "allow"
        confidence = 0.78
        rationale = f"Senior review found benign context for {clause.citation}."
    elif clause.tier == 2:
        decision = "reject"
        confidence = 0.74
        rationale = f"Senior review resolved unclear context against {clause.citation}."
    else:
        decision = initial_verdict.decision
        confidence = max(initial_verdict.confidence, 0.8)
        rationale = f"Senior review confirmed specialist match to {clause.citation}."
    verdict = build_verdict(
        case_id=case.id,
        decision=decision,
        category=initial_verdict.category,
        confidence=confidence,
        rationale=rationale,
        reviewer="senior",
    )
    write_precedent(case, verdict, db_path)
    return verdict
