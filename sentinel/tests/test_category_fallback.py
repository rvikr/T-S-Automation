"""An unrecognised category must never silently become a benign one.

Models do not reproduce taxonomy labels verbatim. Rewriting any unknown label to
"No Violation" (tier 0) meant a near-miss on the *string* — "CSAM" instead of
"Child Exploitation" — disarmed the Tier-1 rail: no quarantine, no mandatory
human ticket. These tests pin the fail-closed behaviour.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sentinel.agents.orchestrator import run_case
from sentinel.models import ProductionAssessment, build_verdict
from sentinel.tools.audit_log import init_db
from sentinel.tools.policy_retrieval import TIER1_CATEGORIES, normalize_category
from sentinel.ui_uploads import build_production_uploaded_case


class NormalizeCategoryTests:
    def test_canonical_labels_pass_through(self):
        for category in TIER1_CATEGORIES:
            assert normalize_category(category) == category

    def test_tier1_aliases_resolve_to_canonical_labels(self):
        for alias in ("CSAM", "csam", "Child Sexual Abuse Material", "child safety"):
            assert normalize_category(alias) == "Child Exploitation"
        for alias in ("terrorism", "Violent Extremism", "terrorist content"):
            assert normalize_category(alias) == "Terrorism & Violent Extremism"

    def test_case_and_whitespace_insensitive(self):
        assert normalize_category("  no violation  ") == "No Violation"

    def test_unknown_label_returns_none(self):
        """None, not "No Violation" — the caller must decide how to fail."""
        assert normalize_category("Rumpelstiltskin") is None
        assert normalize_category("") is None


class BuildVerdictFailClosedTests:
    def test_unknown_category_with_non_allow_decision_escalates(self):
        verdict = build_verdict(
            case_id="c-1",
            decision="reject",
            category="Something The Model Invented",
            confidence=0.9,
            rationale="Model flagged this.",
            reviewer="text-specialist",
        )
        assert verdict.decision == "ambiguous"
        assert "Unrecognised category" in verdict.rationale

    def test_tier1_alias_still_triggers_the_tier1_rail(self):
        verdict = build_verdict(
            case_id="c-2",
            decision="reject",
            category="CSAM",
            confidence=0.4,
            rationale="Detected.",
            reviewer="image-specialist",
        )
        assert verdict.category == "Child Exploitation"
        assert verdict.severity_tier == 1
        assert verdict.decision == "ambiguous"
        assert verdict.confidence >= 0.95

    def test_genuine_allow_is_not_escalated(self):
        verdict = build_verdict(
            case_id="c-3",
            decision="allow",
            category="No Violation",
            confidence=0.9,
            rationale="Benign.",
            reviewer="text-specialist",
        )
        assert verdict.decision == "allow"
        assert verdict.severity_tier != 1


class Tier1AliasRoutingTests(unittest.TestCase):
    """End-to-end: an aliased Tier-1 label must still quarantine and ticket."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.base_path = Path(self.tmpdir.name)
        self.db_path = self.base_path / "audit.sqlite"
        init_db(self.db_path)

    def tearDown(self):
        self.tmpdir.cleanup()

    def _run_with_category(self, category: str):
        case = build_production_uploaded_case(
            name="upload.txt",
            content_type="text/plain",
            payload=b"content under review",
            upload_dir=self.base_path,
        )
        assessment = ProductionAssessment(
            decision="reject",
            category=category,
            confidence=0.97,
            rationale="Model flagged Tier-1 content using a non-canonical label.",
            evidence_summary="Tier-1 signal.",
        )
        with patch("sentinel.tools.production_analysis.analyze_asset", return_value=assessment):
            return run_case(case, db_path=self.db_path)

    def test_csam_alias_quarantines_and_tickets(self):
        result = self._run_with_category("CSAM")

        self.assertEqual(result.verdict.category, "Child Exploitation")
        self.assertEqual(result.verdict.severity_tier, 1)
        self.assertEqual(result.verdict.decision, "ambiguous")
        self.assertEqual(result.verdict.reviewer, "human")
        self.assertIsNotNone(result.ticket)
        self.assertTrue(result.quarantined)

    def test_terrorism_alias_quarantines_and_tickets(self):
        result = self._run_with_category("terrorist content")

        self.assertEqual(result.verdict.category, "Terrorism & Violent Extremism")
        self.assertEqual(result.verdict.severity_tier, 1)
        self.assertIsNotNone(result.ticket)
        self.assertTrue(result.quarantined)

    def test_unknown_category_is_not_allowed_through(self):
        result = self._run_with_category("Totally Made Up Category")

        self.assertNotEqual(result.verdict.decision, "allow")


if __name__ == "__main__":
    unittest.main()
