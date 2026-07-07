import importlib
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sentinel.models import ProductionAssessment
from sentinel.tools.audit_log import init_db


class ModerationApiTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.base_path = Path(self.tmpdir.name)
        self.db_path = self.base_path / "audit.sqlite"
        init_db(self.db_path)

    def tearDown(self):
        self.tmpdir.cleanup()

    def _client_and_key(self):
        api = importlib.import_module("sentinel.api")
        from fastapi.testclient import TestClient

        client = TestClient(
            api.create_app(
                db_path=self.db_path,
                upload_dir=self.base_path / "uploads",
                admin_token="admin-secret",
            )
        )
        response = client.post(
            "/admin/api-keys",
            headers={"Authorization": "Bearer admin-secret"},
            json={
                "tenant_name": "Example Platform",
                "project_name": "Production Moderation",
                "environment": "test",
            },
        )
        self.assertEqual(response.status_code, 201)
        return client, response.json()["api_key"]

    def test_moderate_endpoint_returns_autonomous_enforcement_payload_for_ticketing_tools(self):
        self.assertIsNotNone(importlib.util.find_spec("sentinel.api"))
        assessment = ProductionAssessment(
            decision="reject",
            category="Spam",
            confidence=0.91,
            rationale="The uploaded text appears to be repetitive commercial solicitation.",
            evidence_summary="Promotional solicitation signal.",
        )
        client, api_key = self._client_and_key()

        with patch("sentinel.tools.production_analysis.analyze_asset", return_value=assessment):
            response = client.post(
                "/moderation/cases",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "case_id": "zendesk-123",
                    "asset_type": "text",
                    "content": "buy coins at spam.example",
                    "source_system": "zendesk",
                    "external_reference": "ZD-123",
                },
            )

        self.assertEqual(response.status_code, 201)
        payload = response.json()
        self.assertEqual(payload["case_id"], "zendesk-123")
        self.assertEqual(payload["enforcement"]["mode"], "autonomous")
        self.assertEqual(payload["enforcement"]["action"], "reject")
        self.assertFalse(payload["enforcement"]["escalation_triggered"])
        self.assertEqual(payload["verdict"]["category"], "Spam")
        self.assertEqual(
            payload["integration"]["ticketing_systems"],
            ["jira", "servicenow", "zendesk", "webhook"],
        )
        self.assertEqual(payload["integration"]["ticketing_payload"]["fields"]["external_reference"], "ZD-123")

    def test_tier1_endpoint_returns_escalation_details_and_logs_endpoint_exposes_them(self):
        self.assertIsNotNone(importlib.util.find_spec("sentinel.api"))
        assessment = ProductionAssessment(
            decision="reject",
            category="Child Exploitation",
            confidence=0.99,
            rationale="Tier-1 signal detected; automated adjudication must stop.",
            evidence_summary="Tier-1 routing signal.",
        )
        client, api_key = self._client_and_key()

        with patch("sentinel.tools.production_analysis.analyze_asset", return_value=assessment):
            response = client.post(
                "/moderation/cases",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "case_id": "jira-999",
                    "asset_type": "text",
                    "content": "tier one routing signal",
                    "source_system": "jira",
                    "external_reference": "MOD-999",
                },
            )

        self.assertEqual(response.status_code, 201)
        payload = response.json()
        self.assertEqual(payload["enforcement"]["action"], "escalate")
        self.assertTrue(payload["enforcement"]["escalation_triggered"])
        self.assertEqual(payload["enforcement"]["escalation"]["type"], "human_ticket")
        self.assertEqual(payload["integration"]["ticketing_payload"]["priority"], "critical")

        logs_response = client.get("/moderation/logs", headers={"Authorization": f"Bearer {api_key}"})

        self.assertEqual(logs_response.status_code, 200)
        logs = logs_response.json()["logs"]
        self.assertEqual(logs[0]["case_id"], "jira-999")
        self.assertTrue(logs[0]["escalation_triggered"])
        self.assertEqual(logs[0]["escalation_details"]["ticket"]["status"], "open")


if __name__ == "__main__":
    unittest.main()
