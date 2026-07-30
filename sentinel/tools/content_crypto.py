"""Encryption at rest for quarantined content.

Quarantine holds the worst content the platform sees. With
``SENTINEL_ENCRYPTION_KEY`` set (a Fernet key), assets are encrypted as they
enter quarantine, so a leaked disk image or backup does not leak the content
itself. Reviewers decrypt on demand:

    python -m sentinel.tools.content_crypto generate-key
    python -m sentinel.tools.content_crypto decrypt path/to/file.enc [--output out.bin]

Scope, stated plainly: this covers quarantined files only. Uploads must stay
readable while the pipeline analyzes them, and audit rows live in SQLite where
field-level encryption would break querying — both are documented gaps in
SECURITY.md, not silent ones.

Losing the key means losing access to everything encrypted under it. Store it
in a secrets manager, not in the repo.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

ENCRYPTION_KEY_ENV = "SENTINEL_ENCRYPTION_KEY"

# Suffix marking a Sentinel-encrypted file.
ENCRYPTED_SUFFIX = ".enc"


def encryption_enabled() -> bool:
    return bool(os.getenv(ENCRYPTION_KEY_ENV, "").strip())


def _fernet() -> Fernet:
    key = os.getenv(ENCRYPTION_KEY_ENV, "").strip()
    if not key:
        raise RuntimeError(f"{ENCRYPTION_KEY_ENV} is not set")
    try:
        return Fernet(key.encode("utf-8"))
    except (ValueError, TypeError) as exc:
        # A malformed key must fail loudly at use, not silently store plaintext.
        raise RuntimeError(
            f"{ENCRYPTION_KEY_ENV} is not a valid Fernet key; generate one with "
            "`python -m sentinel.tools.content_crypto generate-key`"
        ) from exc


def encrypt_bytes(data: bytes) -> bytes:
    return _fernet().encrypt(data)


def decrypt_bytes(token: bytes) -> bytes:
    return _fernet().decrypt(token)


def encrypt_file_to(source: str | Path, target: str | Path) -> Path:
    """Encrypt ``source`` into ``target`` (typically ``<name>.enc``)."""
    source_path = Path(source)
    target_path = Path(target)
    target_path.write_bytes(encrypt_bytes(source_path.read_bytes()))
    return target_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sentinel content encryption utilities.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("generate-key", help="Print a new Fernet key for SENTINEL_ENCRYPTION_KEY.")
    decrypt = sub.add_parser("decrypt", help="Decrypt an encrypted quarantine file for review.")
    decrypt.add_argument("path", help="Path to the .enc file.")
    decrypt.add_argument("--output", default=None, help="Write here instead of stripping the .enc suffix.")
    args = parser.parse_args(argv)

    if args.command == "generate-key":
        print(Fernet.generate_key().decode("ascii"))
        return 0

    source = Path(args.path)
    output = Path(args.output) if args.output else source.with_suffix("")
    try:
        output.write_bytes(decrypt_bytes(source.read_bytes()))
    except InvalidToken:
        print(
            "Decryption failed: wrong key, or the file was not encrypted by Sentinel.",
            file=sys.stderr,
        )
        return 1
    print(f"decrypted to {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
