"""Content-hash cache for allow verdicts on production uploads.

Identical bytes moderated twice cost two full agent runs. This cache skips the
second run — but only in the safe direction: **only final ``allow`` verdicts
are ever cached**. Rejections, escalations, tickets, and quarantines always
re-run the full pipeline, so the cache can reduce cost on known-benign content
but can never suppress an enforcement action or an escalation.

Opt-in via ``SENTINEL_VERDICT_CACHE=1``: whether "same bytes ⇒ same verdict"
holds is a policy decision (context-dependent policies may say no), so the
operator makes it explicitly.
"""

from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path

from sentinel.models import Case, Verdict
from sentinel.tools.audit_log import db_connection, init_db, utc_now
from sentinel.tools.hash_match import file_sha256

logger = logging.getLogger(__name__)

VERDICT_CACHE_ENV = "SENTINEL_VERDICT_CACHE"

CACHE_REVIEWER = "cache"


def policy_fingerprint() -> str:
    """Stable digest of the active taxonomy's decision-relevant fields.

    Stored with every cache entry and required to match on lookup, so an allow
    granted under one policy can never be replayed under another — swapping
    SENTINEL_POLICY_FILE (or editing tiers) invalidates the whole cache
    implicitly. Summaries are included: they are what the agents reason from,
    so a reworded clause is a different policy even at the same tier.
    """
    from sentinel.tools.policy_retrieval import POLICY_CLAUSES

    material = ";".join(
        f"{clause.category}|{clause.tier}|{clause.clause_id}|{clause.summary}"
        for clause in sorted(POLICY_CLAUSES.values(), key=lambda clause: clause.category)
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def cache_enabled() -> bool:
    return os.getenv(VERDICT_CACHE_ENV, "").strip().lower() in {"1", "true", "yes"}


def _cacheable(case: Case) -> bool:
    return (
        cache_enabled()
        and case.metadata.get("analysis_mode") == "production"
        and Path(case.asset_path).is_file()
    )


def lookup_allow_verdict(case: Case, db_path: str | Path) -> Verdict | None:
    """Return a cached allow verdict for this exact content, or None."""
    if not _cacheable(case):
        return None
    try:
        content_hash = file_sha256(case.asset_path)
    except OSError:
        return None
    init_db(db_path)
    with db_connection(db_path) as conn:
        row = conn.execute(
            """
            SELECT category, clause, confidence, rationale
            FROM verdict_cache
            WHERE content_hash = ? AND asset_type = ? AND policy_fingerprint = ?
            """,
            (content_hash, case.asset_type, policy_fingerprint()),
        ).fetchone()
    if row is None:
        return None
    return Verdict(
        case_id=case.id,
        decision="allow",
        severity_tier=0,
        category=row[0],
        policy_clause=row[1],
        confidence=float(row[2]),
        rationale=f"Identical content previously allowed (verdict cache). Original rationale: {row[3]}",
        reviewer=CACHE_REVIEWER,
    )


def store_allow_verdict(case: Case, verdict: Verdict, db_path: str | Path) -> None:
    """Cache a final allow verdict. Silently refuses anything else."""
    if verdict.decision != "allow" or verdict.severity_tier != 0:
        return
    if verdict.reviewer == CACHE_REVIEWER:
        # A cache hit must not re-store itself and refresh its own entry.
        return
    if not _cacheable(case):
        return
    try:
        content_hash = file_sha256(case.asset_path)
    except OSError:
        return
    init_db(db_path)
    with db_connection(db_path) as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO verdict_cache (
                content_hash, asset_type, category, clause, confidence, rationale,
                created_at, policy_fingerprint
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                content_hash,
                case.asset_type,
                verdict.category,
                verdict.policy_clause,
                float(verdict.confidence),
                verdict.rationale,
                utc_now(),
                policy_fingerprint(),
            ),
        )
