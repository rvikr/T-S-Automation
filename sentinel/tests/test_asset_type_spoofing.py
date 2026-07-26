"""Asset-type spoofing must not route binary media to the text specialist.

The declared ``asset_type`` arrives from the caller. If it were trusted, an
uploader could label image or video bytes as text, and the content would be
adjudicated by a text model that never sees it — with no vision-capable agent
and therefore no Tier-1 output guardrail in the path.

These tests are hermetic: the agent call is mocked to return an explicit
``allow`` so that any leniency in the result must come from the routing logic
under test, not from a missing API key.
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

from sentinel.agents.orchestrator import run_case
from sentinel.models import Case, ProductionAssessment
from sentinel.tools.audit_log import init_db
from sentinel.tools.media_utils import ASSET_TYPE_MISMATCH_KEY, detect_asset_type, sniff_asset_type
from sentinel.tools.production_analysis import read_text_asset

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"\x00" * 64
MP4_BYTES = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 64
WAV_BYTES = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 64


class SniffAssetTypeTests:
    def test_recognises_common_signatures(self):
        with tempfile.TemporaryDirectory() as tmp:
            for payload, expected in (
                (PNG_BYTES, "image"),
                (JPEG_BYTES, "image"),
                (MP4_BYTES, "video"),
                (WAV_BYTES, "audio"),
            ):
                asset = Path(tmp) / "asset.bin"
                asset.write_bytes(payload)
                assert sniff_asset_type(asset) == expected

    def test_returns_none_for_plain_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            asset = Path(tmp) / "note.txt"
            asset.write_text("just a normal caption", encoding="utf-8")
            assert sniff_asset_type(asset) is None

    def test_missing_file_returns_none_without_raising(self):
        assert sniff_asset_type(Path("does-not-exist.bin")) is None


class DetectAssetTypeReconciliationTests:
    def test_bytes_win_over_a_false_declaration(self):
        with tempfile.TemporaryDirectory() as tmp:
            asset = Path(tmp) / "payload.txt"
            asset.write_bytes(PNG_BYTES)

            metadata = {"asset_type": "text"}
            assert detect_asset_type(asset, metadata) == "image"
            assert metadata[ASSET_TYPE_MISMATCH_KEY] == {"declared": "text", "detected": "image"}

    def test_honest_declaration_records_no_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            asset = Path(tmp) / "photo.png"
            asset.write_bytes(PNG_BYTES)

            metadata = {"asset_type": "image"}
            assert detect_asset_type(asset, metadata) == "image"
            assert ASSET_TYPE_MISMATCH_KEY not in metadata

    def test_text_declaration_on_real_text_is_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            asset = Path(tmp) / "caption.txt"
            asset.write_text("hello from a normal player", encoding="utf-8")

            metadata = {"asset_type": "text"}
            assert detect_asset_type(asset, metadata) == "text"
            assert ASSET_TYPE_MISMATCH_KEY not in metadata


class BinaryAsTextTests:
    def test_binary_declared_as_text_reads_as_empty(self):
        """errors="ignore" used to yield mojibake the text agent would adjudicate."""
        with tempfile.TemporaryDirectory() as tmp:
            asset = Path(tmp) / "payload.txt"
            asset.write_bytes(b"\xff\xfe\x00\x80\x81\x82 binary garbage \xff")

            case = Case(id="binary-as-text", asset_type="text", asset_path=str(asset), metadata={})
            assert read_text_asset(case) == ""


class SpoofedRoutingTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.base_path = Path(self.tmpdir.name)
        self.db_path = self.base_path / "audit.sqlite"
        init_db(self.db_path)

    def tearDown(self):
        self.tmpdir.cleanup()

    def _case(self, payload: bytes, declared: str) -> Case:
        asset = self.base_path / "upload.dat"
        asset.write_bytes(payload)
        return Case(
            id="spoof-001",
            asset_type=declared,
            asset_path=str(asset),
            metadata={"analysis_mode": "production"},
        )

    def test_image_declared_as_text_is_not_allowed(self):
        case = self._case(PNG_BYTES, "text")
        permissive = ProductionAssessment(
            decision="allow",
            category="No Violation",
            confidence=0.99,
            rationale="Nothing of concern in the text.",
            evidence_summary="Benign.",
        )

        with patch("sentinel.tools.production_analysis.analyze_asset", return_value=permissive) as analyze:
            result = run_case(case, db_path=self.db_path)

        # The mismatch short-circuits before adjudication, so no agent runs.
        analyze.assert_not_called()
        self.assertNotEqual(result.verdict.decision, "allow")
        self.assertEqual(result.verdict.decision, "ambiguous")
        self.assertEqual(result.verdict.reviewer, "human")

    def test_mismatch_is_recorded_in_the_trace_and_metadata(self):
        case = self._case(MP4_BYTES, "text")

        result = run_case(case, db_path=self.db_path)

        self.assertTrue(
            any("asset_type_mismatch" in event for event in result.trace),
            f"expected a mismatch event in the trace, got {result.trace}",
        )
        self.assertEqual(
            case.metadata[ASSET_TYPE_MISMATCH_KEY],
            {"declared": "text", "detected": "video"},
        )
        self.assertIn("contradicts", result.verdict.rationale)

    def test_honest_upload_still_reaches_adjudication(self):
        """The guard must not fire on correctly-labelled content."""
        case = self._case(PNG_BYTES, "image")
        assessment = ProductionAssessment(
            decision="allow",
            category="No Violation",
            confidence=0.95,
            rationale="Benign image.",
            evidence_summary="Benign.",
        )

        with patch("sentinel.tools.production_analysis.analyze_asset", return_value=assessment) as analyze:
            result = run_case(case, db_path=self.db_path)

        analyze.assert_called()
        self.assertEqual(result.verdict.decision, "allow")


if __name__ == "__main__":
    unittest.main()
