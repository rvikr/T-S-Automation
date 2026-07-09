from __future__ import annotations

import time
from dataclasses import replace
from pathlib import Path

try:
    from agents import gen_trace_id
    from agents import trace as sdk_trace
except ImportError:  # pragma: no cover - SDK installed in normal setup
    gen_trace_id = None
    sdk_trace = None

from sentinel.agents import audio_agent, image_agent, text_agent, video_agent
from sentinel.agents.senior_reviewer import review_case as senior_review
from sentinel.config import DEFAULT_DB_PATH, load_settings
from sentinel.guardrails import check_tier1_guardrail
from sentinel.models import EVIDENCE_CACHE_KEY, BatchResult, Case, CaseResult, Verdict
from sentinel.tools.audit_log import init_db, write_audit
from sentinel.tools.hash_match import hash_match, known_hash_match
from sentinel.tools.media_utils import detect_asset_type, quarantine
from sentinel.tools.jira_client import create_jira_issue
from sentinel.tools.policy_retrieval import get_clause_for_category
from sentinel.tools.ticketing import attach_external_reference, create_human_ticket


def _dispatch(case: Case, db_path: str | Path, trace: list[str]) -> Verdict:
    asset_type = detect_asset_type(case.asset_path, {"asset_type": case.asset_type})
    trace.append(f"orchestrator.detect_asset_type:{asset_type}")
    if case.metadata.get("analysis_mode") == "production":
        trace.append("production_analysis:enabled")
    if asset_type == "image":
        trace.append("route:image-agent")
        return image_agent.review_case(case, db_path)
    if asset_type == "audio":
        trace.append("route:audio-agent")
        return audio_agent.review_case(case, db_path)
    if asset_type == "video":
        trace.append("route:video-agent")
        return video_agent.review_case(case, db_path)
    trace.append("route:text-agent")
    return text_agent.review_case(case, db_path)


def _warning(verdict: Verdict) -> str | None:
    if verdict.decision != "reject":
        return None
    return (
        f"Upload rejected for {verdict.category}: violates {verdict.policy_clause}. "
        f"Rationale: {verdict.rationale}"
    )


def _human_only_verdict(case: Case, source_verdict: Verdict) -> Verdict:
    clause = get_clause_for_category(source_verdict.category)
    if case.metadata.get("analysis_mode") == "production":
        rationale = "Tier-1 signal detected; automated decision bypassed and routed to human review."
    else:
        rationale = "Tier-1 synthetic stand-in routed to human queue; automated decision bypassed."
    return Verdict(
        case_id=case.id,
        decision="ambiguous",
        severity_tier=1,
        category=source_verdict.category,
        policy_clause=clause.citation,
        confidence=1.0,
        rationale=rationale,
        reviewer="human",
    )


def _write_case_audit(case: Case, verdict: Verdict, db_path: str | Path):
    return write_audit(
        verdict,
        db_path,
        api_key_id=case.metadata.get("api_key_id"),
        tenant_name=case.metadata.get("tenant_name"),
    )


def _drain_agent_events(case: Case, trace: list[str]) -> None:
    for event in case.metadata.pop("agent_events", []):
        trace.append(f"agent.{event}")


def _escalate_ticket(case: Case, verdict: Verdict, ticket, db_path: str | Path, trace: list[str]):
    """Mirror the local ticket to Jira when configured; local ticket is never lost."""
    external = create_jira_issue(ticket, case, verdict)
    if external is None:
        trace.append("ticket.external:local-only")
        return ticket
    key, url = external
    trace.append(f"ticket.external:jira:{key}")
    return attach_external_reference(ticket, key, url, db_path)


def _tracing_enabled(case: Case) -> bool:
    """Only production runs with a real API key create OpenAI platform traces.

    The offline/synthetic path must never construct SDK objects: determinism
    and hermetic tests depend on it.
    """
    return (
        sdk_trace is not None
        and case.metadata.get("analysis_mode") == "production"
        and load_settings().openai_api_key_present
    )


def run_case(case: Case, db_path: str | Path = DEFAULT_DB_PATH) -> CaseResult:
    init_db(db_path)
    trace: list[str] = [f"ingest:{case.id}"]
    started = time.perf_counter()
    try:
        if _tracing_enabled(case):
            trace_id = gen_trace_id()
            case.metadata["openai_trace_id"] = trace_id
            trace.append(f"trace.openai:{trace_id}")
            with sdk_trace(
                "Sentinel moderation",
                trace_id=trace_id,
                group_id=case.id,
                metadata={"case_id": case.id, "modality": case.asset_type},
            ):
                return _run_case_inner(case, db_path, trace)
        return _run_case_inner(case, db_path, trace)
    finally:
        # Pop after both agent passes (the senior run reuses the cache) and
        # before the case object is serialized anywhere.
        case.metadata.pop(EVIDENCE_CACHE_KEY, None)
        # `trace` is the same list the CaseResult holds, so this lands in the result.
        latency_ms = round((time.perf_counter() - started) * 1000)
        case.metadata["latency_ms"] = latency_ms
        trace.append(f"latency:{latency_ms}ms")


def _run_case_inner(case: Case, db_path: str | Path, trace: list[str]) -> CaseResult:
    specialist_verdict = _dispatch(case, db_path, trace)
    _drain_agent_events(case, trace)
    trace.append(f"specialist.verdict:{specialist_verdict.decision}:{specialist_verdict.policy_clause}")

    guardrail = check_tier1_guardrail(specialist_verdict)
    if guardrail.triggered:
        trace.append("guardrail.tier1.triggered")
        trace.append(f"hash_match.flag:{hash_match(case) or known_hash_match(case.asset_path)}")
        quarantined = quarantine(case)
        final_verdict = _human_only_verdict(case, specialist_verdict)
        ticket = create_human_ticket(case, 1, specialist_verdict.category, db_path)
        trace.append("human_ticket.created")
        ticket = _escalate_ticket(case, final_verdict, ticket, db_path, trace)
        _write_case_audit(case, final_verdict, db_path)
        trace.append("quarantine.completed")
        return CaseResult(case=case, verdict=final_verdict, trace=trace, ticket=ticket, quarantined=quarantined)

    if "agent.guardrail.input.injection" in trace:
        # The upload tried to manipulate the moderator. Straight to a human
        # ticket — never re-run another agent over the same hostile input.
        trace.append("guardrail.input.triggered")
        final_verdict = replace(specialist_verdict, decision="ambiguous", reviewer="human")
        ticket = create_human_ticket(case, final_verdict.severity_tier, final_verdict.category, db_path)
        trace.append("human_ticket.created")
        ticket = _escalate_ticket(case, final_verdict, ticket, db_path, trace)
        _write_case_audit(case, final_verdict, db_path)
        return CaseResult(case=case, verdict=final_verdict, trace=trace, ticket=ticket)

    if specialist_verdict.decision == "ambiguous":
        if specialist_verdict.reviewer == "senior":
            # The specialist already handed off to the senior agent inside the run.
            trace.append("handoff:senior-reviewer:in-run")
            final_verdict = specialist_verdict
        else:
            trace.append("route:senior-reviewer")
            final_verdict = senior_review(case, specialist_verdict, db_path)
            _drain_agent_events(case, trace)
        trace.append(f"senior.verdict:{final_verdict.decision}:{final_verdict.policy_clause}")
        if final_verdict.decision == "ambiguous":
            ticket = create_human_ticket(case, final_verdict.severity_tier, final_verdict.category, db_path)
            trace.append("human_ticket.created")
            ticket = _escalate_ticket(case, final_verdict, ticket, db_path, trace)
            quarantined = quarantine(case)
            _write_case_audit(case, final_verdict, db_path)
            return CaseResult(
                case=case,
                verdict=final_verdict,
                trace=trace,
                ticket=ticket,
                quarantined=quarantined,
                warning_message=_warning(final_verdict),
            )
        _write_case_audit(case, final_verdict, db_path)
        return CaseResult(case=case, verdict=final_verdict, trace=trace, warning_message=_warning(final_verdict))

    _write_case_audit(case, specialist_verdict, db_path)
    return CaseResult(case=case, verdict=specialist_verdict, trace=trace, warning_message=_warning(specialist_verdict))


def run_batch(cases: list[Case], db_path: str | Path = DEFAULT_DB_PATH) -> BatchResult:
    results = [run_case(case, db_path=db_path) for case in cases]
    eligible = [result for result in results if result.verdict.severity_tier != 1]
    escalated = [
        result
        for result in eligible
        if result.verdict.reviewer in {"senior", "human"} or result.ticket is not None
    ]
    escalation_rate = len(escalated) / len(eligible) if eligible else 0.0
    return BatchResult(results=results, escalation_rate=escalation_rate)
