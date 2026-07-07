from __future__ import annotations

try:
    from agents import function_tool
except ImportError:  # pragma: no cover
    def function_tool(func):
        return func

from sentinel.models import Case
from sentinel.tools.policy_retrieval import TIER1_CATEGORIES


def hash_match(case: Case) -> bool:
    """Synthetic known-hash stand-in: returns only a flag, never content details."""
    category = str(case.metadata.get("expected_category", ""))
    detected_category = str(case.metadata.get("detected_category", ""))
    label = str(case.metadata.get("synthetic_label", "")).lower()
    return category in TIER1_CATEGORIES or detected_category in TIER1_CATEGORIES or "tier-1" in label


@function_tool
def hash_match_tool(case_id: str, category: str) -> bool:
    """Return whether a synthetic case matches the Tier-1 known-hash stub."""
    return category in TIER1_CATEGORIES
