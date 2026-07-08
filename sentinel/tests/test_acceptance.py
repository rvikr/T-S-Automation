import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sentinel.agents.orchestrator import run_batch, run_case
from sentinel.models import Case
from sentinel.tools.audit_log import fetch_audit_entries, init_db
from sentinel.tools.precedent_memory import clear_precedents


def make_case(base_path, name, asset_type, category, label, expected_decision):
    asset = base_path / f"{name}.synthetic"
    asset.write_text(f"SYNTHETIC PLACEHOLDER ONLY: {label}\n", encoding="utf-8")
    return Case(
        id=name,
        asset_type=asset_type,
        asset_path=str(asset),
        metadata={
            "synthetic_label": label,
            "expected_category": category,
            "expected_decision": expected_decision,
        },
    )


class SentinelAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.base_path = Path(self.tmpdir.name)
        self.db_path = self.base_path / "audit.sqlite"
        init_db(self.db_path)

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_router_dispatches_each_asset_type_to_matching_specialist(self):
        cases = [
            make_case(self.base_path, "image-safe", "image", "No Violation", "friendly image", "allow"),
            make_case(self.base_path, "audio-safe", "audio", "No Violation", "friendly audio", "allow"),
            make_case(self.base_path, "video-safe", "video", "No Violation", "friendly video", "allow"),
            make_case(self.base_path, "text-safe", "text", "No Violation", "friendly text", "allow"),
        ]

        results = [run_case(case, db_path=self.db_path) for case in cases]

        self.assertEqual(
            [result.verdict.reviewer for result in results],
            ["image-specialist", "audio-specialist", "video-specialist", "text-specialist"],
        )

    def test_reject_returns_warning_with_specific_policy_clause(self):
        case = make_case(
            self.base_path,
            "gore-reject",
            "image",
            "Violent Content & Gore",
            "synthetic label for gore category, no depiction",
            "reject",
        )

        result = run_case(case, db_path=self.db_path)

        self.assertEqual(result.verdict.decision, "reject")
        self.assertIsNotNone(result.warning_message)
        self.assertIn("CIV-VCG-001", result.warning_message)
        self.assertIn("Violent Content & Gore", result.warning_message)

    def test_ambiguous_escalates_to_senior_reviewer_and_writes_audit(self):
        case = make_case(
            self.base_path,
            "ambiguous-harassment",
            "text",
            "Harassment & Discrimination",
            "borderline insult between friends",
            "ambiguous",
        )

        result = run_case(case, db_path=self.db_path)
        audits = fetch_audit_entries(self.db_path)

        self.assertEqual(result.verdict.reviewer, "senior")
        self.assertIn(result.verdict.decision, {"allow", "reject"})
        self.assertTrue(any(entry.case_id == case.id and entry.reviewer == "senior" for entry in audits))

    def test_tier1_always_quarantines_and_creates_human_ticket(self):
        case = make_case(
            self.base_path,
            "tier1-standin",
            "image",
            "Child Exploitation",
            "synthetic tier-1 stand-in label only",
            "allow",
        )

        result = run_case(case, db_path=self.db_path)

        self.assertEqual(result.verdict.severity_tier, 1)
        self.assertEqual(result.verdict.decision, "ambiguous")
        self.assertIsNotNone(result.ticket)
        self.assertEqual(result.ticket.status, "open")
        self.assertTrue(result.quarantined)
        self.assertFalse(any("detailed analysis" in event.lower() for event in result.trace))

    def test_batch_precedent_loop_reduces_second_pass_escalation_rate(self):
        clear_precedents(self.db_path)
        cases = [
            make_case(
                self.base_path,
                "case-a",
                "text",
                "Harassment & Discrimination",
                "borderline insult between friends",
                "ambiguous",
            ),
            make_case(
                self.base_path,
                "case-b",
                "text",
                "Harassment & Discrimination",
                "borderline insult in playful context",
                "ambiguous",
            ),
            make_case(
                self.base_path,
                "case-c",
                "text",
                "Spam",
                "repeated promotional links",
                "reject",
            ),
        ]

        first = run_batch(cases, db_path=self.db_path)
        second = run_batch(cases, db_path=self.db_path)

        self.assertGreater(first.escalation_rate, second.escalation_rate)
        self.assertEqual(second.escalation_rate, 0)

    def test_synthetic_manifest_contains_only_labeled_placeholders(self):
        manifest = PROJECT_ROOT / "sentinel" / "data" / "synthetic_cases" / "manifest.json"
        cases = json.loads(manifest.read_text(encoding="utf-8"))

        self.assertGreaterEqual(len(cases), 30)
        self.assertLessEqual(len(cases), 45)
        self.assertTrue(any(case["category"] == "Child Exploitation" for case in cases))
        self.assertTrue(any(case["category"] == "Terrorism & Violent Extremism" for case in cases))
        self.assertTrue(
            all("synthetic" in case["label"].lower() or "benign" in case["label"].lower() for case in cases)
        )
        self.assertTrue(all("real_content" not in case for case in cases))


if __name__ == "__main__":
    unittest.main()
