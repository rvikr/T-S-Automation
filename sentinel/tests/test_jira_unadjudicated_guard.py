"""Unadjudicated production escalations must not reach Jira.

Without a working model every production upload fails closed to an escalation,
and every escalation creates a ticket that mirrors to Jira. A key-less (or
outage-hit) deployment with JIRA_* configured therefore opens one real Jira
issue per request -- benign content included -- for a verdict no model produced.

The local ticket is still written in every case: this suppresses the external
mirror only, so an escalation is never lost.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sentinel.agents import orchestrator
from sentinel.agents.orchestrator import _escalate_ticket, _was_adjudicated
from sentinel.models import Case, Ticket, Verdict


def _case(analysis_mode: str | None) -> Case:
    metadata = {"analysis_mode": analysis_mode} if analysis_mode else {}
    return Case(id="jira-guard-001", asset_type="text", asset_path="", metadata=metadata)


def _verdict() -> Verdict:
    return Verdict(
        case_id="jira-guard-001",
        decision="ambiguous",
        severity_tier=0,
        category="No Violation",
        policy_clause="SAFE-ALLOW-000 (General / No Violation)",
        confidence=0.0,
        rationale="Failing closed.",
        reviewer="senior",
    )


def _ticket() -> Ticket:
    return Ticket(
        id="TKT-TEST",
        case_id="jira-guard-001",
        severity=0,
        category="No Violation",
        status="open",
        created_at="2026-01-01T00:00:00+00:00",
    )


UNADJUDICATED_TRACES = [
    ["ingest:x", "agent.production_analysis.unavailable", "specialist.verdict:ambiguous:c"],
    ["ingest:x", "agent.agent_runtime.error:RuntimeError", "specialist.verdict:ambiguous:c"],
    ["ingest:x", "agent.agent_runtime.error:TimeoutError"],
]


class WasAdjudicatedTests(unittest.TestCase):
    def test_production_case_without_a_model_is_not_adjudicated(self):
        for trace in UNADJUDICATED_TRACES:
            with self.subTest(trace=trace[1]):
                self.assertFalse(_was_adjudicated(_case("production"), trace))

    def test_production_case_with_a_real_verdict_is_adjudicated(self):
        trace = ["ingest:x", "production_analysis:enabled", "specialist.verdict:reject:CIV-VCG-001"]
        self.assertTrue(_was_adjudicated(_case("production"), trace))

    def test_synthetic_runs_count_as_adjudicated(self):
        """Labels drive genuine verdicts offline; the Tier-1 Jira demo relies on it."""
        self.assertTrue(_was_adjudicated(_case(None), ["agent.production_analysis.unavailable"]))


class EscalateTicketMirrorTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.db_path = Path(self.tmpdir.name) / "audit.sqlite"

    def test_unadjudicated_production_case_skips_jira(self):
        trace = ["ingest:x", "agent.production_analysis.unavailable"]

        with patch.object(orchestrator, "create_jira_issue") as create:
            returned = _escalate_ticket(_case("production"), _verdict(), _ticket(), self.db_path, trace)

        create.assert_not_called()
        self.assertIn("ticket.external:skipped-unadjudicated", trace)
        self.assertEqual(returned.id, "TKT-TEST", "local ticket must survive the skip")

    def test_adjudicated_production_case_still_mirrors(self):
        trace = ["ingest:x", "specialist.verdict:reject:CIV-VCG-001"]

        with patch.object(orchestrator, "create_jira_issue", return_value=None) as create:
            _escalate_ticket(_case("production"), _verdict(), _ticket(), self.db_path, trace)

        create.assert_called_once()
        self.assertIn("ticket.external:local-only", trace)

    def test_synthetic_case_still_mirrors(self):
        trace = ["ingest:x", "guardrail.tier1.triggered"]

        with patch.object(orchestrator, "create_jira_issue", return_value=None) as create:
            _escalate_ticket(_case(None), _verdict(), _ticket(), self.db_path, trace)

        create.assert_called_once()

    def test_override_env_restores_mirroring(self):
        trace = ["ingest:x", "agent.production_analysis.unavailable"]

        with patch.object(orchestrator, "MIRROR_UNADJUDICATED_TO_JIRA", True), \
             patch.object(orchestrator, "create_jira_issue", return_value=None) as create:
            _escalate_ticket(_case("production"), _verdict(), _ticket(), self.db_path, trace)

        create.assert_called_once()


if __name__ == "__main__":
    unittest.main()
