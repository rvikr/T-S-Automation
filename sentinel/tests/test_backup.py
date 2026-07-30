"""Tests for the audit-database backup tool."""

import sqlite3
import tempfile
import unittest
from pathlib import Path

from sentinel.models import Verdict
from sentinel.tools.audit_log import init_db, write_audit
from sentinel.tools.backup import create_backup, rotate_backups


def _sample_verdict(case_id: str) -> Verdict:
    return Verdict(
        case_id=case_id,
        decision="allow",
        severity_tier=0,
        category="No Violation",
        policy_clause="SAFE-ALLOW-000 (General / No Violation)",
        confidence=0.9,
        rationale="benign",
        reviewer="specialist",
    )


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.base = Path(self.tmpdir.name)
        self.db_path = self.base / "audit.sqlite"
        init_db(self.db_path)
        write_audit(_sample_verdict("case-1"), self.db_path)
        write_audit(_sample_verdict("case-2"), self.db_path)

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_backup_is_a_readable_copy_with_the_data(self):
        target = create_backup(self.db_path, self.base / "backups")

        self.assertTrue(target.exists())
        conn = sqlite3.connect(target)
        try:
            count = conn.execute("SELECT count(*) FROM audits").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(count, 2)

    def test_backup_is_consistent_while_source_connection_open(self):
        # Simulate the running service holding a connection during the snapshot.
        live = sqlite3.connect(self.db_path)
        try:
            live.execute("BEGIN")
            live.execute(
                "INSERT INTO audits (case_id, decision, clause, reviewer, rationale, timestamp)"
                " VALUES ('mid', 'allow', 'c', 'r', 'x', 't')"
            )
            target = create_backup(self.db_path, self.base / "backups")
        finally:
            live.rollback()
            live.close()
        conn = sqlite3.connect(target)
        try:
            conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
        finally:
            conn.close()

    def test_rotation_keeps_newest_n(self):
        backups = self.base / "backups"
        paths = []
        for i in range(5):
            p = backups / f"audit-2026010{i}T000000Z.sqlite"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"x")
            paths.append(p)

        removed = rotate_backups(backups, keep=2)

        survivors = sorted(p.name for p in backups.glob("audit-*.sqlite"))
        self.assertEqual(len(removed), 3)
        self.assertEqual(survivors, ["audit-20260103T000000Z.sqlite", "audit-20260104T000000Z.sqlite"])

    def test_rotation_refuses_zero_keep(self):
        with self.assertRaises(ValueError):
            rotate_backups(self.base, keep=0)

    def test_missing_source_raises(self):
        with self.assertRaises(FileNotFoundError):
            create_backup(self.base / "nope.sqlite", self.base / "backups")


if __name__ == "__main__":
    unittest.main()
