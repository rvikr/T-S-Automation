"""Tests for the API's operational surface: /health and request correlation."""

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from sentinel.api import create_app


class HealthEndpointTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        base = Path(self.tmpdir.name)
        self.client = TestClient(
            create_app(db_path=base / "audit.sqlite", upload_dir=base / "uploads")
        )

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_health_reports_database_and_configuration_booleans(self):
        response = self.client.get("/health")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["database"], "ok")
        self.assertEqual(payload["version"], "1.0.0")
        # Hermetic suite scrubs credentials, so both integrations read False —
        # and the payload must expose booleans, never credential values.
        self.assertFalse(payload["openai_configured"])
        self.assertFalse(payload["jira_configured"])
        self.assertEqual(payload["ticketing_systems"], ["jira"])

    def test_generated_request_id_header_present(self):
        response = self.client.get("/health")

        self.assertTrue(response.headers.get("X-Request-ID"))

    def test_inbound_request_id_is_echoed(self):
        response = self.client.get("/health", headers={"X-Request-ID": "caller-trace-42"})

        self.assertEqual(response.headers.get("X-Request-ID"), "caller-trace-42")


class MetricsEndpointTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        base = Path(self.tmpdir.name)
        self.db_path = base / "audit.sqlite"
        self.client = TestClient(
            create_app(db_path=self.db_path, upload_dir=base / "uploads", admin_token="admin-secret")
        )

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_metrics_requires_admin_token(self):
        self.assertEqual(self.client.get("/metrics").status_code, 401)
        self.assertEqual(
            self.client.get("/metrics", headers={"Authorization": "Bearer wrong"}).status_code, 401
        )

    def test_metrics_reports_counters(self):
        from sentinel.models import Case, Verdict
        from sentinel.tools.audit_log import write_audit
        from sentinel.tools.ticketing import create_human_ticket

        verdict = Verdict(
            case_id="m-1",
            decision="reject",
            severity_tier=3,
            category="Spam",
            policy_clause="INT-SPAM-001 (Integrity / Spam)",
            confidence=0.9,
            rationale="spam",
            reviewer="specialist",
        )
        write_audit(verdict, self.db_path)
        create_human_ticket(Case(id="m-2", asset_type="text", asset_path="", metadata={}), 2, "Spam", self.db_path)

        payload = self.client.get("/metrics", headers={"Authorization": "Bearer admin-secret"}).json()

        self.assertEqual(payload["audits"]["total"], 1)
        self.assertEqual(payload["audits"]["by_decision"]["reject"], 1)
        self.assertEqual(payload["tickets"]["open"], 1)
        self.assertIn("uptime_seconds", payload)
        self.assertIn("daily_budget", payload)
        self.assertEqual(payload["verdict_cache_entries"], 0)


if __name__ == "__main__":
    unittest.main()
