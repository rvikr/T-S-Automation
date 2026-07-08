"""Human-escalation ticketing.

Deliberately NOT exposed as an agent function tool: escalation is a policy
invariant enforced by the orchestrator, so the AI can neither create nor skip
a ticket.
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from pathlib import Path

from sentinel.models import Case, Ticket
from sentinel.tools.audit_log import db_connection, init_db, utc_now


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
            INSERT INTO tickets (id, case_id, severity, category, status, created_at, api_key_id, tenant_name)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
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


