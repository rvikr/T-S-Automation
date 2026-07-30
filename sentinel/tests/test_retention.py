"""Tests for the data-retention purge job."""

import os
import tempfile
import time
import unittest
from pathlib import Path

from sentinel.tools.audit_log import db_connection, init_db
from sentinel.tools.retention import purge_cache_rows, purge_old_files

_DAY = 86_400


def _make_file(path: Path, age_days: float) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x", encoding="utf-8")
    stamp = time.time() - age_days * _DAY
    os.utime(path, (stamp, stamp))
    return path


class PurgeOldFilesTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmpdir.name)

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_purges_old_keeps_recent(self):
        old = _make_file(self.root / "key_a" / "sub1" / "old.txt", age_days=45)
        recent = _make_file(self.root / "key_a" / "sub2" / "recent.txt", age_days=2)

        purged = purge_old_files(self.root, older_than_days=30)

        self.assertEqual(purged, [old])
        self.assertFalse(old.exists())
        self.assertTrue(recent.exists())
        # The emptied submission directory is cleaned up; the populated one stays.
        self.assertFalse((self.root / "key_a" / "sub1").exists())
        self.assertTrue((self.root / "key_a" / "sub2").exists())

    def test_dry_run_deletes_nothing(self):
        old = _make_file(self.root / "old.txt", age_days=45)

        purged = purge_old_files(self.root, older_than_days=30, dry_run=True)

        self.assertEqual(purged, [old])
        self.assertTrue(old.exists())

    def test_missing_root_is_a_noop(self):
        self.assertEqual(purge_old_files(self.root / "nope", older_than_days=1), [])

    def test_negative_days_rejected(self):
        with self.assertRaises(ValueError):
            purge_old_files(self.root, older_than_days=-1)


class PurgeCacheRowsTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "audit.sqlite"
        init_db(self.db_path)
        with db_connection(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO verdict_cache
                    (content_hash, asset_type, category, clause, confidence, rationale, created_at, policy_fingerprint)
                VALUES
                    ('h-old', 'text', 'No Violation', 'C', 0.9, 'r', '2020-01-01T00:00:00+00:00', 'fp'),
                    ('h-new', 'text', 'No Violation', 'C', 0.9, 'r', '2099-01-01T00:00:00+00:00', 'fp')
                """
            )

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_purges_old_rows_only(self):
        self.assertEqual(purge_cache_rows(self.db_path, older_than_days=30, dry_run=True), 1)
        self.assertEqual(purge_cache_rows(self.db_path, older_than_days=30), 1)
        with db_connection(self.db_path) as conn:
            remaining = [row[0] for row in conn.execute("SELECT content_hash FROM verdict_cache").fetchall()]
        self.assertEqual(remaining, ["h-new"])


if __name__ == "__main__":
    unittest.main()
