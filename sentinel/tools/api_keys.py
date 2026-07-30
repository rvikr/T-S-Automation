"""API key lifecycle management for Sentinel.

Handles creation, hashing (SHA-256), validation, listing, revocation,
expiry, and rotation of tenant API keys. Each key is stored as a hash only —
the plaintext is returned once (on creation or rotation) and never persisted.

Key format: ``sent_<env>_<random>``
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sentinel.models import ApiKeyRecord
from sentinel.tools.audit_log import db_connection, init_db, utc_now

VALID_ENVIRONMENTS = {"test", "live"}

# The grants a key can carry. "moderate" = submit cases; "logs" = read the
# tenant's moderation logs.
VALID_SCOPES = {"moderate", "logs"}
DEFAULT_SCOPES = "moderate,logs"

_KEY_COLUMNS = (
    "id, tenant_name, project_name, environment, key_prefix, status, created_at, "
    "last_used_at, expires_at, scopes, created_by"
)


def create_api_key(
    db_path: str | Path,
    tenant_name: str,
    project_name: str,
    environment: str,
    expires_in_days: int | None = None,
    scopes: list[str] | None = None,
    created_by: str | None = None,
) -> dict:
    init_db(db_path)
    normalized_environment = _normalize_environment(environment)
    normalized_scopes = _normalize_scopes(scopes)
    expires_at = _expiry_timestamp(expires_in_days)
    key_id = f"key_{secrets.token_urlsafe(12).replace('-', '').replace('_', '')[:16]}"
    key_prefix = f"sent_{normalized_environment}"
    api_key = f"{key_prefix}_{secrets.token_urlsafe(32)}"
    created_at = utc_now()
    with db_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO api_keys (
                id, tenant_name, project_name, environment, key_prefix,
                key_hash, status, created_at, last_used_at, expires_at,
                scopes, created_by
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                expires_at,
                normalized_scopes,
                created_by,
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
        "expires_at": expires_at,
        "scopes": normalized_scopes,
        "created_by": created_by,
    }


def _normalize_scopes(scopes: list[str] | None) -> str:
    if scopes is None:
        return DEFAULT_SCOPES
    cleaned = sorted({str(scope).strip().lower() for scope in scopes if str(scope).strip()})
    if not cleaned:
        raise ValueError("scopes must not be empty; omit the field for full access")
    unknown = [scope for scope in cleaned if scope not in VALID_SCOPES]
    if unknown:
        raise ValueError(f"Unknown scopes: {', '.join(unknown)}. Valid scopes: {', '.join(sorted(VALID_SCOPES))}")
    return ",".join(cleaned)


def rotate_api_key(
    db_path: str | Path,
    key_id: str,
    expires_in_days: int | None = None,
    rotated_by: str | None = None,
) -> dict | None:
    """Revoke a key and mint its replacement for the same tenant/project/env.

    Scopes carry over unchanged — rotation refreshes the secret, it must not
    silently widen (or narrow) what the key can do. Returns the new-key payload
    (plaintext shown once, as at creation) with a ``rotated_from`` field, or
    ``None`` when the key does not exist or is not active — rotating a revoked
    key would silently resurrect its access.
    """
    init_db(db_path)
    with db_connection(db_path) as conn:
        row = conn.execute(
            "SELECT tenant_name, project_name, environment, status, scopes FROM api_keys WHERE id = ?",
            (key_id,),
        ).fetchone()
        if row is None or row[3] != "active":
            return None
        conn.execute("UPDATE api_keys SET status = ? WHERE id = ?", ("revoked", key_id))
    carried_scopes = (row[4] or DEFAULT_SCOPES).split(",")
    replacement = create_api_key(
        db_path,
        tenant_name=row[0],
        project_name=row[1],
        environment=row[2],
        expires_in_days=expires_in_days,
        scopes=carried_scopes,
        created_by=rotated_by,
    )
    replacement["rotated_from"] = key_id
    return replacement


def list_api_keys(db_path: str | Path) -> list[ApiKeyRecord]:
    init_db(db_path)
    with db_connection(db_path) as conn:
        rows = conn.execute(
            f"""
            SELECT {_KEY_COLUMNS}
            FROM api_keys
            ORDER BY created_at DESC
            """
        ).fetchall()
    return [_record_from_row(row) for row in rows]


def revoke_api_key(db_path: str | Path, key_id: str) -> ApiKeyRecord | None:
    init_db(db_path)
    with db_connection(db_path) as conn:
        row = conn.execute(
            f"""
            SELECT {_KEY_COLUMNS}
            FROM api_keys
            WHERE id = ?
            """,
            (key_id,),
        ).fetchone()
        if row is None:
            return None
        conn.execute("UPDATE api_keys SET status = ? WHERE id = ?", ("revoked", key_id))
    from dataclasses import replace

    return replace(_record_from_row(row), status="revoked")


def authenticate_api_key(db_path: str | Path, api_key: str) -> ApiKeyRecord | None:
    init_db(db_path)
    if not api_key.startswith(("sent_test_", "sent_live_")):
        return None
    key_hash = _hash_api_key(api_key)
    now = utc_now()
    with db_connection(db_path) as conn:
        row = conn.execute(
            f"""
            SELECT {_KEY_COLUMNS}
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
        if _is_expired(record.expires_at):
            return None
        conn.execute("UPDATE api_keys SET last_used_at = ? WHERE id = ?", (now, record.key_id))
    # dataclasses.replace, not reconstruction: rebuilding field-by-field is how
    # scopes silently fell back to the full-access default once already.
    from dataclasses import replace

    return replace(record, last_used_at=now)


def _expiry_timestamp(expires_in_days: int | None) -> str | None:
    if expires_in_days is None:
        return None
    days = int(expires_in_days)
    if days < 1:
        raise ValueError("expires_in_days must be a positive number of days")
    return (datetime.now(UTC) + timedelta(days=days)).isoformat()


def _is_expired(expires_at: str | None) -> bool:
    if not expires_at:
        return False
    try:
        expiry = datetime.fromisoformat(expires_at)
    except ValueError:
        # An unparseable expiry fails closed: a key whose lifetime cannot be
        # established must not authenticate.
        return True
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=UTC)
    return datetime.now(UTC) >= expiry


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
        expires_at=row[8],
        scopes=row[9] or DEFAULT_SCOPES,
        created_by=row[10],
    )
