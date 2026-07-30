"""Tests for quarantine encryption at rest."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cryptography.fernet import Fernet

from sentinel.models import Case
from sentinel.tools.content_crypto import (
    ENCRYPTED_SUFFIX,
    ENCRYPTION_KEY_ENV,
    decrypt_bytes,
    encrypt_bytes,
    encryption_enabled,
)
from sentinel.tools.content_crypto import (
    main as crypto_cli,
)
from sentinel.tools.media_utils import quarantine


class ContentCryptoTests(unittest.TestCase):
    def setUp(self):
        self.key = Fernet.generate_key().decode("ascii")
        self._env = patch.dict("os.environ", {ENCRYPTION_KEY_ENV: self.key})
        self._env.start()

    def tearDown(self):
        self._env.stop()

    def test_roundtrip(self):
        payload = b"the worst content on the platform"
        token = encrypt_bytes(payload)
        self.assertNotIn(payload, token)
        self.assertEqual(decrypt_bytes(token), payload)

    def test_disabled_without_key(self):
        with patch.dict("os.environ", {ENCRYPTION_KEY_ENV: ""}):
            self.assertFalse(encryption_enabled())

    def test_malformed_key_fails_loudly_not_plaintext(self):
        with patch.dict("os.environ", {ENCRYPTION_KEY_ENV: "not-a-key"}):
            with self.assertRaises(RuntimeError):
                encrypt_bytes(b"data")

    def test_decrypt_cli_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            enc = Path(tmp) / f"asset.bin{ENCRYPTED_SUFFIX}"
            enc.write_bytes(encrypt_bytes(b"evidence"))
            out = Path(tmp) / "asset.out"

            code = crypto_cli(["decrypt", str(enc), "--output", str(out)])

            self.assertEqual(code, 0)
            self.assertEqual(out.read_bytes(), b"evidence")

    def test_decrypt_cli_wrong_key_reports_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            enc = Path(tmp) / f"asset.bin{ENCRYPTED_SUFFIX}"
            enc.write_bytes(encrypt_bytes(b"evidence"))
            with patch.dict("os.environ", {ENCRYPTION_KEY_ENV: Fernet.generate_key().decode("ascii")}):
                code = crypto_cli(["decrypt", str(enc)])
        self.assertEqual(code, 1)


class QuarantineEncryptionTests(unittest.TestCase):
    def setUp(self):
        self.key = Fernet.generate_key().decode("ascii")
        self.tmpdir = tempfile.TemporaryDirectory()
        self.base = Path(self.tmpdir.name)

    def tearDown(self):
        self.tmpdir.cleanup()

    def _production_case(self) -> Case:
        asset = self.base / "upload.txt"
        asset.write_text("terrible content", encoding="utf-8")
        return Case(
            id="case-q",
            asset_type="text",
            asset_path=str(asset),
            metadata={"analysis_mode": "production", "moderation_run_id": "run-q1"},
        )

    def test_quarantine_encrypts_and_removes_plaintext_when_key_set(self):
        case = self._production_case()
        qdir = self.base / "quarantine"
        with patch.dict("os.environ", {ENCRYPTION_KEY_ENV: self.key}):
            self.assertTrue(quarantine(case, quarantine_dir=qdir))

            self.assertFalse(Path(case.asset_path).exists(), "plaintext source must be gone")
            encrypted = list(qdir.glob(f"*{ENCRYPTED_SUFFIX}"))
            self.assertEqual(len(encrypted), 1)
            blob = encrypted[0].read_bytes()
            self.assertNotIn(b"terrible content", blob)
            self.assertEqual(decrypt_bytes(blob), b"terrible content")

    def test_quarantine_without_key_keeps_legacy_plain_move(self):
        case = self._production_case()
        qdir = self.base / "quarantine"
        with patch.dict("os.environ", {ENCRYPTION_KEY_ENV: ""}):
            self.assertTrue(quarantine(case, quarantine_dir=qdir))
        moved = [p for p in qdir.iterdir() if p.name.endswith("upload.txt")]
        self.assertEqual(len(moved), 1)
        self.assertEqual(moved[0].read_text(encoding="utf-8"), "terrible content")


if __name__ == "__main__":
    unittest.main()
