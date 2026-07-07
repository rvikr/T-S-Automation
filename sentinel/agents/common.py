from __future__ import annotations

from pathlib import Path

try:
    from agents import Agent, handoff
except ImportError:  # pragma: no cover
    Agent = None

    def handoff(agent):
        return agent

from sentinel.models import Case, Verdict
from sentinel.tools.policy_retrieval import get_clause_for_category
from sentinel.tools.precedent_memory import retrieve_precedents


def build_sdk_agent(name: str, instructions: str, tools: list | None = None, handoffs: list | None = None):
    if Agent is None:
        return None
    return Agent(name=name, instructions=instructions, tools=tools or [], handoffs=handoffs or [])


def specialist_review(case: Case, reviewer: str, db_path: str | Path) -> Verdict:
    if case.metadata.get("analysis_mode") == "production":
        from sentinel.tools.production_analysis import production_review

        return production_review(case, reviewer, db_path)

    category = str(case.metadata.get("expected_category", "No Violation"))
    expected = str(case.metadata.get("expected_decision", "allow"))
    clause = get_clause_for_category(category)
    precedents = retrieve_precedents(case, db_path)
    if precedents and expected == "ambiguous":
        precedent = precedents[0]
        return Verdict(
            case_id=case.id,
            decision=precedent.verdict,  # type: ignore[arg-type]
            severity_tier=clause.tier,
            category=category,
            policy_clause=precedent.clause,
            confidence=0.86,
            rationale=f"Resolved by nearest precedent #{precedent.id}: {precedent.rationale}",
            reviewer=reviewer,
        )
    if clause.tier == 1:
        decision = "ambiguous"
        confidence = 1.0
        rationale = "Synthetic Tier-1 label detected; automated adjudication is bypassed."
    elif expected == "reject":
        decision = "reject"
        confidence = 0.93
        rationale = f"Synthetic label clearly matches {clause.citation}."
    elif expected == "ambiguous":
        decision = "ambiguous"
        confidence = 0.54
        rationale = f"Synthetic label may match {clause.citation}, but context is unclear."
    else:
        decision = "allow"
        confidence = 0.95
        rationale = "Synthetic benign label has no policy violation signal."
    return Verdict(
        case_id=case.id,
        decision=decision,  # type: ignore[arg-type]
        severity_tier=clause.tier,
        category=category,
        policy_clause=clause.citation,
        confidence=confidence,
        rationale=rationale,
        reviewer=reviewer,
    )
