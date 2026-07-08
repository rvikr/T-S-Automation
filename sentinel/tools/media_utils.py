from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Iterable

from sentinel.config import QUARANTINE_DIR, SYNTHETIC_CASES_DIR
from sentinel.models import Case


IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".ogg"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv"}
TEXT_EXTENSIONS = {".txt", ".md", ".json", ".synthetic"}


def detect_asset_type(path: str | Path, metadata: dict | None = None) -> str:
    if metadata and metadata.get("asset_type"):
        return str(metadata["asset_type"])
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
    target_dir = Path(quarantine_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    source = Path(case.asset_path)
    marker = target_dir / f"{case.id}.quarantined.txt"
    marker.write_text(
        "Synthetic stand-in quarantined. No content analysis or reproduction performed.\n",
        encoding="utf-8",
    )
    if source.exists() and source.is_file():
        target = target_dir / source.name
        if source.resolve() != target.resolve():
            shutil.copyfile(source, target)
    return True
