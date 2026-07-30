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


if __name__ == "__main__":
    unittest.main()
