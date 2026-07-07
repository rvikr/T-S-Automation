from __future__ import annotations

import hashlib
import hmac
import secrets
from pathlib import Path

from sentinel.models import ApiKeyRecord
from sentinel.tools.audit_log import db_connection, init_db, utc_now


VALID_ENVIRONMENTS = {"test", "live"}


def create_api_key(
    db_path: str | Path,
    tenant_name: str,
    project_name: str,
    environment: str,
) -> dict:
    init_db(db_path)
    normalized_environment = _normalize_environment(environment)
    key_id = f"key_{secrets.token_urlsafe(12).replace('-', '').replace('_', '')[:16]}"
    key_prefix = f"sent_{normalized_environment}"
    api_key = f"{key_prefix}_{secrets.token_urlsafe(32)}"
    created_at = utc_now()
    with db_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO api_keys (
                id, tenant_name, project_name, environment, key_prefix,
                key_hash, status, created_at, last_used_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                key_id,
                tenant_name,
                project_name,
                normalized_environment,
                key_prefix,
                _hash_api_key(api_key),
                "active",
                created_at,
                None,
            ),
        )
    return {
        "api_key": api_key,
        "key_id": key_id,
        "tenant_name": tenant_name,
        "project_name": project_name,
        "environment": normalized_environment,
        "key_prefix": key_prefix,
        "status": "active",
        "created_at": created_at,
    }


def list_api_keys(db_path: str | Path) -> list[ApiKeyRecord]:
    init_db(db_path)
    with db_connection(db_path) as conn:
        rows = conn.execute(
            """
            SELECT id, tenant_name, project_name, environment, key_prefix, status, created_at, last_used_at
            FROM api_keys
            ORDER BY created_at DESC
            """
        ).fetchall()
    return [_record_from_row(row) for row in rows]


def revoke_api_key(db_path: str | Path, key_id: str) -> ApiKeyRecord | None:
    init_db(db_path)
    with db_connection(db_path) as conn:
        row = conn.execute(
            """
            SELECT id, tenant_name, project_name, environment, key_prefix, status, created_at, last_used_at
            FROM api_keys
            WHERE id = ?
            """,
            (key_id,),
        ).fetchone()
        if row is None:
            return None
        conn.execute("UPDATE api_keys SET status = ? WHERE id = ?", ("revoked", key_id))
    record = _record_from_row(row)
    return ApiKeyRecord(
        key_id=record.key_id,
        tenant_name=record.tenant_name,
        project_name=record.project_name,
        environment=record.environment,
        key_prefix=record.key_prefix,
        status="revoked",
        created_at=record.created_at,
        last_used_at=record.last_used_at,
    )


def authenticate_api_key(db_path: str | Path, api_key: str) -> ApiKeyRecord | None:
    init_db(db_path)
    if not api_key.startswith(("sent_test_", "sent_live_")):
        return None
    key_hash = _hash_api_key(api_key)
    now = utc_now()
    with db_connection(db_path) as conn:
        row = conn.execute(
            """
            SELECT id, tenant_name, project_name, environment, key_prefix, status, created_at, last_used_at
            FROM api_keys
            WHERE key_hash = ?
            """,
            (key_hash,),
        ).fetchone()
        if row is None:
            return None
        record = _record_from_row(row)
        if not hmac.compare_digest(record.status, "active"):
            return None
        conn.execute("UPDATE api_keys SET last_used_at = ? WHERE id = ?", (now, record.key_id))
    return ApiKeyRecord(
        key_id=record.key_id,
        tenant_name=record.tenant_name,
        project_name=record.project_name,
        environment=record.environment,
        key_prefix=record.key_prefix,
        status=record.status,
        created_at=record.created_at,
        last_used_at=now,
    )


def _hash_api_key(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def _normalize_environment(environment: str) -> str:
    normalized = environment.lower().strip()
    if normalized not in VALID_ENVIRONMENTS:
        raise ValueError("environment must be either 'test' or 'live'")
    return normalized


def _record_from_row(row) -> ApiKeyRecord:
    return ApiKeyRecord(
        key_id=row[0],
        tenant_name=row[1],
        project_name=row[2],
        environment=row[3],
        key_prefix=row[4],
        status=row[5],
        created_at=row[6],
        last_used_at=row[7],
    )
