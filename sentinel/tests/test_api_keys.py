import importlib
import sqlite3
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


class ApiKeyTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.base_path = Path(self.tmpdir.name)
        self.db_path = self.base_path / "audit.sqlite"
        init_db(self.db_path)
        api = importlib.import_module("sentinel.api")
        from fastapi.testclient import TestClient

        self.client = TestClient(
            api.create_app(
                db_path=self.db_path,
                upload_dir=self.base_path / "uploads",
                admin_token="admin-secret",
            )
        )

    def tearDown(self):
        self.client.close()
        self.tmpdir.cleanup()

    def create_key(self, tenant_name="Example Platform", project_name="Moderation", environment="test"):
        response = self.client.post(
            "/admin/api-keys",
            headers={"Authorization": "Bearer admin-secret"},
            json={
                "tenant_name": tenant_name,
                "project_name": project_name,
                "environment": environment,
            },
        )
        self.assertEqual(response.status_code, 201)
        return response.json()

    def test_admin_generates_key_once_and_database_stores_only_hash(self):
        unauthenticated = self.client.post(
            "/admin/api-keys",
            json={"tenant_name": "Example Platform", "project_name": "Moderation", "environment": "live"},
        )
        self.assertEqual(unauthenticated.status_code, 401)

        payload = self.create_key(environment="live")

        self.assertTrue(payload["api_key"].startswith("sent_live_"))
        self.assertTrue(payload["key_id"].startswith("key_"))
        self.assertEqual(payload["environment"], "live")

        conn = sqlite3.connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT key_hash, key_prefix, status FROM api_keys WHERE id = ?",
                (payload["key_id"],),
            ).fetchone()
        finally:
            conn.close()

        self.assertIsNotNone(row)
        self.assertNotEqual(row[0], payload["api_key"])
        self.assertEqual(row[1], "sent_live")
        self.assertEqual(row[2], "active")

        listed = self.client.get("/admin/api-keys", headers={"Authorization": "Bearer admin-secret"}).json()
        self.assertEqual(listed["keys"][0]["key_id"], payload["key_id"])
        self.assertNotIn("api_key", listed["keys"][0])
        self.assertNotIn("key_hash", listed["keys"][0])

    def test_moderation_requires_valid_key_and_scopes_logs_to_key_tenant(self):
        tenant_a = self.create_key(tenant_name="Tenant A")
        tenant_b = self.create_key(tenant_name="Tenant B")
        assessment = ProductionAssessment(
            decision="reject",
            category="Spam",
            confidence=0.91,
            rationale="The uploaded text appears to be repetitive commercial solicitation.",
            evidence_summary="Promotional solicitation signal.",
        )

        missing = self.client.post(
            "/moderation/cases",
            json={"case_id": "missing-key", "asset_type": "text", "content": "spam"},
        )
        invalid = self.client.post(
            "/moderation/cases",
            headers={"Authorization": "Bearer sent_test_invalid"},
            json={"case_id": "invalid-key", "asset_type": "text", "content": "spam"},
        )
        self.assertEqual(missing.status_code, 401)
        self.assertEqual(invalid.status_code, 401)

        with patch("sentinel.tools.production_analysis.analyze_asset", return_value=assessment):
            first = self.client.post(
                "/moderation/cases",
                headers={"Authorization": f"Bearer {tenant_a['api_key']}"},
                json={"case_id": "tenant-a-case", "asset_type": "text", "content": "spam"},
            )
            second = self.client.post(
                "/moderation/cases",
                headers={"Authorization": f"Bearer {tenant_b['api_key']}"},
                json={"case_id": "tenant-b-case", "asset_type": "text", "content": "spam"},
            )

        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 201)

        logs_a = self.client.get(
            "/moderation/logs",
            headers={"Authorization": f"Bearer {tenant_a['api_key']}"},
        ).json()["logs"]
        logs_b = self.client.get(
            "/moderation/logs",
            headers={"Authorization": f"Bearer {tenant_b['api_key']}"},
        ).json()["logs"]

        self.assertEqual([log["case_id"] for log in logs_a], ["tenant-a-case"])
        self.assertEqual([log["case_id"] for log in logs_b], ["tenant-b-case"])

    def test_revoked_key_stops_moderation_requests(self):
        key = self.create_key()
        revoke = self.client.post(
            f"/admin/api-keys/{key['key_id']}/revoke",
            headers={"Authorization": "Bearer admin-secret"},
        )
        self.assertEqual(revoke.status_code, 200)

        response = self.client.post(
            "/moderation/cases",
            headers={"Authorization": f"Bearer {key['api_key']}"},
            json={"case_id": "revoked-key", "asset_type": "text", "content": "spam"},
        )

        self.assertEqual(response.status_code, 401)


if __name__ == "__main__":
    unittest.main()
