"""Tests for Sub-Task 3: Jira webhook listener.

All tests are offline/hermetic:
- Real SQLite in a TemporaryDirectory.
- No Jira or OpenAI calls.
- FastAPI TestClient for endpoint tests.
- SENTINEL_JIRA_WEBHOOK_SECRET env var is cleared between tests unless
  a specific test overrides it.
"""
from __future__ import annotations

import hashlib
import hmac
import importlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sentinel.tools.audit_log import db_connection, get_ticket, init_db, utc_now
from sentinel.tools.ticketing import attach_external_reference, create_human_ticket
from sentinel.models import Case


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

NON_TIER1_CATEGORY = "Spam"


def _jira_payload(external_key: str, to_status: str = "Done", resolution_name: str | None = "Done") -> dict:
    """Build a minimal Jira issue-transitioned webhook payload."""
    return {
        "issue": {
            "key": external_key,
            "fields": {
                "resolution": {"name": resolution_name} if resolution_name else None,
            },
        },
        "changelog": {
            "items": [
                {"field": "status", "fromString": "In Progress", "toString": to_status}
            ]
        },
    }


def _insert_ticket(
    db_path: Path,
    ticket_id: str,
    case_id: str,
    status: str = "open",
    external_key: str | None = None,
) -> None:
    """Directly insert a minimal ticket row for test setup."""
    init_db(db_path)
    with db_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO tickets (id, case_id, severity, category, status, created_at,
                                 external_key, external_url)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (ticket_id, case_id, 2, NON_TIER1_CATEGORY, status, utc_now(), external_key, None),
        )


def _count_precedents(db_path: Path) -> int:
    with db_connection(db_path) as conn:
        row = conn.execute("SELECT COUNT(*) FROM precedents").fetchone()
    return int(row[0])


def _sign(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


# ---------------------------------------------------------------------------
# TestJiraWebhookListener
# ---------------------------------------------------------------------------


class TestJiraWebhookListener(unittest.TestCase):

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
            ),
            raise_server_exceptions=False,
        )

    def tearDown(self):
        self.tmpdir.cleanup()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _post(self, payload: dict, headers: dict | None = None) -> object:
        h = {"Content-Type": "application/json"}
        if headers:
            h.update(headers)
        return self.client.post("/webhooks/jira", content=json.dumps(payload).encode(), headers=h)

    # ------------------------------------------------------------------
    # Tests
    # ------------------------------------------------------------------

    def test_terminal_done_transition_accepted(self):
        """A well-formed done-transition for a known ticket → accepted + status updated + precedent written."""
        _insert_ticket(self.db_path, "TKT-W01", "case-w01", external_key="SENT-123")

        resp = self._post(_jira_payload("SENT-123", to_status="Done"))
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["status"], "accepted")
        self.assertEqual(body["ticket_id"], "TKT-W01")

        # Ticket DB row must be resolved
        ticket = get_ticket("TKT-W01", self.db_path)
        self.assertIsNotNone(ticket)
        self.assertEqual(ticket.status, "resolved")

        # A precedent row must have been written
        self.assertEqual(_count_precedents(self.db_path), 1)

    def test_non_terminal_transition_skipped(self):
        """A changelog transition to 'In Progress' is not terminal → skipped."""
        _insert_ticket(self.db_path, "TKT-W02", "case-w02", external_key="SENT-200")

        resp = self._post(_jira_payload("SENT-200", to_status="In Progress"))
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["status"], "skipped")

    def test_no_changelog_skipped(self):
        """A payload with no changelog field at all → skipped (not a terminal transition)."""
        payload = {
            "issue": {
                "key": "SENT-300",
                "fields": {"resolution": {"name": "Done"}},
            }
            # no "changelog" key
        }
        _insert_ticket(self.db_path, "TKT-W03", "case-w03", external_key="SENT-300")

        resp = self._post(payload)
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["status"], "skipped")

    def test_unknown_external_key_skipped(self):
        """A done-transition for an external_key not in the DB → skipped with 'ticket not found'."""
        resp = self._post(_jira_payload("SENT-NOTEXIST", to_status="Done"))
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["status"], "skipped")
        self.assertEqual(body["reason"], "ticket not found")

    def test_invalid_signature_rejected(self):
        """When SENTINEL_JIRA_WEBHOOK_SECRET is set, a wrong signature → 401."""
        import importlib as _il
        import unittest.mock as mock

        payload = _jira_payload("SENT-400", to_status="Done")
        body = json.dumps(payload).encode()

        with mock.patch.dict("os.environ", {"SENTINEL_JIRA_WEBHOOK_SECRET": "mysecret"}):
            # Patch the module-level constant that was already imported into api.py
            with mock.patch("sentinel.api.JIRA_WEBHOOK_SECRET", "mysecret"):
                resp = self.client.post(
                    "/webhooks/jira",
                    content=body,
                    headers={
                        "Content-Type": "application/json",
                        "X-Hub-Signature": "sha256=badhash",
                    },
                )
        self.assertEqual(resp.status_code, 401)

    def test_valid_signature_accepted(self):
        """Correct HMAC-SHA256 X-Hub-Signature → request accepted normally."""
        import unittest.mock as mock

        _insert_ticket(self.db_path, "TKT-W05", "case-w05", external_key="SENT-500")

        payload = _jira_payload("SENT-500", to_status="Done")
        body = json.dumps(payload).encode()
        sig = _sign(body, "mysecret")

        with mock.patch("sentinel.api.JIRA_WEBHOOK_SECRET", "mysecret"):
            resp = self.client.post(
                "/webhooks/jira",
                content=body,
                headers={
                    "Content-Type": "application/json",
                    "X-Hub-Signature": sig,
                },
            )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "accepted")

    def test_wont_do_resolution_maps_to_allow(self):
        """resolution.name='Won't Do' → decision='allow' and precedent has verdict='allow'."""
        _insert_ticket(self.db_path, "TKT-W06", "case-w06", external_key="SENT-600")

        resp = self._post(_jira_payload("SENT-600", to_status="Done", resolution_name="Won't Do"))
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["status"], "accepted")
        self.assertEqual(body["decision"], "allow")

        # Confirm precedent written with verdict="allow"
        with db_connection(self.db_path) as conn:
            row = conn.execute("SELECT verdict FROM precedents ORDER BY id DESC LIMIT 1").fetchone()
        self.assertIsNotNone(row)
        # verdict column stores a JSON string or the decision string
        self.assertIn("allow", row[0])

    def test_already_resolved_ticket_skipped(self):
        """A done-transition for a ticket already in 'resolved' status → skipped (idempotent)."""
        _insert_ticket(self.db_path, "TKT-W07", "case-w07", status="resolved", external_key="SENT-700")

        resp = self._post(_jira_payload("SENT-700", to_status="Done"))
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["status"], "skipped")


if __name__ == "__main__":
    unittest.main()
