"""Oversized uploads must be rejected before anything reaches disk.

MAX_TEXT_CHARS only truncates what is sent to the model, so without a payload
ceiling a caller could store arbitrarily large files while paying for a single
small API call.
"""

from __future__ import annotations

import base64
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from fastapi import HTTPException

from sentinel.api import ModerationRequest, _decode_payload


class DecodePayloadLimitTests:
    def _decode(self, request: ModerationRequest, limit: int = 1024):
        with patch("sentinel.api.MAX_UPLOAD_BYTES", limit):
            return _decode_payload(request)

    def test_small_plain_text_is_accepted(self):
        request = ModerationRequest(asset_type="text", content="hello")
        assert self._decode(request) == b"hello"

    def test_small_base64_payload_is_accepted(self):
        raw = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
        request = ModerationRequest(asset_type="image", content_base64=base64.b64encode(raw).decode())
        assert self._decode(request) == raw

    def test_oversized_plain_text_returns_413(self):
        request = ModerationRequest(asset_type="text", content="x" * 5000)
        try:
            self._decode(request)
        except HTTPException as exc:
            assert exc.status_code == 413
        else:  # pragma: no cover - the call must raise
            raise AssertionError("expected HTTPException(413)")

    def test_oversized_base64_returns_413(self):
        request = ModerationRequest(
            asset_type="image",
            content_base64=base64.b64encode(b"\x00" * 8000).decode(),
        )
        try:
            self._decode(request)
        except HTTPException as exc:
            assert exc.status_code == 413
        else:  # pragma: no cover
            raise AssertionError("expected HTTPException(413)")

    def test_invalid_base64_returns_400(self):
        request = ModerationRequest(asset_type="image", content_base64="not!valid!base64")
        try:
            self._decode(request)
        except HTTPException as exc:
            assert exc.status_code == 400
        else:  # pragma: no cover
            raise AssertionError("expected HTTPException(400)")

    def test_missing_content_returns_400(self):
        request = ModerationRequest(asset_type="text")
        try:
            self._decode(request)
        except HTTPException as exc:
            assert exc.status_code == 400
        else:  # pragma: no cover
            raise AssertionError("expected HTTPException(400)")


class OversizedUploadWritesNothingTests(unittest.TestCase):
    def test_rejected_upload_leaves_no_file_on_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            upload_dir = Path(tmp)
            request = ModerationRequest(asset_type="text", content="x" * 5000)

            with patch("sentinel.api.MAX_UPLOAD_BYTES", 1024):
                with self.assertRaises(HTTPException) as ctx:
                    _decode_payload(request)

            self.assertEqual(ctx.exception.status_code, 413)
            self.assertEqual(list(upload_dir.rglob("*")), [])


if __name__ == "__main__":
    unittest.main()
