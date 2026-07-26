"""Unit tests for sentinel/tools/hash_match.py.

Covers file_sha256(), known_hash_match(), and graceful fallback when the
known_hashes.txt file is absent.  All tests use temporary files and are
fully offline.
"""

from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from sentinel.tools.hash_match import file_sha256, known_hash_match


# ---------------------------------------------------------------------------
# file_sha256
# ---------------------------------------------------------------------------

class FileSHA256Tests:
    def test_returns_correct_sha256(self):
        with tempfile.NamedTemporaryFile(delete=False, suffix=".txt") as tmp:
            tmp.write(b"hello sentinel")
            tmp_path = tmp.name

        expected = hashlib.sha256(b"hello sentinel").hexdigest()
        assert file_sha256(tmp_path) == expected

    def test_returns_lowercase_hex(self):
        with tempfile.NamedTemporaryFile(delete=False, suffix=".bin") as tmp:
            tmp.write(b"\x00\xff\xab")
            tmp_path = tmp.name

        result = file_sha256(tmp_path)
        assert result == result.lower()
        assert len(result) == 64  # SHA-256 is 32 bytes = 64 hex chars

    def test_empty_file_returns_known_hash(self):
        with tempfile.NamedTemporaryFile(delete=False, suffix=".txt") as tmp:
            tmp_path = tmp.name

        # SHA-256 of empty input is well-known.
        expected = hashlib.sha256(b"").hexdigest()
        assert file_sha256(tmp_path) == expected


# ---------------------------------------------------------------------------
# known_hash_match
# ---------------------------------------------------------------------------

class KnownHashMatchTests:
    def _write_asset(self, content: bytes, tmp_dir: str) -> Path:
        asset = Path(tmp_dir) / "asset.bin"
        asset.write_bytes(content)
        return asset

    def _write_hash_list(self, hashes: list[str], tmp_dir: str) -> Path:
        hash_file = Path(tmp_dir) / "known_hashes.txt"
        hash_file.write_text("\n".join(hashes) + "\n", encoding="utf-8")
        return hash_file

    def test_returns_true_for_matching_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            content = b"malicious content"
            asset = self._write_asset(content, tmp)
            digest = hashlib.sha256(content).hexdigest()
            hash_file = self._write_hash_list([digest], tmp)

            with patch("sentinel.tools.hash_match.KNOWN_HASHES_PATH", hash_file):
                assert known_hash_match(asset) is True

    def test_returns_false_for_non_matching_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            content = b"clean content"
            asset = self._write_asset(content, tmp)
            # Write a different hash in the list.
            hash_file = self._write_hash_list(["abcd1234" * 8], tmp)

            with patch("sentinel.tools.hash_match.KNOWN_HASHES_PATH", hash_file):
                assert known_hash_match(asset) is False

    def test_returns_false_when_hash_file_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            asset = self._write_asset(b"any content", tmp)
            missing_path = Path(tmp) / "nonexistent_hashes.txt"

            with patch("sentinel.tools.hash_match.KNOWN_HASHES_PATH", missing_path):
                # Must return False, not raise.
                assert known_hash_match(asset) is False

    def test_returns_false_when_asset_file_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing_asset = Path(tmp) / "ghost_file.bin"
            hash_file = self._write_hash_list(["somehash" * 8], tmp)

            with patch("sentinel.tools.hash_match.KNOWN_HASHES_PATH", hash_file):
                assert known_hash_match(missing_asset) is False

    def test_ignores_comment_lines_in_hash_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            content = b"clean content"
            asset = self._write_asset(content, tmp)
            digest = hashlib.sha256(content).hexdigest()
            # The hash is preceded by a comment line — should still match.
            hash_file = self._write_hash_list(
                ["# this is a comment", digest, "# another comment"], tmp
            )

            with patch("sentinel.tools.hash_match.KNOWN_HASHES_PATH", hash_file):
                assert known_hash_match(asset) is True

    def test_returns_false_for_empty_hash_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            asset = self._write_asset(b"some content", tmp)
            hash_file = self._write_hash_list([], tmp)

            with patch("sentinel.tools.hash_match.KNOWN_HASHES_PATH", hash_file):
                assert known_hash_match(asset) is False

    def test_hash_matching_is_case_insensitive(self):
        """Hash list entries in upper case should still match the asset's lower-case digest."""
        with tempfile.TemporaryDirectory() as tmp:
            content = b"case test"
            asset = self._write_asset(content, tmp)
            digest_upper = hashlib.sha256(content).hexdigest().upper()
            hash_file = self._write_hash_list([digest_upper], tmp)

            with patch("sentinel.tools.hash_match.KNOWN_HASHES_PATH", hash_file):
                assert known_hash_match(asset) is True
