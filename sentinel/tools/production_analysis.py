from __future__ import annotations

import base64
import json
import logging
import mimetypes
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Any

from sentinel.config import (
    LLM_MAX_RETRIES,
    LLM_TIMEOUT_SECONDS,
    MAX_TEXT_CHARS,
    MAX_VIDEO_FRAMES,
    load_settings,
)
from sentinel.guardrails import check_prompt_injection
from sentinel.models import Case, ProductionAssessment, Verdict, build_verdict
from sentinel.tools.policy_retrieval import (
    POLICY_CLAUSES,
    TIER1_CATEGORIES,
    get_clause_for_category,
    normalize_category,
)

logger = logging.getLogger(__name__)

def production_review(case: Case, reviewer: str, db_path) -> Verdict:
    assessment = analyze_asset(case, db_path=db_path)
    # Resolve aliases first, so a model that writes "CSAM" still lands on the
    # Tier-1 clause below instead of falling through to the generic escalation.
    category = normalize_category(assessment.category) or "No Violation"
    clause = get_clause_for_category(category)
    case.metadata["detected_category"] = category
    _record_agent_metadata(case, assessment)
    senior_reviewed = any("senior" in name.lower() for name in getattr(assessment, "reviewer_chain", []) or [])
    if senior_reviewed:
        reviewer = "senior"

    if clause.tier == 1 or category in TIER1_CATEGORIES:
        decision = "ambiguous"
        rationale = "Tier-1 signal detected; automated decision bypassed and routed to human review."
        confidence = max(assessment.confidence, 0.95)
    elif assessment.decision == "allow":
        decision = "allow"
        rationale = assessment.rationale or "No policy violation detected in the uploaded asset."
        confidence = assessment.confidence
    elif category == "No Violation":
        # Non-allow verdict without a category means the content could not be
        # analyzed (empty evidence, runtime failure): fail closed to review.
        decision = "ambiguous"
        rationale = assessment.rationale or "Content could not be reliably analyzed; routed to escalated review."
        confidence = assessment.confidence
    elif clause.tier == 2 and not senior_reviewed:
        decision = "ambiguous"
        rationale = (
            f"Production analysis found a context-sensitive {category} signal. "
            "Senior review is required before final action."
        )
        confidence = assessment.confidence
    else:
        decision = assessment.decision if assessment.decision in {"reject", "ambiguous"} else "ambiguous"
        rationale = assessment.rationale or f"Production analysis matched {clause.citation}."
        confidence = assessment.confidence

    return build_verdict(
        case_id=case.id,
        decision=decision,
        category=category,
        confidence=confidence,
        rationale=rationale,
        reviewer=reviewer,
    )


def _record_agent_metadata(case: Case, assessment: ProductionAssessment) -> None:
    events = list(getattr(assessment, "agent_events", []) or [])
    if events:
        case.metadata.setdefault("agent_events", []).extend(events)
    citations = list(getattr(assessment, "cited_clauses", []) or [])
    if citations:
        existing = case.metadata.setdefault("cited_clauses", [])
        existing.extend(citation for citation in citations if citation not in existing)
    if getattr(assessment, "usage_total_tokens", 0):
        # Accumulate across runs: the senior pass adds to the specialist pass.
        usage = case.metadata.setdefault(
            "token_usage", {"requests": 0, "input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
        )
        usage["requests"] += int(assessment.usage_requests or 0)
        usage["input_tokens"] += int(assessment.usage_input_tokens or 0)
        usage["output_tokens"] += int(assessment.usage_output_tokens or 0)
        usage["total_tokens"] += int(assessment.usage_total_tokens or 0)
        model = str(getattr(assessment, "usage_model", "") or "unknown")
        by_model = usage.setdefault("by_model", {})
        entry = by_model.setdefault(model, {"input_tokens": 0, "output_tokens": 0})
        entry["input_tokens"] += int(assessment.usage_input_tokens or 0)
        entry["output_tokens"] += int(assessment.usage_output_tokens or 0)


def _agents_sdk_available() -> bool:
    try:
        import agents  # noqa: F401
    except ImportError:
        return False
    return True


def analyze_asset(case: Case, client: Any | None = None, db_path: Any | None = None) -> ProductionAssessment:
    """Classify an asset. Agent runtime when available; legacy single-shot classifier otherwise.

    On agent-runtime failure the system fails closed: the case comes back
    ambiguous and the orchestrator rails escalate it to review.
    """
    settings = load_settings()
    if client is None and not settings.openai_api_key_present:
        # The injection screen is deterministic regex over the upload's own
        # bytes — no model call, no credentials. Returning "unavailable" without
        # running it reported a hostile upload as an infrastructure gap rather
        # than as the manipulation attempt it was, losing the attribution the
        # audit trail depends on. Both paths still escalate to a human, so this
        # changes what the trace says, not whether the content is enforced.
        # With credentials present the SDK input guardrail performs this screen
        # inside the agent run, so the tripwire span stays in the OpenAI trace.
        screened = _screen_text_asset_for_injection(case)
        if screened is not None:
            return screened
        return _unavailable_assessment("OpenAI credentials are not configured.")

    if _agents_sdk_available() and settings.openai_api_key_present:
        from sentinel.agents.runtime import run_specialist_case

        try:
            return run_specialist_case(case, db_path=db_path, client=client)
        except Exception as exc:
            logger.exception("Agent runtime failed for case %s: %s", case.id, exc)
            return ProductionAssessment(
                decision="ambiguous",
                category="No Violation",
                confidence=0.0,
                rationale=f"Agent runtime failed ({type(exc).__name__}); failing closed to escalated review.",
                evidence_summary="Agent runtime error; no automated analysis available.",
                agent_events=[f"agent_runtime.error:{type(exc).__name__}"],
            )
    try:
        return _legacy_analyze_asset(case, client)
    except Exception as exc:
        logger.exception("Legacy production analysis failed for case %s: %s", case.id, exc)
        return _unavailable_assessment(f"Production analysis failed ({type(exc).__name__}).")


def _unavailable_assessment(reason: str) -> ProductionAssessment:
    return ProductionAssessment(
        decision="ambiguous",
        category="No Violation",
        confidence=0.0,
        rationale=f"{reason} Failing closed to escalated review.",
        evidence_summary="No automated analysis was available.",
        agent_events=["production_analysis.unavailable"],
    )


def _screen_text_asset_for_injection(case: Case) -> ProductionAssessment | None:
    """Run the credential-free injection screen over a text upload.

    Only text is screenable without a model: audio needs transcription first
    (handled in :func:`_legacy_analyze_asset`) and image/video carry no directly
    readable text. Returns ``None`` when the upload is not text or is clean, so
    the caller falls through to its normal path.
    """
    if case.asset_type != "text":
        return None
    screen = check_prompt_injection(read_text_asset(case))
    if not screen.triggered:
        return None
    return _legacy_injection_assessment(screen.matched)


def _legacy_injection_assessment(matched: str) -> ProductionAssessment:
    return ProductionAssessment(
        decision="ambiguous",
        category="No Violation",
        confidence=0.99,
        rationale=(
            "Input guardrail detected an attempt to manipulate the moderator; "
            "routed to human review without adjudication."
        ),
        evidence_summary=f"Prompt-injection screen tripped before adjudication (matched: {matched!r}).",
        agent_events=["guardrail.input.injection"],
    )


def _apply_tier1_rail(assessment: ProductionAssessment) -> ProductionAssessment:
    """Mirror the SDK Tier-1 output tripwire for the legacy classifier.

    The SDK path halts the agent via ``tier1_output_guardrail``. The legacy path
    has no agent to halt, so the equivalent invariant is enforced on the result:
    a Tier-1 category never carries an automated decision.
    """
    if assessment.category not in TIER1_CATEGORIES:
        return assessment
    return replace(
        assessment,
        decision="ambiguous",
        confidence=max(assessment.confidence, 0.95),
        rationale="Tier-1 category detected; automated adjudication bypassed and routed to human review.",
        agent_events=list(assessment.agent_events) + ["guardrail.tier1.legacy"],
    )


def _legacy_analyze_asset(case: Case, client: Any | None = None) -> ProductionAssessment:
    """Single-shot classifier used when the Agents SDK is unavailable.

    Carries the same two rails as the SDK path — prompt-injection screening
    before adjudication, Tier-1 enforcement after — so that losing the SDK
    degrades capability without degrading safety.
    """
    client = client or _openai_client()

    if case.asset_type == "audio":
        transcript = transcribe_audio_asset(case, client)
        screen = check_prompt_injection(transcript)
        if screen.triggered:
            return _legacy_injection_assessment(screen.matched)
        return _apply_tier1_rail(
            classify_text(
                f"Audio transcript for moderation:\n{transcript}",
                client=client,
                asset_type="audio",
            )
        )
    if case.asset_type == "image":
        return _apply_tier1_rail(classify_image(case, client))
    if case.asset_type == "video":
        return _apply_tier1_rail(classify_video(case, client))

    text = read_text_asset(case)
    screen = check_prompt_injection(text)
    if screen.triggered:
        return _legacy_injection_assessment(screen.matched)
    return _apply_tier1_rail(classify_text(text, client=client, asset_type="text"))


def read_text_asset(case: Case) -> str:
    path = Path(case.asset_path)
    if not path.exists():
        return ""
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        # Binary content is never a legitimate text upload. Decoding it with
        # errors="ignore" would yield non-empty mojibake that the text specialist
        # would then "adjudicate" — an allow verdict on content nobody read.
        # Returning empty routes the case to human review instead.
        logger.warning("Asset %s declared as text is not valid UTF-8; treating as unreadable", case.id)
        return ""
    return text[:MAX_TEXT_CHARS]


def transcribe_audio_asset(case: Case, client: Any) -> str:
    with open(case.asset_path, "rb") as audio_file:
        transcript = client.audio.transcriptions.create(
            model=load_settings().transcribe_model,
            file=audio_file,
            response_format="text",
        )
    if isinstance(transcript, str):
        return transcript
    return str(getattr(transcript, "text", ""))


def classify_text(text: str, client: Any, asset_type: str) -> ProductionAssessment:
    if not text.strip():
        return ProductionAssessment(
            decision="ambiguous",
            category="No Violation",
            confidence=0.0,
            rationale="No readable text or transcript could be extracted.",
            evidence_summary="Empty extracted text.",
        )
    response = client.responses.create(
        model=load_settings().production_model,
        instructions=_moderation_instructions(asset_type),
        input=f"Classify this {asset_type} upload against the policy taxonomy:\n\n{text[:MAX_TEXT_CHARS]}",
        text={"format": _assessment_format()},
    )
    return _parse_assessment(response.output_text)


def classify_image(case: Case, client: Any) -> ProductionAssessment:
    image_url = _data_url(case.asset_path, case.metadata.get("upload_content_type"))
    response = client.responses.create(
        model=load_settings().production_model,
        instructions=_moderation_instructions("image"),
        input=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": "Classify this uploaded image against the policy taxonomy.",
                    },
                    {"type": "input_image", "image_url": image_url},
                ],
            }
        ],
        text={"format": _assessment_format()},
    )
    return _parse_assessment(response.output_text)


def classify_video(case: Case, client: Any) -> ProductionAssessment:
    content: list[dict[str, str]] = [
        {
            "type": "input_text",
            "text": "Classify this uploaded video from sampled frames and any extracted transcript.",
        }
    ]
    for image_url in sample_video_frame_data_urls(case.asset_path):
        content.append({"type": "input_image", "image_url": image_url})
    transcript = extract_video_audio_transcript(case, client)
    if transcript:
        content.append({"type": "input_text", "text": f"Extracted audio transcript:\n{transcript[:MAX_TEXT_CHARS]}"})
    if len(content) == 1:
        return ProductionAssessment(
            decision="ambiguous",
            category="No Violation",
            confidence=0.0,
            rationale="No video frames or transcript could be extracted for analysis.",
            evidence_summary="Video extraction failed.",
        )
    response = client.responses.create(
        model=load_settings().production_model,
        instructions=_moderation_instructions("video"),
        input=[{"role": "user", "content": content}],
        text={"format": _assessment_format()},
    )
    return _parse_assessment(response.output_text)


def sample_video_frame_data_urls(asset_path: str, max_frames: int = MAX_VIDEO_FRAMES) -> list[str]:
    try:
        import cv2
    except ImportError:
        logger.warning("cv2 (opencv-python) not installed; video frame sampling unavailable")
        return []

    capture = cv2.VideoCapture(str(asset_path))
    if not capture.isOpened():
        return []
    frame_count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    if frame_count <= 0:
        positions = [0]
    else:
        step = max(frame_count // (max_frames + 1), 1)
        positions = [step * (index + 1) for index in range(max_frames)]
    frames: list[str] = []
    try:
        for position in positions:
            capture.set(cv2.CAP_PROP_POS_FRAMES, position)
            ok, frame = capture.read()
            if not ok:
                continue
            ok, encoded = cv2.imencode(".jpg", frame)
            if ok:
                frames.append(f"data:image/jpeg;base64,{base64.b64encode(encoded.tobytes()).decode('ascii')}")
    finally:
        capture.release()
    return frames


def extract_video_audio_transcript(case: Case, client: Any) -> str:
    temp_path: str | None = None
    try:
        try:
            from moviepy import VideoFileClip
        except ImportError:
            from moviepy.editor import VideoFileClip  # type: ignore

        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as temp_file:
            temp_path = temp_file.name
        clip = VideoFileClip(case.asset_path)
        try:
            if clip.audio is None:
                return ""
            clip.audio.write_audiofile(temp_path, logger=None)
        finally:
            clip.close()
        audio_case = Case(id=f"{case.id}-audio", asset_type="audio", asset_path=temp_path, metadata={})
        return transcribe_audio_asset(audio_case, client)
    except Exception as exc:
        logger.exception("Video audio extraction failed: %s", exc)
        return ""
    finally:
        if temp_path:
            Path(temp_path).unlink(missing_ok=True)


def _openai_client():
    """Build an OpenAI client with an explicit timeout and bounded retries.

    The SDK's own retry handling applies exponential backoff with jitter to
    connection errors and 429/5xx responses, so we do not hand-roll one. The
    timeout is the important part: the API routes are sync and run in FastAPI's
    threadpool, so an untimed request pins a worker until the socket dies.
    """
    from openai import OpenAI

    load_settings()
    return OpenAI(timeout=LLM_TIMEOUT_SECONDS, max_retries=LLM_MAX_RETRIES)


def _data_url(asset_path: str, content_type: Any | None = None) -> str:
    mime = str(content_type or "") or mimetypes.guess_type(asset_path)[0] or "application/octet-stream"
    data = Path(asset_path).read_bytes()
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def _moderation_instructions(asset_type: str) -> str:
    categories = "\n".join(
        f"- {clause.category}: Tier {clause.tier}, clause {clause.citation}, {clause.summary}"
        for clause in POLICY_CLAUSES.values()
        if clause.category != "No Violation"
    )
    return (
        "You are Sentinel's production content-moderation classifier. "
        "Classify the uploaded asset against the policy taxonomy below. "
        "Return only structured JSON matching the schema. "
        "Do not quote or reproduce user content. "
        "If a Tier-1 category may apply, return that category with decision reject, but keep rationale high-level only.\n\n"
        f"Asset type: {asset_type}\n"
        "Allowed decision values: allow, reject, ambiguous.\n"
        "Use category No Violation when no policy category applies.\n\n"
        f"Policy taxonomy:\n{categories}"
    )


def _assessment_format() -> dict[str, Any]:
    return {
        "type": "json_schema",
        "name": "sentinel_moderation_assessment",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "decision": {"type": "string", "enum": ["allow", "reject", "ambiguous"]},
                "category": {"type": "string", "enum": list(POLICY_CLAUSES.keys())},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "rationale": {"type": "string"},
                "evidence_summary": {"type": "string"},
            },
            "required": ["decision", "category", "confidence", "rationale", "evidence_summary"],
        },
    }


def _parse_assessment(output_text: str) -> ProductionAssessment:
    try:
        payload = json.loads(output_text)
    except json.JSONDecodeError:
        return ProductionAssessment(
            decision="ambiguous",
            category="No Violation",
            confidence=0.0,
            rationale="The model did not return parseable structured output.",
            evidence_summary="Unparseable model output.",
        )
    category = payload.get("category", "No Violation")
    if category not in POLICY_CLAUSES:
        category = "No Violation"
    decision = payload.get("decision", "ambiguous")
    if decision not in {"allow", "reject", "ambiguous"}:
        decision = "ambiguous"
    return ProductionAssessment(
        decision=decision,
        category=category,
        confidence=float(payload.get("confidence", 0.0)),
        rationale=str(payload.get("rationale", "")),
        evidence_summary=str(payload.get("evidence_summary", "")),
    )
