"""Tests for Sub-Task 2: Human Feedback API — ticket resolve endpoint.

All tests are offline/hermetic:
- Real SQLite in a TemporaryDirectory (no in-memory :memory: path so that
  init_db's WAL pragma works correctly).
- No OpenAI or Jira calls.
- FastAPI TestClient for endpoint tests.
"""
from __future__ import annotations

import importlib
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sentinel.tools.audit_log import db_connection, get_ticket, init_db, utc_now
from sentinel.tools.ticketing import resolve_ticket_with_precedent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

NON_TIER1_CATEGORY = "Spam"  # tier 0 in the policy taxonomy


def _insert_ticket(db_path: Path, ticket_id: str, case_id: str, status: str = "open") -> None:
    """Directly insert a minimal ticket row for test setup."""
    init_db(db_path)
    with db_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO tickets (id, case_id, severity, category, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (ticket_id, case_id, 2, NON_TIER1_CATEGORY, status, utc_now()),
        )


def _count_precedents(db_path: Path) -> int:
    with db_connection(db_path) as conn:
        row = conn.execute("SELECT COUNT(*) FROM precedents").fetchone()
    return int(row[0])


# ---------------------------------------------------------------------------
# TestResolveTicketHelper — unit tests for resolve_ticket()
# ---------------------------------------------------------------------------


class TestResolveTicketHelper(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "audit.sqlite"
        init_db(self.db_path)

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_successful_resolve(self):
        """resolve_ticket_with_precedent() marks status='resolved' in DB and writes a precedent row."""
        _insert_ticket(self.db_path, "TKT-001", "case-001")

        result = resolve_ticket_with_precedent("TKT-001", "reject", "Clearly spam.", NON_TIER1_CATEGORY, self.db_path)

        self.assertEqual(result.status, "resolved")
        self.assertEqual(result.id, "TKT-001")

        # Confirm DB row was updated
        reloaded = get_ticket("TKT-001", self.db_path)
        self.assertIsNotNone(reloaded)
        self.assertEqual(reloaded.status, "resolved")

        # Confirm a precedent was written
        self.assertEqual(_count_precedents(self.db_path), 1)

    def test_unknown_ticket_raises_key_error(self):
        with self.assertRaises(KeyError) as ctx:
            resolve_ticket_with_precedent("DOES-NOT-EXIST", "allow", "Some reason.", NON_TIER1_CATEGORY, self.db_path)
        self.assertIn("DOES-NOT-EXIST", str(ctx.exception))

    def test_double_resolve_raises_value_error(self):
        _insert_ticket(self.db_path, "TKT-002", "case-002", status="resolved")
        with self.assertRaises(ValueError) as ctx:
            resolve_ticket_with_precedent("TKT-002", "allow", "Benign.", NON_TIER1_CATEGORY, self.db_path)
        self.assertIn("already resolved", str(ctx.exception))

    def test_tier1_category_raises_value_error(self):
        _insert_ticket(self.db_path, "TKT-003", "case-003")
        with self.assertRaises(ValueError) as ctx:
            resolve_ticket_with_precedent("TKT-003", "allow", "Reviewed.", "Child Exploitation", self.db_path)
        self.assertIn("Tier-1", str(ctx.exception))


# ---------------------------------------------------------------------------
# TestResolveTicketEndpoint — integration tests via FastAPI TestClient
# ---------------------------------------------------------------------------


class TestResolveTicketEndpoint(unittest.TestCase):
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
        # Mint an API key for use in tests
        resp = self.client.post(
            "/admin/api-keys",
            headers={"Authorization": "Bearer admin-secret"},
            json={
                "tenant_name": "Test Tenant",
                "project_name": "Feedback Loop Tests",
                "environment": "test",
            },
        )
        self.assertEqual(resp.status_code, 201)
        self.api_key = resp.json()["api_key"]

    def tearDown(self):
        self.tmpdir.cleanup()

    def _auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"}

    def _resolve(self, ticket_id: str, category: str = NON_TIER1_CATEGORY, status: int = 0) -> object:
        return self.client.post(
            f"/moderation/tickets/{ticket_id}/resolve",
            headers=self._auth(),
            json={"decision": "reject", "rationale": "Test rationale.", "category": category},
        )

    def test_resolve_endpoint_200(self):
        """POST with valid API key and open ticket → 200 with correct JSON shape."""
        _insert_ticket(self.db_path, "TKT-E01", "case-e01")
        resp = self._resolve("TKT-E01")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["ticket_id"], "TKT-E01")
        self.assertTrue(body["precedent_written"])
        self.assertEqual(body["decision"], "reject")

    def test_resolve_endpoint_404(self):
        """Unknown ticket_id → 404."""
        resp = self._resolve("NONEXISTENT")
        self.assertEqual(resp.status_code, 404)

    def test_resolve_endpoint_409(self):
        """Already-resolved ticket → 409."""
        _insert_ticket(self.db_path, "TKT-E02", "case-e02", status="resolved")
        resp = self._resolve("TKT-E02")
        self.assertEqual(resp.status_code, 409)

    def test_resolve_endpoint_403_tier1(self):
        """Tier-1 category → 403."""
        _insert_ticket(self.db_path, "TKT-E03", "case-e03")
        resp = self.client.post(
            "/moderation/tickets/TKT-E03/resolve",
            headers=self._auth(),
            json={"decision": "allow", "rationale": "Reviewed.", "category": "Child Exploitation"},
        )
        self.assertEqual(resp.status_code, 403)

    def test_resolve_endpoint_401(self):
        """Missing/invalid API key → 401."""
        _insert_ticket(self.db_path, "TKT-E04", "case-e04")
        resp = self.client.post(
            "/moderation/tickets/TKT-E04/resolve",
            headers={"Authorization": "Bearer invalid-key-xyz"},
            json={"decision": "reject", "rationale": "No auth.", "category": NON_TIER1_CATEGORY},
        )
        self.assertEqual(resp.status_code, 401)


if __name__ == "__main__":
    unittest.main()
