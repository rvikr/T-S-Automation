from __future__ import annotations

from pathlib import Path

from sentinel.agents.common import build_sdk_agent, specialist_review
from sentinel.models import Case, Verdict
from sentinel.tools.media_utils import sample_video_frames, transcribe_audio
from sentinel.tools.policy_retrieval import retrieve_policy_tool
from sentinel.tools.precedent_memory import retrieve_precedents_tool


VIDEO_AGENT_INSTRUCTIONS = (
    "Sample synthetic video frames, reuse audio transcript signals, and produce a policy-grounded verdict."
)


def build_video_agent():
    return build_sdk_agent("Video agent", VIDEO_AGENT_INSTRUCTIONS, [retrieve_policy_tool, retrieve_precedents_tool])


def review_case(case: Case, db_path: str | Path) -> Verdict:
    _ = sample_video_frames(case)
    _ = transcribe_audio(case)
    return specialist_review(case, "video-specialist", db_path)
