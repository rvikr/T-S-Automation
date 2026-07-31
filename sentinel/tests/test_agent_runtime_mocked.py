"""Mock-based tests for the live agent runtime path.

The hermetic suite scrubs OPENAI_API_KEY, so the code that runs real agents —
``run_specialist_case`` / ``run_senior_case`` and their tripwire handling —
previously had no automated coverage at all. These tests stub the SDK's
``Runner.run_sync`` (never the surrounding Sentinel code) so the wiring that
costs money in production is exercised offline: output parsing, usage
accounting, guardrail-exception mapping, and the fail-closed path for
unrecognised categories.
"""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agents.exceptions import InputGuardrailTripwireTriggered, OutputGuardrailTripwireTriggered

from sentinel.agents import runtime
from sentinel.agents.runtime import AssessmentOutput, run_specialist_case
from sentinel.models import Case


def _text_case(tmpdir: str, content: str = "hello world") -> Case:
    asset = Path(tmpdir) / "upload.txt"
    asset.write_text(content, encoding="utf-8")
    return Case(
        id="case-live-mock",
        asset_type="text",
        asset_path=str(asset),
        metadata={"analysis_mode": "production"},
    )


def _fake_run_result(output: AssessmentOutput) -> SimpleNamespace:
    agent = SimpleNamespace(name="Text Specialist")
    return SimpleNamespace(
        final_output=output,
        new_items=[
            SimpleNamespace(
                type="tool_call_item",
                agent=agent,
                raw_item=SimpleNamespace(name="retrieve_policy_tool"),
            ),
            SimpleNamespace(type="message_output_item", agent=agent),
        ],
        last_agent=agent,
        context_wrapper=SimpleNamespace(
            usage=SimpleNamespace(requests=2, input_tokens=900, output_tokens=120, total_tokens=1020)
        ),
    )


def _tripwire(exc_class, output_info):
    guardrail_result = SimpleNamespace(
        guardrail=SimpleNamespace(),
        output=SimpleNamespace(output_info=output_info),
    )
    return exc_class(guardrail_result)


class RunSpecialistCaseTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.case = _text_case(self.tmpdir.name)
        # A sentinel client object: the SDK is never reached, but the adopt
        # hook must not receive a real (network-capable) client in tests.
        self.client = object()
        self._adopt = patch.object(runtime, "_adopt_client_for_sdk", lambda client: None)
        self._adopt.start()

    def tearDown(self):
        self._adopt.stop()
        self.tmpdir.cleanup()

    def test_normal_run_maps_output_events_and_usage(self):
        output = AssessmentOutput(
            decision="reject",
            category="Spam",
            confidence=0.9,
            rationale="Repetitive promotional content.",
            evidence_summary="Promotional signal.",
            cited_clauses=["INT-SPAM-001"],
        )
        with patch.object(runtime.Runner, "run_sync", return_value=_fake_run_result(output)):
            assessment = run_specialist_case(self.case, client=self.client)

        self.assertEqual(assessment.decision, "reject")
        self.assertEqual(assessment.category, "Spam")
        self.assertEqual(assessment.cited_clauses, ["INT-SPAM-001"])
        self.assertEqual(assessment.usage_total_tokens, 1020)
        self.assertEqual(assessment.usage_requests, 2)
        self.assertIn("tool_call:Text Specialist:retrieve_policy_tool", assessment.agent_events)
        self.assertIn("verdict_drafted:Text Specialist", assessment.agent_events)
        self.assertEqual(assessment.reviewer_chain, ["Text Specialist"])

    def test_tier1_output_tripwire_maps_to_human_route(self):
        exc = _tripwire(OutputGuardrailTripwireTriggered, {"category": "Child Exploitation"})
        with patch.object(runtime.Runner, "run_sync", side_effect=exc):
            assessment = run_specialist_case(self.case, client=self.client)

        self.assertEqual(assessment.decision, "ambiguous")
        self.assertEqual(assessment.category, "Child Exploitation")
        self.assertEqual(assessment.reviewer_chain, ["guardrail"])
        self.assertTrue(any(event.startswith("guardrail.tier1.tripwire:") for event in assessment.agent_events))
        # The halt must never leak content detail into the rationale.
        self.assertNotIn("hello world", assessment.rationale)

    def test_tier1_tripwire_with_unknown_category_still_lands_on_tier1(self):
        # A tripwire whose payload carries a garbage category must not produce
        # a non-Tier-1 assessment — that would disarm the downstream rail.
        exc = _tripwire(OutputGuardrailTripwireTriggered, {"category": "???"})
        with patch.object(runtime.Runner, "run_sync", side_effect=exc):
            assessment = run_specialist_case(self.case, client=self.client)

        from sentinel.tools.policy_retrieval import TIER1_CATEGORIES

        self.assertIn(assessment.category, TIER1_CATEGORIES)
        self.assertEqual(assessment.decision, "ambiguous")

    def test_injection_input_tripwire_maps_to_human_route(self):
        exc = _tripwire(InputGuardrailTripwireTriggered, {"matched": "ignore previous instructions"})
        with patch.object(runtime.Runner, "run_sync", side_effect=exc):
            assessment = run_specialist_case(self.case, client=self.client)

        self.assertEqual(assessment.decision, "ambiguous")
        self.assertIn("guardrail.input.injection", assessment.agent_events)
        self.assertEqual(assessment.reviewer_chain, ["guardrail"])

    def test_unrecognised_category_fails_closed_to_review(self):
        output = AssessmentOutput(
            decision="reject",
            category="Totally Invented Category",
            confidence=0.9,
            rationale="Model made up a label.",
            evidence_summary="n/a",
            cited_clauses=[],
        )
        with patch.object(runtime.Runner, "run_sync", return_value=_fake_run_result(output)):
            assessment = run_specialist_case(self.case, client=self.client)

        # Never silently relabel an enforcement verdict as benign: route to review.
        self.assertEqual(assessment.decision, "ambiguous")
        self.assertEqual(assessment.category, "No Violation")

    def test_sdk_default_client_must_be_async(self):
        """The SDK awaits ``client.responses.create``; a sync client breaks every run.

        Handing ``set_default_openai_client`` the synchronous client raised
        "object Response can't be used in 'await' expression" on every agent
        run. ``analyze_asset`` catches that and fails closed, so the symptom
        was not a crash but 100% of cases silently escalating to human review.
        """
        from openai import AsyncOpenAI, OpenAI

        self._adopt.stop()  # exercise the real adopter for this test
        try:
            captured: list[object] = []
            with patch.object(runtime, "set_default_openai_client", captured.append):
                runtime._adopt_client_for_sdk(OpenAI(api_key="sk-test-not-used"))
        finally:
            self._adopt.start()

        self.assertEqual(len(captured), 1)
        self.assertIsInstance(captured[0], AsyncOpenAI)

    def test_empty_text_asset_short_circuits_without_calling_the_sdk(self):
        empty_case = _text_case(self.tmpdir.name + "", content="   ")
        with patch.object(runtime.Runner, "run_sync") as run_sync:
            assessment = run_specialist_case(empty_case, client=self.client)

        run_sync.assert_not_called()
        self.assertEqual(assessment.decision, "ambiguous")
        self.assertIn("evidence:empty", assessment.agent_events)


if __name__ == "__main__":
    unittest.main()
