from __future__ import annotations

import sqlite3
from dataclasses import asdict
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from sentinel.models import Audit, ModerationLog, Ticket, Verdict


SCHEMA = """
CREATE TABLE IF NOT EXISTS audits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id TEXT NOT NULL,
    decision TEXT NOT NULL,
    clause TEXT NOT NULL,
    reviewer TEXT NOT NULL,
    rationale TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    api_key_id TEXT,
    tenant_name TEXT
);

CREATE TABLE IF NOT EXISTS tickets (
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    severity INTEGER NOT NULL,
    category TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    api_key_id TEXT,
    tenant_name TEXT
);

CREATE TABLE IF NOT EXISTS api_keys (
    id TEXT PRIMARY KEY,
    tenant_name TEXT NOT NULL,
    project_name TEXT NOT NULL,
    environment TEXT NOT NULL,
    key_prefix TEXT NOT NULL,
    key_hash TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_used_at TEXT
);

CREATE TABLE IF NOT EXISTS precedents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    embedding TEXT NOT NULL,
    verdict TEXT NOT NULL,
    category TEXT NOT NULL,
    clause TEXT NOT NULL,
    rationale TEXT NOT NULL,
    case_signature TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


@contextmanager
def db_connection(db_path: str | Path):
    conn = sqlite3.connect(db_path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(db_path: str | Path) -> Path:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with db_connection(path) as conn:
        conn.executescript(SCHEMA)
        _ensure_column(conn, "audits", "api_key_id", "TEXT")
        _ensure_column(conn, "audits", "tenant_name", "TEXT")
        _ensure_column(conn, "tickets", "api_key_id", "TEXT")
        _ensure_column(conn, "tickets", "tenant_name", "TEXT")
        _ensure_column(conn, "tickets", "external_key", "TEXT")
        _ensure_column(conn, "tickets", "external_url", "TEXT")
    return path


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_audit(
    verdict: Verdict,
    db_path: str | Path,
    api_key_id: str | None = None,
    tenant_name: str | None = None,
) -> Audit:
    init_db(db_path)
    timestamp = utc_now()
    with db_connection(db_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO audits (case_id, decision, clause, reviewer, rationale, timestamp, api_key_id, tenant_name)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                verdict.case_id,
                verdict.decision,
                verdict.policy_clause,
                verdict.reviewer,
                verdict.rationale,
                timestamp,
                api_key_id,
                tenant_name,
            ),
        )
        audit_id = int(cursor.lastrowid)
    return Audit(
        id=audit_id,
        case_id=verdict.case_id,
        decision=verdict.decision,
        clause=verdict.policy_clause,
        reviewer=verdict.reviewer,
        rationale=verdict.rationale,
        timestamp=timestamp,
    )


def fetch_audit_entries(db_path: str | Path) -> list[Audit]:
    init_db(db_path)
    with db_connection(db_path) as conn:
        rows = conn.execute(
            """
            SELECT id, case_id, decision, clause, reviewer, timestamp, rationale
            FROM audits
            ORDER BY id
            """
        ).fetchall()
    return [
        Audit(
            id=row[0],
            case_id=row[1],
            decision=row[2],
            clause=row[3],
            reviewer=row[4],
            timestamp=row[5],
            rationale=row[6],
        )
        for row in rows
    ]


def list_moderation_logs(db_path: str | Path, tenant_name: str | None = None) -> list[ModerationLog]:
    init_db(db_path)
    audit_filter = "WHERE tenant_name = ?" if tenant_name is not None else ""
    ticket_filter = "WHERE tenant_name = ?" if tenant_name is not None else ""
    audit_params = (tenant_name,) if tenant_name is not None else ()
    ticket_params = (tenant_name,) if tenant_name is not None else ()
    with db_connection(db_path) as conn:
        audit_rows = conn.execute(
            f"""
            SELECT id, case_id, decision, clause, reviewer, timestamp, rationale
            FROM audits
            {audit_filter}
            ORDER BY id DESC
            """,
            audit_params,
        ).fetchall()
        ticket_rows = conn.execute(
            f"""
            SELECT id, case_id, severity, category, status, created_at, external_key, external_url
            FROM tickets
            {ticket_filter}
            ORDER BY created_at DESC
            """,
            ticket_params,
        ).fetchall()

    tickets_by_case: dict[str, Ticket] = {}
    for row in ticket_rows:
        ticket = Ticket(
            id=row[0],
            case_id=row[1],
            severity=row[2],
            category=row[3],
            status=row[4],
            created_at=row[5],
            external_key=row[6],
            external_url=row[7],
        )
        tickets_by_case.setdefault(ticket.case_id, ticket)

    logs: list[ModerationLog] = []
    for row in audit_rows:
        audit = Audit(
            id=row[0],
            case_id=row[1],
            decision=row[2],
            clause=row[3],
            reviewer=row[4],
            timestamp=row[5],
            rationale=row[6],
        )
        ticket = tickets_by_case.get(audit.case_id)
        escalation_type, escalation_details = _escalation_details(audit, ticket)
        logs.append(
            ModerationLog(
                id=audit.id,
                case_id=audit.case_id,
                decision=audit.decision,
                clause=audit.clause,
                reviewer=audit.reviewer,
                timestamp=audit.timestamp,
                rationale=audit.rationale,
                escalation_triggered=escalation_type is not None,
                escalation_type=escalation_type,
                escalation_details=escalation_details,
            )
        )
    return logs


def _escalation_details(audit: Audit, ticket: Ticket | None) -> tuple[str | None, dict]:
    if ticket is not None:
        return (
            "human_ticket",
            {
                "reviewer": audit.reviewer,
                "status": ticket.status,
                "final_decision": audit.decision,
                "policy_clause": audit.clause,
                "rationale": audit.rationale,
                "ticket": asdict(ticket),
            },
        )
    if audit.reviewer == "senior":
        return (
            "senior_review",
            {
                "reviewer": "senior",
                "status": "resolved",
                "final_decision": audit.decision,
                "policy_clause": audit.clause,
                "rationale": audit.rationale,
            },
        )
    if audit.reviewer == "human":
        return (
            "human_review",
            {
                "reviewer": "human",
                "status": "pending",
                "final_decision": audit.decision,
                "policy_clause": audit.clause,
                "rationale": audit.rationale,
            },
        )
    return None, {}


def reset_db(db_path: str | Path) -> None:
    path = Path(db_path)
    if path.exists():
        path.unlink()
    init_db(path)
