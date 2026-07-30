from __future__ import annotations

import logging
import sqlite3
import time
import uuid
from dataclasses import replace
from pathlib import Path

try:
    from agents import gen_trace_id
    from agents import trace as sdk_trace
except ImportError:  # pragma: no cover - SDK installed in normal setup
    gen_trace_id = None  # type: ignore[assignment]
    sdk_trace = None  # type: ignore[assignment]

from sentinel.agents import audio_agent, image_agent, text_agent, video_agent
from sentinel.agents.senior_reviewer import review_case as senior_review
from sentinel.config import DEFAULT_DB_PATH, MIRROR_UNADJUDICATED_TO_JIRA, load_settings
from sentinel.guardrails import check_tier1_guardrail
from sentinel.models import EVIDENCE_CACHE_KEY, BatchResult, Case, CaseResult, Verdict
from sentinel.tools.audit_log import init_db, write_audit
from sentinel.tools.hash_match import hash_match, known_hash_match
from sentinel.tools.jira_client import create_jira_issue
from sentinel.tools.media_utils import ASSET_TYPE_MISMATCH_KEY, detect_asset_type, quarantine
from sentinel.tools.policy_retrieval import get_clause_for_category
from sentinel.tools.ticketing import attach_external_reference, create_human_ticket
from sentinel.tools.verdict_cache import lookup_allow_verdict, store_allow_verdict

logger = logging.getLogger(__name__)


def _asset_type_mismatch_verdict(case: Case, mismatch: dict) -> Verdict:
    """Fail closed when the declared asset type contradicts the file's bytes.

    A disagreement here is an evasion signal in its own right — the most direct
    way to keep image or video content away from a vision-capable agent is to
    label it as text. We route to a human without spending an LLM call rather
    than silently re-routing, so the attempt is visible in the audit trail.
    """
    clause = get_clause_for_category("No Violation")
    return Verdict(
        case_id=case.id,
        decision="ambiguous",
        severity_tier=clause.tier,
        category="No Violation",
        policy_clause=clause.citation,
        confidence=1.0,
        rationale=(
            f"Declared asset type '{mismatch['declared']}' contradicts detected content type "
            f"'{mismatch['detected']}'. Automated adjudication bypassed and routed to human review."
        ),
        reviewer="human",
    )


def _dispatch(case: Case, db_path: str | Path, trace: list[str]) -> Verdict:
    probe: dict = {"asset_type": case.asset_type}
    asset_type = detect_asset_type(case.asset_path, probe)
    trace.append(f"orchestrator.detect_asset_type:{asset_type}")

    mismatch = probe.get(ASSET_TYPE_MISMATCH_KEY)
    if mismatch:
        case.metadata[ASSET_TYPE_MISMATCH_KEY] = mismatch
        trace.append(
            f"guardrail.asset_type_mismatch:declared={mismatch['declared']},detected={mismatch['detected']}"
        )
        return _asset_type_mismatch_verdict(case, mismatch)

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
    """Persist the audit row, but never let its failure discard an escalation.

    Audit writes happen *after* quarantine and ticket creation have already
    committed. Letting a transient SQLite lock propagate here would surface as a
    failed request even though the content was correctly quarantined and
    escalated — the caller would likely retry, double-ticketing. A missing audit
    row is the lesser harm, provided it is loud.
    """
    try:
        return write_audit(
            verdict,
            db_path,
            api_key_id=case.metadata.get("api_key_id"),
            tenant_name=case.metadata.get("tenant_name"),
            run_id=case.metadata.get("moderation_run_id"),
        )
    except sqlite3.Error:
        logger.exception(
            "Audit write failed for case %s (verdict=%s, category=%s); "
            "enforcement already applied, audit row missing",
            case.id,
            verdict.decision,
            verdict.category,
        )
        return None


def _drain_agent_events(case: Case, trace: list[str]) -> None:
    for event in case.metadata.pop("agent_events", []):
        trace.append(f"agent.{event}")


# Agent events meaning no model actually classified the content: credentials
# absent, or the call failed. Both leave the verdict at "No Violation / tier 0"
# while the rails still escalate, so the escalation is real but the *verdict*
# carries no information worth filing externally.
_UNADJUDICATED_EVENT_MARKERS = ("production_analysis.unavailable", "agent_runtime.error:")


def _was_adjudicated(case: Case, trace: list[str]) -> bool:
    """True when a model actually classified this content.

    Synthetic runs count as adjudicated — their labels drive genuine verdicts and
    the offline Tier-1 demo legitimately mirrors to Jira. This is specifically
    about production uploads whose model call never happened or failed.
    """
    if case.metadata.get("analysis_mode") != "production":
        return True
    return not any(marker in event for event in trace for marker in _UNADJUDICATED_EVENT_MARKERS)


def _escalate_ticket(case: Case, verdict: Verdict, ticket, db_path: str | Path, trace: list[str]):
    """Mirror the local ticket to Jira when configured; local ticket is never lost."""
    if not MIRROR_UNADJUDICATED_TO_JIRA and not _was_adjudicated(case, trace):
        # Without a working model *every* production upload escalates, so
        # mirroring here opens a Jira issue per request — benign content
        # included — for a verdict no model produced. Skip the external mirror
        # only; the local ticket below still records the escalation.
        trace.append("ticket.external:skipped-unadjudicated")
        return ticket
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
    case.metadata["moderation_run_id"] = uuid.uuid4().hex
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
                result = _run_case_inner(case, db_path, trace)
        else:
            result = _run_case_inner(case, db_path, trace)
        # Cache only the safe direction: a final allow with no ticket. The
        # store helper re-checks decision/tier/reviewer, so escalations and
        # rejections can never populate the cache.
        if result.ticket is None and not result.quarantined:
            store_allow_verdict(case, result.verdict, db_path)
        return result
    finally:
        # Pop after both agent passes (the senior run reuses the cache) and
        # before the case object is serialized anywhere.
        case.metadata.pop(EVIDENCE_CACHE_KEY, None)
        # `trace` is the same list the CaseResult holds, so this lands in the result.
        latency_ms = round((time.perf_counter() - started) * 1000)
        case.metadata["latency_ms"] = latency_ms
        trace.append(f"latency:{latency_ms}ms")


def _handle_tier1_guardrail(
    case: Case, verdict: Verdict, db_path: str | Path, trace: list[str]
) -> CaseResult:
    """Quarantine, ticket, and return for Tier-1 triggered cases."""
    trace.append("guardrail.tier1.triggered")
    trace.append(f"hash_match.flag:{hash_match(case) or known_hash_match(case.asset_path)}")
    quarantined = quarantine(case)
    final_verdict = _human_only_verdict(case, verdict)
    ticket = create_human_ticket(case, 1, verdict.category, db_path)
    trace.append("human_ticket.created")
    ticket = _escalate_ticket(case, final_verdict, ticket, db_path, trace)
    _write_case_audit(case, final_verdict, db_path)
    trace.append("quarantine.completed")
    return CaseResult(case=case, verdict=final_verdict, trace=trace, ticket=ticket, quarantined=quarantined)


def _handle_direct_human_escalation(
    case: Case, verdict: Verdict, db_path: str | Path, trace: list[str], reason: str = "guardrail.input.triggered"
) -> CaseResult:
    """Ticket straight to a human without re-running any agent.

    Used for inputs that are hostile or untrustworthy in themselves — prompt
    injection, or a declared asset type that contradicts the file's bytes. In
    both cases a second adjudication pass would be re-reading attacker-chosen
    input, and a lenient outcome from it would undo the rail that fired.
    """
    trace.append(reason)
    final_verdict = replace(verdict, decision="ambiguous", reviewer="human")
    ticket = create_human_ticket(case, final_verdict.severity_tier, final_verdict.category, db_path)
    trace.append("human_ticket.created")
    ticket = _escalate_ticket(case, final_verdict, ticket, db_path, trace)
    _write_case_audit(case, final_verdict, db_path)
    return CaseResult(case=case, verdict=final_verdict, trace=trace, ticket=ticket)


def _handle_ambiguous_escalation(
    case: Case, specialist_verdict: Verdict, db_path: str | Path, trace: list[str]
) -> CaseResult:
    """Escalate an ambiguous specialist verdict to the senior reviewer."""
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


def _run_case_inner(case: Case, db_path: str | Path, trace: list[str]) -> CaseResult:
    cached_verdict = lookup_allow_verdict(case, db_path)
    if cached_verdict is not None:
        # Only allow verdicts are ever cached, so a hit can skip the agents
        # without ever skipping an enforcement action or escalation.
        trace.append("cache.hit:allow")
        _write_case_audit(case, cached_verdict, db_path)
        return CaseResult(case=case, verdict=cached_verdict, trace=trace)

    specialist_verdict = _dispatch(case, db_path, trace)
    _drain_agent_events(case, trace)
    trace.append(f"specialist.verdict:{specialist_verdict.decision}:{specialist_verdict.policy_clause}")

    if check_tier1_guardrail(specialist_verdict).triggered:
        return _handle_tier1_guardrail(case, specialist_verdict, db_path, trace)

    if "agent.guardrail.input.injection" in trace:
        # The upload tried to manipulate the moderator. Straight to a human
        # ticket — never re-run another agent over the same hostile input.
        return _handle_direct_human_escalation(case, specialist_verdict, db_path, trace)

    if ASSET_TYPE_MISMATCH_KEY in case.metadata:
        # The declared type contradicted the bytes. Handing this to the senior
        # reviewer would just re-adjudicate it — and a senior "allow" would undo
        # the rail. Ticket it for a human directly, as with prompt injection.
        return _handle_direct_human_escalation(
            case, specialist_verdict, db_path, trace, reason="guardrail.asset_type_mismatch.triggered"
        )

    if specialist_verdict.decision == "ambiguous":
        return _handle_ambiguous_escalation(case, specialist_verdict, db_path, trace)

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
