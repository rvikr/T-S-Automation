"""Tests for signed verdict webhooks and their SSRF guard."""

import hashlib
import hmac
import importlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from sentinel.models import ProductionAssessment
from sentinel.tools.webhook import (
    SIGNATURE_HEADER,
    WEBHOOK_ALLOWED_HOSTS_ENV,
    WEBHOOK_SECRET_ENV,
    deliver_webhook,
    validate_callback_url,
)


class ValidateCallbackUrlTests(unittest.TestCase):
    def test_no_allowlist_means_disabled(self):
        with patch.dict("os.environ", {WEBHOOK_ALLOWED_HOSTS_ENV: ""}):
            self.assertIsNotNone(validate_callback_url("https://hooks.example.com/x"))

    def test_host_on_allowlist_passes(self):
        with patch.dict("os.environ", {WEBHOOK_ALLOWED_HOSTS_ENV: "hooks.example.com, other.example.org"}):
            self.assertIsNone(validate_callback_url("https://hooks.example.com/moderation"))
            self.assertIsNone(validate_callback_url("http://OTHER.example.org/cb"))

    def test_host_off_allowlist_rejected(self):
        with patch.dict("os.environ", {WEBHOOK_ALLOWED_HOSTS_ENV: "hooks.example.com"}):
            # The classic SSRF targets must never validate.
            self.assertIsNotNone(validate_callback_url("http://169.254.169.254/latest/meta-data"))
            self.assertIsNotNone(validate_callback_url("http://localhost:8000/admin/api-keys"))

    def test_non_http_scheme_rejected(self):
        with patch.dict("os.environ", {WEBHOOK_ALLOWED_HOSTS_ENV: "hooks.example.com"}):
            self.assertIsNotNone(validate_callback_url("ftp://hooks.example.com/x"))
            self.assertIsNotNone(validate_callback_url("file:///etc/passwd"))


class DeliverWebhookTests(unittest.TestCase):
    def test_successful_delivery(self):
        response = MagicMock(status_code=200)
        with patch("sentinel.tools.webhook.requests.post", return_value=response) as post:
            self.assertTrue(deliver_webhook("https://hooks.example.com/x", {"case_id": "c1"}))
        kwargs = post.call_args.kwargs
        # Redirect-following would reopen the SSRF door after validation.
        self.assertFalse(kwargs["allow_redirects"])

    def test_non_2xx_and_network_failure_report_false(self):
        import requests as requests_lib

        with patch("sentinel.tools.webhook.time.sleep"):
            with patch("sentinel.tools.webhook.requests.post", return_value=MagicMock(status_code=500)):
                self.assertFalse(deliver_webhook("https://hooks.example.com/x", {}))
            with patch("sentinel.tools.webhook.requests.post", side_effect=requests_lib.ConnectionError):
                self.assertFalse(deliver_webhook("https://hooks.example.com/x", {}))

    def test_transient_failures_are_retried_until_success(self):
        import requests as requests_lib

        responses = [requests_lib.ConnectionError(), MagicMock(status_code=503), MagicMock(status_code=200)]
        with (
            patch("sentinel.tools.webhook.time.sleep") as sleep,
            patch("sentinel.tools.webhook.requests.post", side_effect=responses) as post,
        ):
            self.assertTrue(deliver_webhook("https://hooks.example.com/x", {"case": "c1"}))
        self.assertEqual(post.call_count, 3)
        self.assertEqual(sleep.call_count, 2)

    def test_permanent_rejection_is_not_retried(self):
        with (
            patch("sentinel.tools.webhook.time.sleep") as sleep,
            patch("sentinel.tools.webhook.requests.post", return_value=MagicMock(status_code=404)) as post,
        ):
            self.assertFalse(deliver_webhook("https://hooks.example.com/x", {}))
        # A 404 means the receiver rejected the payload; retrying cannot fix it.
        self.assertEqual(post.call_count, 1)
        sleep.assert_not_called()

    def test_signature_header_when_secret_configured(self):
        response = MagicMock(status_code=204)
        payload = {"case_id": "c1", "verdict": "allow"}
        with (
            patch.dict("os.environ", {WEBHOOK_SECRET_ENV: "topsecret"}),
            patch("sentinel.tools.webhook.requests.post", return_value=response) as post,
        ):
            self.assertTrue(deliver_webhook("https://hooks.example.com/x", payload))
        kwargs = post.call_args.kwargs
        body = kwargs["data"]
        expected = "sha256=" + hmac.new(b"topsecret", body, hashlib.sha256).hexdigest()
        self.assertEqual(kwargs["headers"][SIGNATURE_HEADER], expected)
        self.assertEqual(json.loads(body.decode("utf-8")), payload)


class ModerationApiWebhookTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        base = Path(self.tmpdir.name)
        api = importlib.import_module("sentinel.api")
        from fastapi.testclient import TestClient

        self.api = api
        self.client = TestClient(
            api.create_app(db_path=base / "audit.sqlite", upload_dir=base / "uploads", admin_token="admin-secret")
        )
        response = self.client.post(
            "/admin/api-keys",
            headers={"Authorization": "Bearer admin-secret"},
            json={"tenant_name": "T", "project_name": "P", "environment": "test"},
        )
        self.key = response.json()["api_key"]

    def tearDown(self):
        self.tmpdir.cleanup()

    def _post_case(self, callback_url: str):
        assessment = ProductionAssessment(
            decision="allow",
            category="No Violation",
            confidence=0.95,
            rationale="Benign.",
            evidence_summary="Benign.",
        )
        with patch("sentinel.tools.production_analysis.analyze_asset", return_value=assessment):
            return self.client.post(
                "/moderation/cases",
                headers={"Authorization": f"Bearer {self.key}"},
                json={"asset_type": "text", "content": "hello", "callback_url": callback_url},
            )

    def test_disallowed_callback_rejected_before_moderation(self):
        with patch.dict("os.environ", {WEBHOOK_ALLOWED_HOSTS_ENV: "hooks.example.com"}):
            response = self._post_case("http://internal.attacker.net/cb")

        self.assertEqual(response.status_code, 422)

    def test_allowed_callback_delivers_and_reports_status(self):
        with (
            patch.dict("os.environ", {WEBHOOK_ALLOWED_HOSTS_ENV: "hooks.example.com"}),
            patch("sentinel.api.deliver_webhook", return_value=True) as deliver,
        ):
            response = self._post_case("https://hooks.example.com/moderation")

        self.assertEqual(response.status_code, 201)
        webhook = response.json()["integration"]["webhook"]
        self.assertEqual(webhook, {"url": "https://hooks.example.com/moderation", "delivered": True})
        delivered_payload = deliver.call_args.args[1]
        self.assertEqual(delivered_payload["verdict"]["decision"], "allow")


if __name__ == "__main__":
    unittest.main()
