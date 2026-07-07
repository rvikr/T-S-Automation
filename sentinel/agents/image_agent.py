from __future__ import annotations

from pathlib import Path

from sentinel.agents.common import build_sdk_agent, specialist_review
from sentinel.models import Case, Verdict
from sentinel.tools.policy_retrieval import retrieve_policy_tool
from sentinel.tools.precedent_memory import retrieve_precedents_tool


IMAGE_AGENT_INSTRUCTIONS = (
    "Review synthetic image labels and sampled metadata against policy. "
    "Never analyze Tier-1 details; return only routing-safe verdicts."
)


def build_image_agent():
    return build_sdk_agent("Image agent", IMAGE_AGENT_INSTRUCTIONS, [retrieve_policy_tool, retrieve_precedents_tool])


def review_case(case: Case, db_path: str | Path) -> Verdict:
    return specialist_review(case, "image-specialist", db_path)
