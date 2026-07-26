"""Media asset utilities for Sentinel.

Provides:

* :func:`sniff_asset_type` — infers asset modality from magic bytes.
* :func:`detect_asset_type` — resolves the modality used for agent routing,
  treating the caller-declared type as a hint that the file's bytes can override.
* :func:`load_synthetic_case` — reads a ``.synthetic`` fixture file and
  constructs a :class:`~sentinel.models.Case` for offline testing.
* :func:`quarantine` — moves a flagged asset to the quarantine directory and
  returns whether the move succeeded.
"""

from __future__ import annotations

import json
import re
import shutil
import uuid
from pathlib import Path
from typing import Iterable

from sentinel.config import QUARANTINE_DIR, SYNTHETIC_CASES_DIR
from sentinel.models import Case


IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".ogg"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv"}
TEXT_EXTENSIONS = {".txt", ".md", ".json", ".synthetic"}

# Metadata key set when the declared asset type contradicts the bytes on disk.
ASSET_TYPE_MISMATCH_KEY = "asset_type_mismatch"

# Byte signatures checked from offset 0 unless noted. Kept deliberately small and
# dependency-free: this is a routing safety check, not a full format parser.
_MAGIC_PREFIXES: list[tuple[bytes, str]] = [
    (b"\x89PNG\r\n\x1a\n", "image"),
    (b"\xff\xd8\xff", "image"),  # JPEG
    (b"GIF87a", "image"),
    (b"GIF89a", "image"),
    (b"BM", "image"),  # BMP
    (b"ID3", "audio"),  # MP3 with ID3 tag
    (b"\xff\xfb", "audio"),  # MP3 frame sync
    (b"\xff\xf3", "audio"),
    (b"\xff\xf2", "audio"),
    (b"OggS", "audio"),
    (b"fLaC", "audio"),
    (b"\x1a\x45\xdf\xa3", "video"),  # Matroska / WebM
    (b"FLV\x01", "video"),
]


def sniff_asset_type(path: str | Path) -> str | None:
    """Infer the modality from the file's leading bytes.

    Returns ``None`` when the bytes do not match a known signature — which is the
    normal case for plain text. Never raises: an unreadable file simply yields
    ``None`` so the caller can fall back to extension-based detection.
    """
    try:
        with open(path, "rb") as handle:
            header = handle.read(64)
    except OSError:
        return None
    if not header:
        return None

    for prefix, asset_type in _MAGIC_PREFIXES:
        if header.startswith(prefix):
            return asset_type

    # Container formats identify themselves a few bytes in rather than at 0.
    if header[4:8] == b"ftyp":
        # ISO base media: MP4/MOV are video, M4A is audio.
        return "audio" if header[8:12] in (b"M4A ", b"M4B ") else "video"
    if header[:4] == b"RIFF":
        if header[8:12] == b"WAVE":
            return "audio"
        if header[8:12] == b"AVI ":
            return "video"
        if header[8:12] == b"WEBP":
            return "image"
    return None


def detect_asset_type(path: str | Path, metadata: dict | None = None) -> str:
    """Determine the modality used to route a case to a specialist agent.

    The caller-declared ``metadata["asset_type"]`` is a *hint*, not the truth. If
    the bytes on disk say otherwise, the bytes win and the disagreement is
    recorded under :data:`ASSET_TYPE_MISMATCH_KEY` so the orchestrator can fail
    the case closed to human review. Trusting the declared type unconditionally
    would let an uploader route image or video content to the text specialist,
    where no vision-capable agent — and therefore no Tier-1 output guardrail —
    ever sees it.
    """
    sniffed = sniff_asset_type(path)
    declared = str(metadata["asset_type"]) if metadata and metadata.get("asset_type") else None

    if declared:
        if sniffed and sniffed != declared:
            if metadata is not None:
                metadata[ASSET_TYPE_MISMATCH_KEY] = {"declared": declared, "detected": sniffed}
            return sniffed
        return declared

    if sniffed:
        return sniffed

    suffix = Path(path).suffix.lower()
    if suffix in IMAGE_EXTENSIONS:
        return "image"
    if suffix in AUDIO_EXTENSIONS:
        return "audio"
    if suffix in VIDEO_EXTENSIONS:
        return "video"
    return "text"


def load_synthetic_cases(manifest_path: str | Path | None = None) -> list[Case]:
    manifest = Path(manifest_path) if manifest_path else SYNTHETIC_CASES_DIR / "manifest.json"
    raw_cases = json.loads(manifest.read_text(encoding="utf-8"))
    cases: list[Case] = []
    for item in raw_cases:
        asset_path = (manifest.parent / item["asset_file"]).resolve()
        cases.append(
            Case(
                id=item["id"],
                asset_type=item["asset_type"],
                asset_path=str(asset_path),
                metadata={
                    "synthetic_label": item["label"],
                    "expected_category": item["category"],
                    "expected_decision": item["expected_decision"],
                },
            )
        )
    return cases


def quarantine(case: Case, quarantine_dir: str | Path = QUARANTINE_DIR) -> bool:
    """Isolate production content and mark synthetic stand-ins as quarantined.

    Production assets are moved out of upload storage. Synthetic fixtures are
    intentionally retained because they are reusable, non-sensitive stand-ins.
    """
    target_dir = Path(quarantine_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    source = Path(case.asset_path)
    if not source.exists() or not source.is_file():
        return False

    run_id = str(case.metadata.get("moderation_run_id") or uuid.uuid4().hex)
    safe_run_id = re.sub(r"[^A-Za-z0-9._-]", "_", run_id)
    safe_case_id = re.sub(r"[^A-Za-z0-9._-]", "_", case.id)
    marker = target_dir / f"{safe_case_id}-{safe_run_id}.quarantined.txt"

    if case.metadata.get("analysis_mode") == "production":
        target = target_dir / f"{safe_run_id}-{source.name}"
        if source.resolve() == target.resolve():
            return True
        shutil.move(str(source), str(target))

    marker.write_text(
        "Asset quarantined by Sentinel. No content reproduction performed.\n",
        encoding="utf-8",
    )
    return True
