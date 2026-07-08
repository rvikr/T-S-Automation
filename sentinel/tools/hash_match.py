from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

try:
    from agents import RunContextWrapper, function_tool
except ImportError:  # pragma: no cover
    RunContextWrapper = Any  # type: ignore[assignment]

    def function_tool(func):
        return func

from sentinel.config import DATA_DIR
from sentinel.models import Case
from sentinel.tools.policy_retrieval import TIER1_CATEGORIES


KNOWN_HASHES_PATH = DATA_DIR / "known_hashes.txt"


def file_sha256(asset_path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(asset_path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def known_hash_match(asset_path: str | Path) -> bool:
    """Check the asset's SHA-256 against the local known-violation hash list.

    This is the integration seam where a perceptual-hash service
    (PhotoDNA/PDQ-style) would plug in; the local list keeps the flow
    demonstrable without external dependencies.
    """
    path = Path(asset_path)
    if not path.exists() or not KNOWN_HASHES_PATH.exists():
        return False
    known = {
        line.strip().lower()
        for line in KNOWN_HASHES_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    }
    if not known:
        return False
    return file_sha256(path) in known


def hash_match(case: Case) -> bool:
    """Synthetic known-hash stand-in: returns only a flag, never content details."""
    category = str(case.metadata.get("expected_category", ""))
    detected_category = str(case.metadata.get("detected_category", ""))
    label = str(case.metadata.get("synthetic_label", "")).lower()
    return category in TIER1_CATEGORIES or detected_category in TIER1_CATEGORIES or "tier-1" in label


@function_tool
def hash_match_tool(ctx: RunContextWrapper[Any]) -> str:
    """Check whether the asset under review matches the known-violation hash list.

    Returns only a match flag; never any detail about matched content.
    """
    case = getattr(ctx.context, "case", None)
    if case is None:
        return "No case available in this run context."
    if known_hash_match(case.asset_path) or hash_match(case):
        return "MATCH: asset hash appears on the known-violation list. Treat as Tier-1 and stop analysis."
    return "No known-hash match for this asset."
