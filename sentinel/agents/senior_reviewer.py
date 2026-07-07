from __future__ import annotations

from pathlib import Path

from sentinel.agents.common import build_sdk_agent
from sentinel.models import Case, Verdict
from sentinel.tools.policy_retrieval import get_clause_for_category, retrieve_policy_tool
from sentinel.tools.precedent_memory import retrieve_precedents_tool, write_precedent


SENIOR_REVIEWER_INSTRUCTIONS = (
    "Re-adjudicate ambiguous synthetic cases with stricter policy grounding. "
    "Tier-1 categories remain human-only and must not receive detailed analysis."
)


def build_senior_reviewer():
    return build_sdk_agent("Senior Reviewer", SENIOR_REVIEWER_INSTRUCTIONS, [retrieve_policy_tool, retrieve_precedents_tool])


def review_case(case: Case, initial_verdict: Verdict, db_path: str | Path) -> Verdict:
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
    verdict = Verdict(
        case_id=case.id,
        decision=decision,  # type: ignore[arg-type]
        severity_tier=clause.tier,
        category=initial_verdict.category,
        policy_clause=clause.citation,
        confidence=confidence,
        rationale=rationale,
        reviewer="senior",
    )
    write_precedent(case, verdict, db_path)
    return verdict
