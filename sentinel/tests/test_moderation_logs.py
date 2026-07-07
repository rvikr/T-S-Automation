import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sentinel.agents.orchestrator import run_case
from sentinel.models import Case
from sentinel.tools import audit_log
from sentinel.tools.audit_log import init_db


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


class ModerationLogTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.base_path = Path(self.tmpdir.name)
        self.db_path = self.base_path / "audit.sqlite"
        init_db(self.db_path)

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_logs_show_whether_escalation_triggered_and_include_details(self):
        safe = make_case(
            self.base_path,
            "safe-text",
            "text",
            "No Violation",
            "friendly text",
            "allow",
        )
        senior = make_case(
            self.base_path,
            "senior-text",
            "text",
            "Harassment & Discrimination",
            "borderline insult between friends",
            "ambiguous",
        )
        human = make_case(
            self.base_path,
            "human-tier1",
            "image",
            "Child Exploitation",
            "synthetic tier-1 stand-in label only",
            "allow",
        )

        for case in [safe, senior, human]:
            run_case(case, db_path=self.db_path)

        self.assertTrue(hasattr(audit_log, "list_moderation_logs"))
        logs = audit_log.list_moderation_logs(self.db_path)
        logs_by_case = {log.case_id: log for log in logs}

        self.assertFalse(logs_by_case["safe-text"].escalation_triggered)
        self.assertIsNone(logs_by_case["safe-text"].escalation_type)
        self.assertEqual(logs_by_case["safe-text"].escalation_details, {})

        self.assertTrue(logs_by_case["senior-text"].escalation_triggered)
        self.assertEqual(logs_by_case["senior-text"].escalation_type, "senior_review")
        self.assertEqual(logs_by_case["senior-text"].escalation_details["reviewer"], "senior")
        self.assertEqual(logs_by_case["senior-text"].escalation_details["status"], "resolved")

        self.assertTrue(logs_by_case["human-tier1"].escalation_triggered)
        self.assertEqual(logs_by_case["human-tier1"].escalation_type, "human_ticket")
        self.assertEqual(logs_by_case["human-tier1"].escalation_details["ticket"]["status"], "open")
        self.assertEqual(logs_by_case["human-tier1"].escalation_details["ticket"]["severity"], 1)


if __name__ == "__main__":
    unittest.main()
