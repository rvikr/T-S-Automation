import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sentinel import ui_uploads
from sentinel.models import ModerationLog
from sentinel.ui_uploads import FLOW_STAGES, build_uploaded_case, infer_upload_asset_type


class UploadCaseTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.upload_dir = Path(self.tmpdir.name)

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_infers_all_demo_upload_modalities(self):
        examples = [
            ("sample.png", "image/png", "image"),
            ("clip.mp4", "video/mp4", "video"),
            ("voice.wav", "audio/wav", "audio"),
            ("caption.txt", "text/plain", "text"),
        ]

        for filename, mime_type, expected in examples:
            with self.subTest(filename=filename):
                self.assertEqual(infer_upload_asset_type(filename, mime_type), expected)

    def test_build_uploaded_case_persists_file_and_sets_demo_policy_signal(self):
        case = build_uploaded_case(
            name="demo.png",
            content_type="image/png",
            payload=b"synthetic image bytes",
            category="Violent Content & Gore",
            decision="reject",
            label="synthetic demo label for gore category, no depiction",
            upload_dir=self.upload_dir,
        )

        self.assertEqual(case.asset_type, "image")
        self.assertTrue(Path(case.asset_path).exists())
        self.assertEqual(Path(case.asset_path).read_bytes(), b"synthetic image bytes")
        self.assertEqual(case.metadata["expected_category"], "Violent Content & Gore")
        self.assertEqual(case.metadata["expected_decision"], "reject")
        self.assertIn("synthetic demo label", case.metadata["synthetic_label"])

    def test_flow_stages_show_full_autonomous_moderation_path(self):
        self.assertEqual(
            FLOW_STAGES,
            [
                "Ingest upload",
                "Detect modality",
                "Retrieve policy",
                "Retrieve precedents",
                "Specialist review",
                "Guardrail check",
                "Senior or human escalation",
                "Audit log",
            ],
        )

    def test_describe_trace_event_renders_agent_and_rail_steps(self):
        examples = [
            ("agent.agent_run:Text Specialist:gpt-4o-mini", "Text Specialist agent started (gpt-4o-mini)"),
            ("agent.tool_call:Text Specialist:retrieve_policy_tool", "Text Specialist called retrieve_policy_tool"),
            ("agent.handoff:Text Specialist->Senior Reviewer", "Text Specialist → Senior Reviewer"),
            ("agent.guardrail.tier1.tripwire:SAF-TVE-001", "Tier-1 guardrail halted the agent mid-run (SAF-TVE-001)"),
            ("ticket.external:jira:MOD-42", "Jira issue created: MOD-42"),
            ("quarantine.completed", "Content quarantined"),
        ]
        for event, expected_fragment in examples:
            with self.subTest(event=event):
                icon, text = ui_uploads.describe_trace_event(event)
                self.assertTrue(icon)
                self.assertIn(expected_fragment, text)

    def test_eval_run_listing_returns_latest_first(self):
        eval_dir = self.upload_dir / "eval_runs"
        for name, payload in [
            ("20260101T000000Z-offline", '{"metrics": {"accuracy": 1.0}}'),
            ("20260201T000000Z-live", '{"metrics": {"accuracy": 0.8}}'),
        ]:
            run_dir = eval_dir / name
            run_dir.mkdir(parents=True)
            (run_dir / "results.json").write_text(payload, encoding="utf-8")
        (eval_dir / "streamlit.out.log").write_text("noise", encoding="utf-8")

        runs = ui_uploads.list_eval_runs(eval_dir)

        self.assertEqual([run.name for run in runs], ["20260201T000000Z-live", "20260101T000000Z-offline"])
        self.assertEqual(ui_uploads.load_eval_run(runs[0])["metrics"]["accuracy"], 0.8)

    def test_log_rows_summarize_escalation_status_and_details(self):
        self.assertTrue(hasattr(ui_uploads, "LOG_VIEW_LABEL"))
        self.assertEqual(ui_uploads.LOG_VIEW_LABEL, "Logs")
        self.assertTrue(hasattr(ui_uploads, "format_moderation_log_rows"))
        rows = ui_uploads.format_moderation_log_rows(
            [
                ModerationLog(
                    id=1,
                    case_id="case-1",
                    decision="ambiguous",
                    clause="SAF-CE-001 (Safety / Child Exploitation)",
                    reviewer="human",
                    timestamp="2026-07-06T00:00:00+00:00",
                    rationale="Routed to human review.",
                    escalation_triggered=True,
                    escalation_type="human_ticket",
                    escalation_details={"ticket": {"id": "TKT-123", "status": "open"}},
                ),
                ModerationLog(
                    id=2,
                    case_id="case-2",
                    decision="allow",
                    clause="SAFE-ALLOW-000 (General / No Violation)",
                    reviewer="text-specialist",
                    timestamp="2026-07-06T00:01:00+00:00",
                    rationale="No policy violation.",
                    escalation_triggered=False,
                    escalation_type=None,
                    escalation_details={},
                ),
            ]
        )

        self.assertEqual(rows[0]["Escalated"], "Yes")
        self.assertEqual(rows[0]["Escalation Type"], "human_ticket")
        self.assertIn("TKT-123", rows[0]["Escalation Details"])
        self.assertEqual(rows[1]["Escalated"], "No")
        self.assertEqual(rows[1]["Escalation Details"], "")


if __name__ == "__main__":
    unittest.main()
