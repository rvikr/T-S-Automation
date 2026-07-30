"""Core data models for Sentinel.

Exported dataclasses and types:

* :class:`Case` — the asset being moderated (id, type, path, metadata).
* :class:`Verdict` — the moderation decision (decision, category, clause, etc.).
* :func:`build_verdict` — factory that validates and constructs a
  :class:`Verdict` with consistent Tier-1 and confidence-clamping logic.
* :class:`ProductionAssessment` — structured output from the agent runtime.
* :class:`Ticket` — a human-review escalation record.
* :class:`Audit` / :class:`ModerationLog` — audit trail entries.
* :class:`ApiKeyRecord` — a tenant API key entry.
* :class:`CaseResult` / :class:`BatchResult` — orchestrator return types.

Reserved ``Case.metadata`` keys (prefixed ``_`` are internal use only):

* ``analysis_mode`` *(str)* — ``"synthetic"`` or ``"production"``
* ``synthetic_label`` *(str)* — expected category label for synthetic cases
* ``expected_decision`` *(str)* — expected verdict for eval harness
* ``_evidence_input`` *(list)* — cached Responses-API input; popped before
  serialisation; external callers must not set this key
* ``detected_category`` *(str)* — category detected in production path
* ``agent_events`` *(list[str])* — per-run trace events accumulated by agents
* ``cited_clauses`` *(list[str])* — policy clause IDs cited during the run
* ``token_usage`` *(dict)* — token counts accumulated across specialist+senior
* ``latency_ms`` *(int)* — wall-clock milliseconds for the full run
* ``openai_trace_id`` *(str)* — OpenAI platform trace ID (production only)
* ``moderation_run_id`` *(str)* — unique correlation ID shared by an audit and
  any ticket created during that moderation run
* ``submission_id`` *(str)* — unique API upload-storage namespace
* ``api_key_id``, ``tenant_name``, ``project_name``, ``api_environment`` *(str)*
  — API attribution fields set by the REST layer
* ``source_system``, ``external_reference``, ``upload_filename``,
  ``upload_content_type`` *(str)* — metadata forwarded from the API request
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from sentinel.tools.policy_retrieval import PolicyClause


Decision = Literal["allow", "reject", "ambiguous"]

# Metadata key caching prepared Responses-API input (may hold large base64
# data-URLs). The senior run reuses it; the orchestrator must pop it before
# the case is serialized into results, logs, or API responses.
EVIDENCE_CACHE_KEY = "_evidence_input"


@dataclass(frozen=True)
class Case:
    id: str
    asset_type: str
    asset_path: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Verdict:
    case_id: str
    decision: Decision
    severity_tier: int
    category: str
    policy_clause: str
    confidence: float
    rationale: str
    reviewer: str


def build_verdict(
    case_id: str,
    decision: str,
    category: str,
    confidence: float,
    rationale: str,
    reviewer: str,
) -> Verdict:
    """Construct a :class:`Verdict` with consistent validation applied.

    Rules enforced here (single source of truth):
    - ``category`` is normalised against the taxonomy (aliases resolved).
    - An *unresolvable* category fails closed: if the decision was not ``allow``,
      the case is routed to human review as ``ambiguous`` rather than being
      relabelled ``"No Violation"``. Silently downgrading an unknown label to
      tier 0 would disarm the Tier-1 rail on a mere spelling mismatch.
    - Tier-1 categories always produce ``decision="ambiguous"`` with
      ``confidence >= 0.95`` (the deterministic safety rail).
    - ``confidence`` is clamped to ``[0.0, 1.0]``.
    """
    import logging

    from sentinel.tools.policy_retrieval import TIER1_CATEGORIES, get_clause_for_category, normalize_category

    resolved = normalize_category(category)
    if resolved is None:
        logging.getLogger(__name__).warning(
            "Unrecognised policy category %r on case %s; failing closed to human review", category, case_id
        )
        if decision != "allow":
            decision = "ambiguous"
            rationale = f"{rationale} [Unrecognised category '{category}'; routed to human review.]"
        category = "No Violation"
    else:
        category = resolved
    clause: PolicyClause = get_clause_for_category(category)

    if clause.tier == 1 or category in TIER1_CATEGORIES:
        decision = "ambiguous"
        confidence = max(float(confidence), 0.95)

    return Verdict(
        case_id=case_id,
        decision=decision,  # type: ignore[arg-type]
        severity_tier=clause.tier,
        category=category,
        policy_clause=clause.citation,
        confidence=max(0.0, min(float(confidence), 1.0)),
        rationale=rationale,
        reviewer=reviewer,
    )


@dataclass(frozen=True)
class ProductionAssessment:
    decision: Decision
    category: str
    confidence: float
    rationale: str
    evidence_summary: str
    reviewer_chain: list[str] = field(default_factory=list)
    agent_events: list[str] = field(default_factory=list)
    cited_clauses: list[str] = field(default_factory=list)
    usage_requests: int = 0
    usage_input_tokens: int = 0
    usage_output_tokens: int = 0
    usage_total_tokens: int = 0
    usage_model: str = ""


@dataclass(frozen=True)
class Ticket:
    id: str
    case_id: str
    severity: int
    category: str
    status: str
    created_at: str
    external_key: str | None = None
    external_url: str | None = None


@dataclass(frozen=True)
class Precedent:
    id: int
    embedding: str
    verdict: str
    category: str
    clause: str
    rationale: str


@dataclass(frozen=True)
class Audit:
    id: int
    case_id: str
    decision: str
    clause: str
    reviewer: str
    timestamp: str
    rationale: str = ""


@dataclass(frozen=True)
class ModerationLog:
    id: int
    case_id: str
    decision: str
    clause: str
    reviewer: str
    timestamp: str
    rationale: str
    escalation_triggered: bool
    escalation_type: str | None
    escalation_details: dict[str, Any]


@dataclass(frozen=True)
class ApiKeyRecord:
    key_id: str
    tenant_name: str
    project_name: str
    environment: str
    key_prefix: str
    status: str
    created_at: str
    last_used_at: str | None = None


@dataclass(frozen=True)
class CaseResult:
    case: Case
    verdict: Verdict
    trace: list[str]
    warning_message: str | None = None
    ticket: Ticket | None = None
    quarantined: bool = False


@dataclass(frozen=True)
class BatchResult:
    results: list[CaseResult]
    escalation_rate: float
