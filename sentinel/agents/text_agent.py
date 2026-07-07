from __future__ import annotations

from pathlib import Path

from sentinel.agents.common import build_sdk_agent, specialist_review
from sentinel.models import Case, Verdict
from sentinel.tools.policy_retrieval import retrieve_policy_tool
from sentinel.tools.precedent_memory import retrieve_precedents_tool


TEXT_AGENT_INSTRUCTIONS = "Review synthetic titles, captions, and descriptions against the grounded policy corpus."


def build_text_agent():
    return build_sdk_agent("Text agent", TEXT_AGENT_INSTRUCTIONS, [retrieve_policy_tool, retrieve_precedents_tool])


def review_case(case: Case, db_path: str | Path) -> Verdict:
    return specialist_review(case, "text-specialist", db_path)
