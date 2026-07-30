"""Tests for the in-process rate limiter and its API wiring."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from sentinel.api import create_app
from sentinel.tools.rate_limit import RateLimiter


class RateLimiterUnitTests(unittest.TestCase):
    def test_allows_up_to_limit_then_blocks(self):
        limiter = RateLimiter(3)
        with patch("sentinel.tools.rate_limit.time.time", return_value=1_000_000.0):
            results = [limiter.check("1.2.3.4")[0] for _ in range(5)]
        self.assertEqual(results, [True, True, True, False, False])

    def test_window_resets(self):
        limiter = RateLimiter(1)
        with patch("sentinel.tools.rate_limit.time.time", return_value=1_000_000.0):
            self.assertTrue(limiter.check("1.2.3.4")[0])
            self.assertFalse(limiter.check("1.2.3.4")[0])
        with patch("sentinel.tools.rate_limit.time.time", return_value=1_000_060.0):
            self.assertTrue(limiter.check("1.2.3.4")[0])

    def test_keys_are_independent(self):
        limiter = RateLimiter(1)
        with patch("sentinel.tools.rate_limit.time.time", return_value=1_000_000.0):
            self.assertTrue(limiter.check("1.2.3.4")[0])
            self.assertTrue(limiter.check("5.6.7.8")[0])

    def test_zero_limit_disables(self):
        limiter = RateLimiter(0)
        self.assertEqual([limiter.check("x")[0] for _ in range(50)], [True] * 50)

    def test_retry_after_is_at_most_the_window(self):
        limiter = RateLimiter(1)
        with patch("sentinel.tools.rate_limit.time.time", return_value=1_000_000.0):
            limiter.check("1.2.3.4")
            allowed, retry_after = limiter.check("1.2.3.4")
        self.assertFalse(allowed)
        self.assertTrue(1 <= retry_after <= 60)


class ApiRateLimitTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.base = Path(self.tmpdir.name)

    def tearDown(self):
        self.tmpdir.cleanup()

    def _client(self, rate_limit: int, admin_rate_limit: int) -> TestClient:
        return TestClient(
            create_app(
                db_path=self.base / "audit.sqlite",
                upload_dir=self.base / "uploads",
                admin_token="admin-secret",
                rate_limit_per_minute=rate_limit,
                admin_rate_limit_per_minute=admin_rate_limit,
            )
        )

    def test_global_limit_returns_429_with_retry_after(self):
        client = self._client(rate_limit=3, admin_rate_limit=30)
        statuses = [client.get("/health").status_code for _ in range(5)]

        self.assertEqual(statuses[:3], [200, 200, 200])
        self.assertEqual(statuses[3:], [429, 429])
        response = client.get("/health")
        self.assertEqual(response.status_code, 429)
        self.assertTrue(response.headers.get("Retry-After"))
        # 429s still carry a correlation ID (request-context wraps the limiter).
        self.assertTrue(response.headers.get("X-Request-ID"))

    def test_admin_bucket_is_stricter_than_global(self):
        client = self._client(rate_limit=100, admin_rate_limit=2)
        headers = {"Authorization": "Bearer wrong-token"}

        # Brute-force attempts hit the admin bucket after 2 tries...
        statuses = [client.get("/admin/api-keys", headers=headers).status_code for _ in range(4)]
        self.assertEqual(statuses, [401, 401, 429, 429])
        # ...while non-admin routes are untouched by the admin bucket.
        self.assertEqual(client.get("/health").status_code, 200)

    def test_zero_disables_limiting(self):
        client = self._client(rate_limit=0, admin_rate_limit=0)
        statuses = {client.get("/health").status_code for _ in range(30)}
        self.assertEqual(statuses, {200})


if __name__ == "__main__":
    unittest.main()
