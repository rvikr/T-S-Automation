"""Tests for human ticket resolution (the review-queue backend)."""

import tempfile
import unittest
from pathlib import Path

from sentinel.models import Case
from sentinel.tools.audit_log import fetch_audit_entries, init_db, list_moderation_logs
from sentinel.tools.ticketing import create_human_ticket, list_human_tickets, resolve_ticket


class ResolveTicketTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "audit.sqlite"
        init_db(self.db_path)
        self.case = Case(
            id="case-esc-1",
            asset_type="text",
            asset_path="",
            metadata={
                "moderation_run_id": "run-123",
                "tenant_name": "Example Platform",
                "api_key_id": "key_abc",
            },
        )
        self.ticket = create_human_ticket(self.case, 2, "Harassment & Discrimination", self.db_path)

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_resolution_updates_status_and_writes_human_audit(self):
        resolved = resolve_ticket(self.ticket.id, "reject", "Clear identity-targeted abuse.", self.db_path)

        self.assertIsNotNone(resolved)
        assert resolved is not None
        self.assertEqual(resolved.status, "resolved-reject")
        stored = {ticket.id: ticket for ticket in list_human_tickets(self.db_path)}
        self.assertEqual(stored[self.ticket.id].status, "resolved-reject")

        audits = fetch_audit_entries(self.db_path)
        self.assertEqual(len(audits), 1)
        self.assertEqual(audits[0].reviewer, "human")
        self.assertEqual(audits[0].decision, "reject")
        self.assertEqual(audits[0].case_id, "case-esc-1")

    def test_resolution_joins_to_original_run_in_moderation_logs(self):
        resolve_ticket(self.ticket.id, "allow", "Banter between friends; context checks out.", self.db_path)

        logs = list_moderation_logs(self.db_path, tenant_name="Example Platform")
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0].reviewer, "human")
        self.assertTrue(logs[0].escalation_triggered)
        self.assertEqual(logs[0].escalation_details["ticket"]["status"], "resolved-allow")

    def test_double_resolution_is_rejected(self):
        first = resolve_ticket(self.ticket.id, "allow", "Fine on review.", self.db_path)
        second = resolve_ticket(self.ticket.id, "reject", "Changed my mind.", self.db_path)

        self.assertIsNotNone(first)
        # A second reviewer racing on the same ticket must not double-audit.
        self.assertIsNone(second)
        audits = fetch_audit_entries(self.db_path)
        self.assertEqual(len(audits), 1)

    def test_unknown_ticket_returns_none(self):
        self.assertIsNone(resolve_ticket("TKT-DOESNOTEXIST", "allow", "n/a", self.db_path))

    def test_invalid_decision_and_empty_rationale_raise(self):
        with self.assertRaises(ValueError):
            resolve_ticket(self.ticket.id, "escalate", "not a terminal decision", self.db_path)
        with self.assertRaises(ValueError):
            resolve_ticket(self.ticket.id, "allow", "   ", self.db_path)


if __name__ == "__main__":
    unittest.main()
