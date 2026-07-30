"""Tests for the content-hash allow-verdict cache.

The invariant under test: the cache may only ever skip work in the safe
direction. An allow verdict for identical bytes can be reused; a rejection,
escalation, or Tier-1 route must always re-run the full pipeline.
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sentinel.agents.orchestrator import run_case
from sentinel.models import Case, ProductionAssessment
from sentinel.tools.audit_log import init_db
from sentinel.tools.verdict_cache import VERDICT_CACHE_ENV, lookup_allow_verdict

_ALLOW = ProductionAssessment(
    decision="allow",
    category="No Violation",
    confidence=0.95,
    rationale="Benign gameplay text.",
    evidence_summary="Benign.",
)
_REJECT = ProductionAssessment(
    decision="reject",
    category="Spam",
    confidence=0.9,
    rationale="Promotional spam.",
    evidence_summary="Spam signal.",
)


class VerdictCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.base = Path(self.tmpdir.name)
        self.db_path = self.base / "audit.sqlite"
        init_db(self.db_path)
        self._env = patch.dict("os.environ", {VERDICT_CACHE_ENV: "1"})
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self.tmpdir.cleanup()

    def _case(self, name: str, content: str) -> Case:
        asset = self.base / name
        asset.write_text(content, encoding="utf-8")
        return Case(
            id=f"case-{name}",
            asset_type="text",
            asset_path=str(asset),
            metadata={"analysis_mode": "production"},
        )

    def test_second_identical_upload_hits_cache_and_skips_agents(self):
        with patch("sentinel.tools.production_analysis.analyze_asset", return_value=_ALLOW) as analyze:
            first = run_case(self._case("a.txt", "hello world"), db_path=self.db_path)
        self.assertEqual(first.verdict.decision, "allow")
        self.assertEqual(analyze.call_count, 1)

        with patch("sentinel.tools.production_analysis.analyze_asset", return_value=_REJECT) as analyze:
            second = run_case(self._case("b.txt", "hello world"), db_path=self.db_path)

        analyze.assert_not_called()
        self.assertEqual(second.verdict.decision, "allow")
        self.assertEqual(second.verdict.reviewer, "cache")
        self.assertIn("cache.hit:allow", second.trace)

    def test_different_content_misses_cache(self):
        with patch("sentinel.tools.production_analysis.analyze_asset", return_value=_ALLOW) as analyze:
            run_case(self._case("a.txt", "hello world"), db_path=self.db_path)
            run_case(self._case("b.txt", "different bytes"), db_path=self.db_path)
        self.assertEqual(analyze.call_count, 2)

    def test_rejections_are_never_cached(self):
        with patch("sentinel.tools.production_analysis.analyze_asset", return_value=_REJECT) as analyze:
            run_case(self._case("a.txt", "spam spam"), db_path=self.db_path)
            result = run_case(self._case("b.txt", "spam spam"), db_path=self.db_path)
        self.assertEqual(analyze.call_count, 2)
        self.assertEqual(result.verdict.decision, "reject")

    def test_cache_disabled_by_default(self):
        self._env.stop()
        try:
            with patch.dict("os.environ", {VERDICT_CACHE_ENV: ""}):
                with patch("sentinel.tools.production_analysis.analyze_asset", return_value=_ALLOW) as analyze:
                    run_case(self._case("a.txt", "hello world"), db_path=self.db_path)
                    run_case(self._case("b.txt", "hello world"), db_path=self.db_path)
                self.assertEqual(analyze.call_count, 2)
        finally:
            self._env.start()

    def test_synthetic_cases_never_use_the_cache(self):
        synthetic = Case(
            id="syn-1",
            asset_type="text",
            asset_path=str(self.base / "a.txt"),
            metadata={"expected_category": "No Violation", "expected_decision": "allow"},
        )
        (self.base / "a.txt").write_text("hello world", encoding="utf-8")
        self.assertIsNone(lookup_allow_verdict(synthetic, self.db_path))


if __name__ == "__main__":
    unittest.main()
