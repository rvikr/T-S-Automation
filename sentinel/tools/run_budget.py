"""Global daily budget for moderation runs.

The per-session UI cap resets on a page refresh and never covered the API at
all — so the operator's actual spend had no ceiling. This budget is stored in
SQLite, shared by every surface that accepts production content (API and UI),
and counts *accepted* moderation cases per UTC day.

Disabled by default (limit 0): whether to cap throughput is an operator
decision. Enable with ``SENTINEL_DAILY_LIVE_RUN_LIMIT=<n>``.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from pathlib import Path

from sentinel.tools.audit_log import db_connection, init_db

logger = logging.getLogger(__name__)

DAILY_BUDGET_ENV = "SENTINEL_DAILY_LIVE_RUN_LIMIT"


def daily_limit() -> int:
    raw = os.getenv(DAILY_BUDGET_ENV, "").strip()
    if not raw:
        return 0
    try:
        value = int(raw)
    except ValueError:
        logger.warning("%s=%r is not an integer; the daily budget is disabled", DAILY_BUDGET_ENV, raw)
        return 0
    return max(0, value)


def _today() -> str:
    return datetime.now(UTC).date().isoformat()


def seconds_until_utc_midnight() -> int:
    now = datetime.now(UTC)
    return max(1, 86_400 - (now.hour * 3600 + now.minute * 60 + now.second))


def consume_daily_budget(db_path: str | Path, limit: int | None = None) -> tuple[bool, int]:
    """Atomically consume one unit of today's budget.

    Returns ``(allowed, used_today)``. The conditional UPDATE makes the
    check-and-increment a single statement, so concurrent requests cannot
    overshoot the limit between a read and a write.
    """
    effective = daily_limit() if limit is None else max(0, int(limit))
    if effective <= 0:
        return True, 0
    day = _today()
    init_db(db_path)
    with db_connection(db_path) as conn:
        conn.execute("INSERT OR IGNORE INTO live_run_budget (day, count) VALUES (?, 0)", (day,))
        cursor = conn.execute(
            "UPDATE live_run_budget SET count = count + 1 WHERE day = ? AND count < ?",
            (day, effective),
        )
        allowed = cursor.rowcount == 1
        row = conn.execute("SELECT count FROM live_run_budget WHERE day = ?", (day,)).fetchone()
    return allowed, int(row[0]) if row else 0
