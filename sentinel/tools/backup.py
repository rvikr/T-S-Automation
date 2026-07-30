"""Audit-database backups.

The SQLite file is the only copy of the audit trail — the record the whole
accountability story rests on. This tool takes a consistent snapshot using
SQLite's online backup API (safe while the app is running, WAL included) and
rotates old snapshots:

    python -m sentinel.tools.backup --output-dir sentinel/db/backups --keep 14
    python -m sentinel.tools.backup --output-dir D:/backups --keep 30 --every-hours 24

Schedule it (Windows Task Scheduler / cron), run the ``--every-hours`` loop as
a sidecar process, or use the ``ops`` profile in docker-compose. ChromaDB is
deliberately not backed up: both the policy index and the precedent vectors
are derived data, rebuildable from the SQLite tables and the policy corpus.

A file copy (``copy audit.sqlite``) is NOT a safe backup while the service is
running — a write mid-copy yields a torn, unreadable database. The backup API
is why this tool exists.
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path

from sentinel.config import DEFAULT_DB_PATH

logger = logging.getLogger(__name__)

BACKUP_SUFFIX = ".sqlite"


def create_backup(db_path: str | Path, output_dir: str | Path) -> Path:
    """Snapshot the database into ``output_dir``; returns the snapshot path."""
    source_path = Path(db_path)
    if not source_path.exists():
        raise FileNotFoundError(f"No database to back up at {source_path}")
    target_dir = Path(output_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    target = target_dir / f"audit-{stamp}{BACKUP_SUFFIX}"

    source = sqlite3.connect(source_path)
    try:
        destination = sqlite3.connect(target)
        try:
            source.backup(destination)
        finally:
            destination.close()
    finally:
        source.close()

    # A snapshot that cannot answer a query is not a backup.
    verify = sqlite3.connect(target)
    try:
        verify.execute("SELECT count(*) FROM sqlite_master").fetchone()
    finally:
        verify.close()
    return target


def rotate_backups(output_dir: str | Path, keep: int) -> list[Path]:
    """Delete all but the newest ``keep`` snapshots; returns what was removed."""
    if keep < 1:
        raise ValueError("keep must be >= 1 — rotating to zero deletes every backup")
    target_dir = Path(output_dir)
    if not target_dir.exists():
        return []
    snapshots = sorted(target_dir.glob(f"audit-*{BACKUP_SUFFIX}"), reverse=True)
    removed: list[Path] = []
    for stale in snapshots[keep:]:
        try:
            stale.unlink()
            removed.append(stale)
        except OSError:
            logger.exception("Could not remove stale backup %s; continuing", stale)
    return removed


def run_once(db_path: str | Path, output_dir: str | Path, keep: int) -> Path:
    target = create_backup(db_path, output_dir)
    removed = rotate_backups(output_dir, keep)
    print(f"backup written: {target}" + (f" (rotated out {len(removed)} old)" if removed else ""))
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Snapshot the Sentinel audit database.")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="SQLite DB path to back up.")
    parser.add_argument("--output-dir", required=True, help="Directory snapshots are written to.")
    parser.add_argument("--keep", type=int, default=14, help="Snapshots to retain (default 14).")
    parser.add_argument(
        "--every-hours",
        type=float,
        default=None,
        help="Run forever, snapshotting every N hours (sidecar mode). Omit for one shot.",
    )
    args = parser.parse_args(argv)

    if args.every_hours is None:
        run_once(args.db, args.output_dir, args.keep)
        return 0
    if args.every_hours <= 0:
        parser.error("--every-hours must be positive")
    while True:
        try:
            run_once(args.db, args.output_dir, args.keep)
        except Exception:
            # A failed snapshot must not kill the sidecar; the next interval retries.
            logger.exception("Scheduled backup failed; retrying next interval")
        time.sleep(args.every_hours * 3600)


if __name__ == "__main__":
    raise SystemExit(main())
