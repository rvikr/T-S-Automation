"""Regression tests for the evaluation harness.

The bug these pin down: `_live_case` used to point production cases at the
committed fixture on disk. Escalated production cases are quarantined, and
quarantine *moves* the file — so running the live eval deleted the golden set,
and every subsequent run scored empty assets, escalated 100% of cases, and
reported ~44% accuracy that looked like model regression rather than data loss.

Measuring a system must never mutate the thing being measured.
"""

import tempfile
import unittest
from pathlib import Path

from sentinel.eval.run_eval import _live_case
from sentinel.models import Case
from sentinel.tools.media_utils import load_synthetic_cases, quarantine


class LiveCaseIsolationTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.base = Path(self.tmpdir.name)
        self.fixture = self.base / "fixtures" / "txt-fixture.synthetic"
        self.fixture.parent.mkdir(parents=True)
        self.fixture.write_text("golden set content", encoding="utf-8")
        self.golden = Case(
            id="fixture-1",
            asset_type="text",
            asset_path=str(self.fixture),
            metadata={"expected_category": "Spam", "expected_decision": "reject"},
        )

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_live_case_copies_rather_than_referencing_the_fixture(self):
        work_dir = self.base / "work"
        work_dir.mkdir()

        live = _live_case(self.golden, work_dir)

        self.assertNotEqual(Path(live.asset_path), self.fixture)
        self.assertEqual(Path(live.asset_path).read_text(encoding="utf-8"), "golden set content")
        self.assertEqual(live.metadata, {"analysis_mode": "production"})

    def test_quarantining_a_live_case_leaves_the_fixture_on_disk(self):
        work_dir = self.base / "work"
        work_dir.mkdir()
        live = _live_case(self.golden, work_dir)
        live.metadata["moderation_run_id"] = "run-1"

        # This is the operation that used to eat the golden set.
        self.assertTrue(quarantine(live, quarantine_dir=self.base / "quarantine"))

        self.assertTrue(self.fixture.exists(), "the committed fixture must survive a live eval")
        self.assertEqual(self.fixture.read_text(encoding="utf-8"), "golden set content")


class GoldenSetIntegrityTests(unittest.TestCase):
    def test_every_manifest_asset_exists_and_is_non_empty(self):
        # A deleted or emptied fixture makes live runs read nothing and
        # escalate everything — a silent, plausible-looking scoring collapse.
        missing: list[str] = []
        empty: list[str] = []
        for case in load_synthetic_cases():
            path = Path(case.asset_path)
            if not path.exists():
                missing.append(case.id)
            elif not path.read_bytes().strip():
                empty.append(case.id)

        self.assertEqual(missing, [], f"golden-set assets missing from disk: {missing}")
        self.assertEqual(empty, [], f"golden-set assets are empty: {empty}")


if __name__ == "__main__":
    unittest.main()
