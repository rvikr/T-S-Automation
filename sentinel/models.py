from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


Decision = Literal["allow", "reject", "ambiguous"]


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
