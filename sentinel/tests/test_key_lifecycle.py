"""Tests for API key expiry and rotation."""

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from sentinel.api import create_app
from sentinel.tools.api_keys import authenticate_api_key, create_api_key, rotate_api_key
from sentinel.tools.audit_log import db_connection, init_db


class KeyExpiryTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "audit.sqlite"
        init_db(self.db_path)

    def tearDown(self):
        self.tmpdir.cleanup()

    def _force_expiry(self, key_id: str, expires_at: str) -> None:
        with db_connection(self.db_path) as conn:
            conn.execute("UPDATE api_keys SET expires_at = ? WHERE id = ?", (expires_at, key_id))

    def test_key_without_expiry_never_expires(self):
        created = create_api_key(self.db_path, "T", "P", "test")
        self.assertIsNone(created["expires_at"])
        self.assertIsNotNone(authenticate_api_key(self.db_path, created["api_key"]))

    def test_future_expiry_authenticates_and_is_reported(self):
        created = create_api_key(self.db_path, "T", "P", "test", expires_in_days=30)
        self.assertIsNotNone(created["expires_at"])
        record = authenticate_api_key(self.db_path, created["api_key"])
        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(record.expires_at, created["expires_at"])

    def test_expired_key_fails_authentication(self):
        created = create_api_key(self.db_path, "T", "P", "test", expires_in_days=30)
        self._force_expiry(created["key_id"], "2020-01-01T00:00:00+00:00")

        self.assertIsNone(authenticate_api_key(self.db_path, created["api_key"]))

    def test_unparseable_expiry_fails_closed(self):
        created = create_api_key(self.db_path, "T", "P", "test", expires_in_days=30)
        self._force_expiry(created["key_id"], "not-a-timestamp")

        self.assertIsNone(authenticate_api_key(self.db_path, created["api_key"]))

    def test_non_positive_expiry_rejected(self):
        with self.assertRaises(ValueError):
            create_api_key(self.db_path, "T", "P", "test", expires_in_days=0)


class KeyRotationTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "audit.sqlite"
        init_db(self.db_path)
        self.original = create_api_key(self.db_path, "Tenant A", "Proj", "live", expires_in_days=90)

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_rotation_revokes_old_and_mints_equivalent_new(self):
        replacement = rotate_api_key(self.db_path, self.original["key_id"], expires_in_days=90)

        self.assertIsNotNone(replacement)
        assert replacement is not None
        # Old key is dead; new key works and belongs to the same tenant/project/env.
        self.assertIsNone(authenticate_api_key(self.db_path, self.original["api_key"]))
        record = authenticate_api_key(self.db_path, replacement["api_key"])
        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(record.tenant_name, "Tenant A")
        self.assertEqual(record.environment, "live")
        self.assertEqual(replacement["rotated_from"], self.original["key_id"])

    def test_rotating_revoked_or_unknown_key_returns_none(self):
        rotate_api_key(self.db_path, self.original["key_id"])
        # Second rotation of the (now revoked) key must not resurrect access.
        self.assertIsNone(rotate_api_key(self.db_path, self.original["key_id"]))
        self.assertIsNone(rotate_api_key(self.db_path, "key_doesnotexist"))


class KeyLifecycleApiTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        base = Path(self.tmpdir.name)
        self.client = TestClient(
            create_app(db_path=base / "audit.sqlite", upload_dir=base / "uploads", admin_token="admin-secret")
        )
        self.admin = {"Authorization": "Bearer admin-secret"}

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_create_with_expiry_and_rotate_endpoint(self):
        created = self.client.post(
            "/admin/api-keys",
            headers=self.admin,
            json={"tenant_name": "T", "project_name": "P", "environment": "test", "expires_in_days": 7},
        ).json()
        self.assertIsNotNone(created["expires_at"])

        rotated = self.client.post(
            f"/admin/api-keys/{created['key_id']}/rotate",
            headers=self.admin,
            json={"expires_in_days": 7},
        )
        self.assertEqual(rotated.status_code, 201)
        payload = rotated.json()
        self.assertEqual(payload["rotated_from"], created["key_id"])
        self.assertNotEqual(payload["api_key"], created["api_key"])

        # The rotated-away key no longer authenticates against the API surface.
        response = self.client.get(
            "/moderation/logs", headers={"Authorization": f"Bearer {created['api_key']}"}
        )
        self.assertEqual(response.status_code, 401)

    def test_rotate_requires_admin_and_active_key(self):
        response = self.client.post("/admin/api-keys/key_x/rotate", json={})
        self.assertEqual(response.status_code, 401)
        response = self.client.post("/admin/api-keys/key_x/rotate", headers=self.admin, json={})
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
