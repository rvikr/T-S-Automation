"""Human-escalation ticketing.

Deliberately NOT exposed as an agent function tool: escalation is a policy
invariant enforced by the orchestrator, so the AI can neither create nor skip
a ticket. Resolution (:func:`resolve_ticket`) is likewise human-only — it is
called from the review-queue UI, never from an agent.
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from pathlib import Path

from sentinel.models import Case, Ticket, Verdict
from sentinel.tools.audit_log import db_connection, init_db, utc_now, write_audit
from sentinel.tools.policy_retrieval import get_clause_for_category


def create_human_ticket(case: Case, severity: int, category: str, db_path: str | Path) -> Ticket:
    init_db(db_path)
    ticket = Ticket(
        id=f"TKT-{uuid.uuid4().hex[:8].upper()}",
        case_id=case.id,
        severity=severity,
        category=category,
        status="open",
        created_at=utc_now(),
    )
    with db_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO tickets (
                id, case_id, severity, category, status, created_at,
                api_key_id, tenant_name, run_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ticket.id,
                ticket.case_id,
                ticket.severity,
                ticket.category,
                ticket.status,
                ticket.created_at,
                case.metadata.get("api_key_id"),
                case.metadata.get("tenant_name"),
                case.metadata.get("moderation_run_id"),
            ),
        )
    return ticket


def attach_external_reference(ticket: Ticket, external_key: str, external_url: str, db_path: str | Path) -> Ticket:
    """Record the external (e.g. Jira) issue on an existing local ticket."""
    with db_connection(db_path) as conn:
        conn.execute(
            "UPDATE tickets SET external_key = ?, external_url = ? WHERE id = ?",
            (external_key, external_url, ticket.id),
        )
    return replace(ticket, external_key=external_key, external_url=external_url)


# Terminal ticket statuses a human resolution can produce.
RESOLUTION_STATUSES = {"allow": "resolved-allow", "reject": "resolved-reject"}


def resolve_ticket(
    ticket_id: str,
    decision: str,
    rationale: str,
    db_path: str | Path,
) -> Ticket | None:
    """Resolve an open human-review ticket with a final allow/reject decision.

    Writes the human verdict to the audit log under the ticket's original
    ``run_id`` so the moderation-log view joins the resolution to the case that
    escalated it. Returns the updated ticket, or ``None`` when the ticket does
    not exist or is no longer open (a second reviewer racing on the same ticket
    must not double-audit).

    Tier-1 tickets can be resolved here: the human reviewer *is* the authority
    the rails escalate to, so their decision is final — unlike ``build_verdict``,
    which forces Tier-1 back to ``ambiguous`` for automated reviewers.
    """
    if decision not in RESOLUTION_STATUSES:
        raise ValueError(f"decision must be one of {sorted(RESOLUTION_STATUSES)}, not {decision!r}")
    if not rationale.strip():
        raise ValueError("A human resolution requires a rationale for the audit trail")

    init_db(db_path)
    new_status = RESOLUTION_STATUSES[decision]
    with db_connection(db_path) as conn:
        row = conn.execute(
            """
            SELECT id, case_id, severity, category, status, created_at,
                   external_key, external_url, api_key_id, tenant_name, run_id
            FROM tickets
            WHERE id = ?
            """,
            (ticket_id,),
        ).fetchone()
        if row is None or row[4] != "open":
            return None
        conn.execute("UPDATE tickets SET status = ? WHERE id = ?", (new_status, ticket_id))

    clause = get_clause_for_category(row[3])
    verdict = Verdict(
        case_id=row[1],
        decision=decision,  # type: ignore[arg-type]
        severity_tier=row[2],
        category=row[3],
        policy_clause=clause.citation,
        confidence=1.0,
        rationale=rationale.strip(),
        reviewer="human",
    )
    write_audit(verdict, db_path, api_key_id=row[8], tenant_name=row[9], run_id=row[10])
    return Ticket(
        id=row[0],
        case_id=row[1],
        severity=row[2],
        category=row[3],
        status=new_status,
        created_at=row[5],
        external_key=row[6],
        external_url=row[7],
    )


def list_human_tickets(db_path: str | Path) -> list[Ticket]:
    init_db(db_path)
    with db_connection(db_path) as conn:
        rows = conn.execute(
            """
            SELECT id, case_id, severity, category, status, created_at, external_key, external_url
            FROM tickets
            ORDER BY created_at DESC
            """
        ).fetchall()
    return [
        Ticket(
            id=row[0],
            case_id=row[1],
            severity=row[2],
            category=row[3],
            status=row[4],
            created_at=row[5],
            external_key=row[6],
            external_url=row[7],
        )
        for row in rows
    ]


