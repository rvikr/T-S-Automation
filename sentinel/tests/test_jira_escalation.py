import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sentinel.agents.orchestrator import run_case
from sentinel.models import Case
from sentinel.tools.audit_log import init_db, list_moderation_logs
from sentinel.tools.jira_client import create_jira_issue, jira_enabled
from sentinel.tools.ticketing import list_human_tickets


def _tier1_case(base_path: Path) -> Case:
    asset = base_path / "tier1-standin.synthetic"
    asset.write_text("SYNTHETIC PLACEHOLDER ONLY: tier-1 stand-in. No depiction.", encoding="utf-8")
    return Case(
        id="jira-test-001",
        asset_type="text",
        asset_path=str(asset),
        metadata={
            "synthetic_label": "tier-1 terror stand-in",
            "expected_category": "Terrorism & Violent Extremism",
            "expected_decision": "ambiguous",
        },
    )


def _jira_env():
    return patch.dict(
        "os.environ",
        {
            "JIRA_BASE_URL": "https://example.atlassian.net",
            "JIRA_EMAIL": "moderator@example.com",
            "JIRA_API_TOKEN": "token-123",
            "JIRA_PROJECT_KEY": "MOD",
        },
    )


class JiraEscalationTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.base_path = Path(self.tmpdir.name)
        self.db_path = self.base_path / "audit.sqlite"
        init_db(self.db_path)

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_jira_disabled_without_configuration(self):
        self.assertFalse(jira_enabled())
        result = run_case(_tier1_case(self.base_path), db_path=self.db_path)
        self.assertIsNotNone(result.ticket)
        self.assertIsNone(result.ticket.external_key)
        self.assertIn("ticket.external:local-only", result.trace)

    def test_tier1_escalation_creates_jira_issue_and_records_reference(self):
        response = MagicMock(status_code=201)
        response.json.return_value = {"key": "MOD-42"}

        with _jira_env(), patch("sentinel.tools.jira_client.requests.post", return_value=response) as post:
            result = run_case(_tier1_case(self.base_path), db_path=self.db_path)

        self.assertEqual(result.ticket.external_key, "MOD-42")
        self.assertEqual(result.ticket.external_url, "https://example.atlassian.net/browse/MOD-42")
        self.assertIn("ticket.external:jira:MOD-42", result.trace)

        fields = post.call_args.kwargs["json"]["fields"]
        self.assertEqual(fields["project"], {"key": "MOD"})
        self.assertEqual(fields["priority"], {"name": "Highest"})
        self.assertIn("tier-1", fields["labels"])
        self.assertIn("Tier-1", fields["summary"])
        description_text = str(fields["description"])
        self.assertIn("SAF-TVE-001", description_text)

        stored = list_human_tickets(self.db_path)[0]
        self.assertEqual(stored.external_key, "MOD-42")
        log = list_moderation_logs(self.db_path)[0]
        self.assertEqual(log.escalation_details["ticket"]["external_key"], "MOD-42")

    def test_jira_failure_falls_back_to_local_ticket(self):
        import requests as requests_lib

        with _jira_env(), patch(
            "sentinel.tools.jira_client.requests.post",
            side_effect=requests_lib.ConnectionError("jira down"),
        ):
            result = run_case(_tier1_case(self.base_path), db_path=self.db_path)

        self.assertIsNotNone(result.ticket)
        self.assertIsNone(result.ticket.external_key)
        self.assertIn("ticket.external:local-only", result.trace)


if __name__ == "__main__":
    unittest.main()
