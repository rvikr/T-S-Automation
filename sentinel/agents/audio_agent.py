from __future__ import annotations

from pathlib import Path

from sentinel.agents.common import build_sdk_agent, specialist_review
from sentinel.models import Case, Verdict
from sentinel.tools.media_utils import transcribe_audio
from sentinel.tools.policy_retrieval import retrieve_policy_tool
from sentinel.tools.precedent_memory import retrieve_precedents_tool


AUDIO_AGENT_INSTRUCTIONS = (
    "Transcribe synthetic audio labels, check policy, and escalate unclear context."
)


def build_audio_agent():
    return build_sdk_agent("Audio agent", AUDIO_AGENT_INSTRUCTIONS, [retrieve_policy_tool, retrieve_precedents_tool])


def review_case(case: Case, db_path: str | Path) -> Verdict:
    _ = transcribe_audio(case)
    return specialist_review(case, "audio-specialist", db_path)
