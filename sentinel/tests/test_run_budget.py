"""Tests for the global daily live-run budget."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from sentinel.api import create_app
from sentinel.models import ProductionAssessment
from sentinel.tools.audit_log import init_db
from sentinel.tools.run_budget import DAILY_BUDGET_ENV, consume_daily_budget, daily_limit


class ConsumeDailyBudgetTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "audit.sqlite"
        init_db(self.db_path)

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_enforces_limit(self):
        results = [consume_daily_budget(self.db_path, limit=2)[0] for _ in range(4)]
        self.assertEqual(results, [True, True, False, False])

    def test_zero_limit_disables(self):
        results = [consume_daily_budget(self.db_path, limit=0)[0] for _ in range(10)]
        self.assertEqual(results, [True] * 10)

    def test_budget_resets_on_a_new_day(self):
        with patch("sentinel.tools.run_budget._today", return_value="2026-07-30"):
            self.assertTrue(consume_daily_budget(self.db_path, limit=1)[0])
            self.assertFalse(consume_daily_budget(self.db_path, limit=1)[0])
        with patch("sentinel.tools.run_budget._today", return_value="2026-07-31"):
            self.assertTrue(consume_daily_budget(self.db_path, limit=1)[0])

    def test_env_limit_parsing_fails_safe_to_disabled(self):
        with patch.dict("os.environ", {DAILY_BUDGET_ENV: "lots"}):
            self.assertEqual(daily_limit(), 0)
        with patch.dict("os.environ", {DAILY_BUDGET_ENV: "-5"}):
            self.assertEqual(daily_limit(), 0)
        with patch.dict("os.environ", {DAILY_BUDGET_ENV: "500"}):
            self.assertEqual(daily_limit(), 500)


class ApiDailyBudgetTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        base = Path(self.tmpdir.name)
        self.client = TestClient(
            create_app(
                db_path=base / "audit.sqlite",
                upload_dir=base / "uploads",
                admin_token="admin-secret",
                daily_live_run_limit=2,
            )
        )
        response = self.client.post(
            "/admin/api-keys",
            headers={"Authorization": "Bearer admin-secret"},
            json={"tenant_name": "T", "project_name": "P", "environment": "test"},
        )
        self.key = response.json()["api_key"]

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_budget_exhaustion_returns_429_with_retry_after(self):
        assessment = ProductionAssessment(
            decision="allow",
            category="No Violation",
            confidence=0.95,
            rationale="Benign.",
            evidence_summary="Benign.",
        )
        with patch("sentinel.tools.production_analysis.analyze_asset", return_value=assessment):
            statuses = [
                self.client.post(
                    "/moderation/cases",
                    headers={"Authorization": f"Bearer {self.key}"},
                    json={"asset_type": "text", "content": f"hello {n}"},
                ).status_code
                for n in range(3)
            ]
            final = self.client.post(
                "/moderation/cases",
                headers={"Authorization": f"Bearer {self.key}"},
                json={"asset_type": "text", "content": "hello again"},
            )

        self.assertEqual(statuses, [201, 201, 429])
        self.assertEqual(final.status_code, 429)
        self.assertTrue(final.headers.get("Retry-After"))
        self.assertIn("Daily moderation budget exhausted", final.json()["detail"])


if __name__ == "__main__":
    unittest.main()
