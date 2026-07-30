"""Signed delivery of moderation results to caller-provided callback URLs.

Security model: the callback URL arrives from the API caller, which makes it
an SSRF vector — the service would POST to any address the caller names,
including internal ones. Delivery is therefore *disabled unless the operator
allowlists hosts* via ``SENTINEL_WEBHOOK_ALLOWED_HOSTS`` (comma-separated,
exact hostname match). Redirects are never followed for the same reason.

When ``SENTINEL_WEBHOOK_SECRET`` is set, every delivery carries an
``X-Sentinel-Signature: sha256=<hmac>`` header over the exact request body so
receivers can authenticate the payload.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
from typing import Any
from urllib.parse import urlparse

import requests

from sentinel.config import WEBHOOK_TIMEOUT_SECONDS

logger = logging.getLogger(__name__)

WEBHOOK_ALLOWED_HOSTS_ENV = "SENTINEL_WEBHOOK_ALLOWED_HOSTS"
WEBHOOK_SECRET_ENV = "SENTINEL_WEBHOOK_SECRET"

SIGNATURE_HEADER = "X-Sentinel-Signature"


def allowed_webhook_hosts() -> set[str]:
    raw = os.getenv(WEBHOOK_ALLOWED_HOSTS_ENV, "")
    return {host.strip().lower() for host in raw.split(",") if host.strip()}


def validate_callback_url(url: str) -> str | None:
    """Return a rejection reason for a callback URL, or None when acceptable.

    Fails closed: no allowlist configured means no webhook delivery at all.
    """
    hosts = allowed_webhook_hosts()
    if not hosts:
        return (
            "Webhook delivery is disabled: the operator has not configured "
            f"{WEBHOOK_ALLOWED_HOSTS_ENV}."
        )
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return f"callback_url must use http or https, not {parsed.scheme!r}"
    hostname = (parsed.hostname or "").lower()
    if not hostname:
        return "callback_url has no hostname"
    if hostname not in hosts:
        return f"callback_url host {hostname!r} is not on the configured allowlist"
    return None


def sign_payload(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def deliver_webhook(url: str, payload: dict[str, Any]) -> bool:
    """POST the payload to the (pre-validated) callback URL. Returns success.

    Failure is logged and reported to the caller in the API response, but never
    fails the moderation request itself — the verdict and audit row already
    exist, and the caller can re-fetch via /moderation/logs.
    """
    body = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    secret = os.getenv(WEBHOOK_SECRET_ENV, "").strip()
    if secret:
        headers[SIGNATURE_HEADER] = sign_payload(body, secret)
    try:
        response = requests.post(
            url,
            data=body,
            headers=headers,
            timeout=WEBHOOK_TIMEOUT_SECONDS,
            allow_redirects=False,
        )
    except requests.RequestException:
        logger.exception("Webhook delivery to %s failed", url)
        return False
    if not 200 <= response.status_code < 300:
        logger.warning("Webhook delivery to %s returned status %s", url, response.status_code)
        return False
    return True
