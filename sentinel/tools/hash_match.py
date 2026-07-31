from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

try:
    from agents import RunContextWrapper, function_tool
except ImportError:  # pragma: no cover
    RunContextWrapper = Any  # type: ignore[misc, assignment]

    def function_tool(func):  # type: ignore[no-redef]
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

    This function is the integration seam for Tier-1 detection. It intentionally
    has no external dependencies so the routing flow is demonstrable without
    service registration or an API key. The three-level upgrade path is:

    Short-term (no code change required):
        Add real SHA-256 hex digests of known-violation files to
        ``sentinel/data/known_hashes.txt``, one per line. Lines starting with
        ``#`` are treated as comments; blank lines are ignored. The pipeline
        already quarantines and tickets any asset whose hash matches.

    Medium-term — PDQ perceptual hashing (free, open-source, no external service):
        SHA-256 only catches exact byte-for-byte copies. Re-encoded, resized, or
        slightly edited images evade it. To catch near-duplicates:
        1. ``pip install pdqhash`` (Meta's open-source perceptual hash library).
        2. Compute a PDQ hash alongside SHA-256: ``import pdqhash`` and call
           ``pdqhash.compute(image_array)`` on the decoded image bytes.
        3. Store PDQ hashes in ``known_hashes.txt`` with a ``pdq:`` prefix, e.g.
           ``pdq:f8f8f0cee0f4a84f0696f14ee3d64c4f...``.
        4. In this function, read lines with a ``pdq:`` prefix separately and
           compare with Hamming distance ≤ 10 (PDQ's recommended threshold).
        The function signature stays identical; only the comparison body changes.

    Long-term — NCMEC / PhotoDNA (requires registration or subscription):
        For a production platform handling real user-generated content, industry
        standard is to submit image/video hashes to the NCMEC hash database.
        Steps:
        1. Register as an Electronic Service Provider (ESP) at
           https://www.missingkids.org/gethelpnow/cybertipline — this is a legal
           and compliance process, not a code problem.
        2. Replace the local-list lookup in this function body with an HTTP call
           to the NCMEC hash-check API. The function signature stays the same.
        Alternative: Azure Content Safety (PhotoDNA-based) offers perceptual
        image matching without ESP registration via an Azure subscription.
        In both cases, the calling code in ``orchestrator.py`` and
        ``hash_match_tool`` are unchanged — only this function body changes.
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
