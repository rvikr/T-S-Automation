import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sentinel.agents.orchestrator import run_case
from sentinel.models import ProductionAssessment
from sentinel.tools.audit_log import init_db
from sentinel.tools.production_analysis import production_review
from sentinel.ui_uploads import build_production_uploaded_case


class ProductionModerationTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.base_path = Path(self.tmpdir.name)
        self.db_path = self.base_path / "audit.sqlite"
        init_db(self.db_path)

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_production_upload_has_no_demo_policy_labels(self):
        case = build_production_uploaded_case(
            name="caption.txt",
            content_type="text/plain",
            payload=b"hello from a normal player",
            upload_dir=self.base_path,
        )

        self.assertEqual(case.asset_type, "text")
        self.assertEqual(case.metadata["analysis_mode"], "production")
        self.assertNotIn("expected_category", case.metadata)
        self.assertNotIn("expected_decision", case.metadata)
        self.assertNotIn("synthetic_label", case.metadata)

    def test_production_review_rejects_detected_tier3_policy(self):
        case = build_production_uploaded_case(
            name="caption.txt",
            content_type="text/plain",
            payload=b"buy coins at spam.example",
            upload_dir=self.base_path,
        )
        assessment = ProductionAssessment(
            decision="reject",
            category="Spam",
            confidence=0.91,
            rationale="The uploaded text appears to be repetitive commercial solicitation.",
            evidence_summary="Promotional solicitation signal.",
        )

        with patch("sentinel.tools.production_analysis.analyze_asset", return_value=assessment):
            verdict = production_review(case, reviewer="text-specialist", db_path=self.db_path)

        self.assertEqual(verdict.decision, "reject")
        self.assertEqual(verdict.category, "Spam")
        self.assertIn("INT-SPAM-001", verdict.policy_clause)
        self.assertEqual(verdict.reviewer, "text-specialist")

    def test_run_case_production_upload_uses_live_analysis_path(self):
        case = build_production_uploaded_case(
            name="caption.txt",
            content_type="text/plain",
            payload=b"buy coins at spam.example",
            upload_dir=self.base_path,
        )
        assessment = ProductionAssessment(
            decision="reject",
            category="Spam",
            confidence=0.88,
            rationale="The uploaded text appears to be commercial spam.",
            evidence_summary="Spam signal.",
        )

        with patch("sentinel.tools.production_analysis.analyze_asset", return_value=assessment):
            result = run_case(case, db_path=self.db_path)

        self.assertEqual(result.verdict.decision, "reject")
        self.assertEqual(result.verdict.reviewer, "text-specialist")
        self.assertIn("production_analysis:enabled", result.trace)
        self.assertIn("INT-SPAM-001", result.warning_message)

    def test_production_tier1_routes_to_human_without_synthetic_rationale(self):
        case = build_production_uploaded_case(
            name="image.png",
            content_type="image/png",
            payload=b"not a real image for mocked test",
            upload_dir=self.base_path,
        )
        assessment = ProductionAssessment(
            decision="reject",
            category="Child Exploitation",
            confidence=0.99,
            rationale="Tier-1 signal detected; automated adjudication must stop.",
            evidence_summary="Tier-1 routing signal.",
        )

        with patch("sentinel.tools.production_analysis.analyze_asset", return_value=assessment):
            result = run_case(case, db_path=self.db_path)

        self.assertEqual(result.verdict.decision, "ambiguous")
        self.assertEqual(result.verdict.reviewer, "human")
        self.assertIsNotNone(result.ticket)
        self.assertTrue(result.quarantined)
        self.assertNotIn("synthetic", result.verdict.rationale.lower())
        self.assertIn("automated decision bypassed", result.verdict.rationale.lower())


if __name__ == "__main__":
    unittest.main()
