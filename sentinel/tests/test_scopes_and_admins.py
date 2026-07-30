"""Tests for API key scopes and named admin tokens."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from sentinel.api import create_app
from sentinel.tools.api_keys import rotate_api_key


class KeyScopeTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        base = Path(self.tmpdir.name)
        self.db_path = base / "audit.sqlite"
        self.client = TestClient(
            create_app(db_path=self.db_path, upload_dir=base / "uploads", admin_token="admin-secret")
        )
        self.admin = {"Authorization": "Bearer admin-secret"}

    def tearDown(self):
        self.tmpdir.cleanup()

    def _mint(self, scopes):
        body = {"tenant_name": "T", "project_name": "P", "environment": "test"}
        if scopes is not None:
            body["scopes"] = scopes
        response = self.client.post("/admin/api-keys", headers=self.admin, json=body)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def test_logs_only_key_cannot_moderate(self):
        key = self._mint(["logs"])
        headers = {"Authorization": f"Bearer {key['api_key']}"}

        self.assertEqual(self.client.get("/moderation/logs", headers=headers).status_code, 200)
        response = self.client.post(
            "/moderation/cases", headers=headers, json={"asset_type": "text", "content": "x"}
        )
        self.assertEqual(response.status_code, 403)
        self.assertIn("moderate", response.json()["detail"])

    def test_moderate_only_key_cannot_read_logs(self):
        key = self._mint(["moderate"])
        headers = {"Authorization": f"Bearer {key['api_key']}"}

        self.assertEqual(self.client.get("/moderation/logs", headers=headers).status_code, 403)

    def test_default_key_keeps_full_access(self):
        key = self._mint(None)
        self.assertEqual(key["scopes"], "moderate,logs")
        headers = {"Authorization": f"Bearer {key['api_key']}"}
        self.assertEqual(self.client.get("/moderation/logs", headers=headers).status_code, 200)

    def test_unknown_scope_rejected_at_creation(self):
        response = self.client.post(
            "/admin/api-keys",
            headers=self.admin,
            json={"tenant_name": "T", "project_name": "P", "environment": "test", "scopes": ["superuser"]},
        )
        self.assertEqual(response.status_code, 400)

    def test_rotation_preserves_scopes(self):
        key = self._mint(["logs"])
        replacement = rotate_api_key(self.db_path, key["key_id"], rotated_by="tester")
        assert replacement is not None
        self.assertEqual(replacement["scopes"], "logs")


class NamedAdminTokenTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.base = Path(self.tmpdir.name)

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_named_tokens_authenticate_and_attribute(self):
        with patch.dict("os.environ", {"SENTINEL_ADMIN_TOKENS": "alice:tok-a, bob:tok-b"}):
            client = TestClient(
                create_app(db_path=self.base / "audit.sqlite", upload_dir=self.base / "uploads")
            )
            response = client.post(
                "/admin/api-keys",
                headers={"Authorization": "Bearer tok-a"},
                json={"tenant_name": "T", "project_name": "P", "environment": "test"},
            )
            self.assertEqual(response.status_code, 201)
            # The mint is attributed to the acting admin, not a shared identity.
            self.assertEqual(response.json()["created_by"], "alice")

            self.assertEqual(
                client.get(
                    "/admin/api-keys", headers={"Authorization": "Bearer tok-b"}
                ).status_code,
                200,
            )
            self.assertEqual(
                client.get(
                    "/admin/api-keys", headers={"Authorization": "Bearer tok-c"}
                ).status_code,
                401,
            )

    def test_legacy_single_token_still_works_as_admin(self):
        client = TestClient(
            create_app(
                db_path=self.base / "audit.sqlite", upload_dir=self.base / "uploads", admin_token="solo"
            )
        )
        response = client.post(
            "/admin/api-keys",
            headers={"Authorization": "Bearer solo"},
            json={"tenant_name": "T", "project_name": "P", "environment": "test"},
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["created_by"], "admin")


if __name__ == "__main__":
    unittest.main()
