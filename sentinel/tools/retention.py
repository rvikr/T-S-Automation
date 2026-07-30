"""Data-retention purge job.

Uploads, quarantine files, and verdict-cache rows otherwise persist forever —
a liability for a moderation service, whose stored content is by definition
sometimes the worst content. This module is the retention tool an operator
schedules (cron / Task Scheduler):

    python -m sentinel.tools.retention --uploads-days 30 --quarantine-days 90 --cache-days 30
    python -m sentinel.tools.retention --uploads-days 30 --dry-run

Only the categories explicitly passed are purged. Audit rows and tickets are
deliberately *not* purgeable here: they are the enforcement record, and
regulator-facing retention of decisions usually exceeds retention of content.

Quarantine deserves care: purging a quarantined asset before its ticket is
resolved destroys the evidence a human reviewer needs. Choose
``--quarantine-days`` longer than your review SLA.
"""

from __future__ import annotations

import argparse
import logging
import time
from datetime import UTC
from pathlib import Path

from sentinel.config import DEFAULT_DB_PATH, QUARANTINE_DIR, UPLOADS_DIR
from sentinel.tools.audit_log import db_connection, init_db

logger = logging.getLogger(__name__)

_SECONDS_PER_DAY = 86_400


def purge_old_files(root: str | Path, older_than_days: int, dry_run: bool = False) -> list[Path]:
    """Delete files under ``root`` whose mtime is older than the cutoff.

    Returns the files that were (or, in dry-run, would be) deleted. Empty
    directories left behind are removed too. Never raises for an individual
    file — one undeletable file must not abort the rest of the purge.
    """
    root_path = Path(root)
    if older_than_days < 0:
        raise ValueError("older_than_days must be >= 0")
    if not root_path.exists():
        return []
    cutoff = time.time() - older_than_days * _SECONDS_PER_DAY
    purged: list[Path] = []
    for path in sorted(root_path.rglob("*")):
        if not path.is_file():
            continue
        try:
            if path.stat().st_mtime >= cutoff:
                continue
            if not dry_run:
                path.unlink()
            purged.append(path)
        except OSError:
            logger.exception("Retention purge could not delete %s; continuing", path)
    if not dry_run:
        _remove_empty_dirs(root_path)
    return purged


def _remove_empty_dirs(root: Path) -> None:
    # Deepest-first so parents empty out as children are removed; the root
    # itself is kept.
    for directory in sorted((p for p in root.rglob("*") if p.is_dir()), reverse=True):
        try:
            directory.rmdir()
        except OSError:
            continue  # not empty (or not removable) — fine


def purge_cache_rows(db_path: str | Path, older_than_days: int, dry_run: bool = False) -> int:
    """Delete verdict-cache rows older than the cutoff; returns the row count."""
    if older_than_days < 0:
        raise ValueError("older_than_days must be >= 0")
    init_db(db_path)
    from datetime import datetime, timedelta

    cutoff = (datetime.now(UTC) - timedelta(days=older_than_days)).isoformat()
    with db_connection(db_path) as conn:
        if dry_run:
            row = conn.execute(
                "SELECT COUNT(*) FROM verdict_cache WHERE created_at < ?", (cutoff,)
            ).fetchone()
            return int(row[0])
        cursor = conn.execute("DELETE FROM verdict_cache WHERE created_at < ?", (cutoff,))
        return int(cursor.rowcount)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Purge aged Sentinel content per retention policy.")
    parser.add_argument("--uploads-days", type=int, default=None, help="Purge uploads older than N days.")
    parser.add_argument(
        "--quarantine-days",
        type=int,
        default=None,
        help="Purge quarantine files older than N days (choose longer than your review SLA).",
    )
    parser.add_argument("--cache-days", type=int, default=None, help="Purge verdict-cache rows older than N days.")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="SQLite DB path (for --cache-days).")
    parser.add_argument("--dry-run", action="store_true", help="Report what would be purged without deleting.")
    parser.add_argument(
        "--every-hours",
        type=float,
        default=None,
        help="Run forever, purging every N hours (sidecar mode). Omit for one shot.",
    )
    args = parser.parse_args(argv)

    if args.uploads_days is None and args.quarantine_days is None and args.cache_days is None:
        parser.error("Nothing to do: pass at least one of --uploads-days / --quarantine-days / --cache-days")
    if args.every_hours is not None and args.every_hours <= 0:
        parser.error("--every-hours must be positive")

    def _run_purges() -> None:
        prefix = "[dry-run] would purge" if args.dry_run else "purged"
        if args.uploads_days is not None:
            files = purge_old_files(UPLOADS_DIR, args.uploads_days, dry_run=args.dry_run)
            print(f"{prefix} {len(files)} upload file(s) older than {args.uploads_days}d from {UPLOADS_DIR}")
        if args.quarantine_days is not None:
            files = purge_old_files(QUARANTINE_DIR, args.quarantine_days, dry_run=args.dry_run)
            print(f"{prefix} {len(files)} quarantine file(s) older than {args.quarantine_days}d from {QUARANTINE_DIR}")
        if args.cache_days is not None:
            count = purge_cache_rows(args.db, args.cache_days, dry_run=args.dry_run)
            print(f"{prefix} {count} verdict-cache row(s) older than {args.cache_days}d from {args.db}")

    if args.every_hours is None:
        _run_purges()
        return 0
    while True:
        try:
            _run_purges()
        except Exception:
            # A failed purge must not kill the sidecar; the next interval retries.
            logger.exception("Scheduled retention purge failed; retrying next interval")
        time.sleep(args.every_hours * 3600)


if __name__ == "__main__":
    raise SystemExit(main())
