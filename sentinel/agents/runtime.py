"""OpenAI Agents SDK runtime: the live moderation agents.

This module hard-requires the SDK; callers gate entry via
``sentinel.tools.production_analysis.analyze_asset``, which falls back to the
legacy classifier when the SDK or API key is unavailable.

Design: judgment is agentic (tool-calling loop, LLM handoff to the senior
reviewer, SDK Tier-1 tripwire); policy invariants stay deterministic in
``orchestrator.run_case`` (Tier-1 quarantine/ticketing, guaranteed senior
review of ambiguous verdicts). Agents deliberately have no ticketing tool —
the AI can neither create nor skip an escalation.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from agents import Agent, ModelSettings, RunHooks, Runner
from agents.exceptions import InputGuardrailTripwireTriggered, OutputGuardrailTripwireTriggered

from sentinel.agents import live_events
from sentinel.config import Settings, load_settings
from sentinel.guardrails import injection_input_guardrail, tier1_output_guardrail
from sentinel.models import EVIDENCE_CACHE_KEY, Case, ProductionAssessment, Verdict
from sentinel.tools.hash_match import hash_match_tool
from sentinel.tools.policy_retrieval import POLICY_CLAUSES, TIER1_CATEGORIES, get_clause_for_category, retrieve_policy_tool
from sentinel.tools.precedent_memory import retrieve_precedents_tool


MAX_TEXT_CHARS = 12000
MAX_AGENT_TURNS = 10


@dataclass
class ModerationContext:
    """Run-local context handed to function tools via RunContextWrapper."""

    case: Case
    db_path: str | None = None


class AssessmentOutput(BaseModel):
    """Structured verdict every moderation agent must produce."""

    decision: Literal["allow", "reject", "ambiguous"]
    category: str = Field(description="Exact policy category name from the taxonomy, or 'No Violation'.")
    confidence: float = Field(description="Calibrated confidence between 0 and 1.")
    rationale: str = Field(description="Concise policy-grounded rationale. Never quote or reproduce user content.")
    evidence_summary: str = Field(description="High-level description of what was observed, without reproducing it.")
    cited_clauses: list[str] = Field(description="Policy clause ids cited, e.g. SAF-HAR-001. Empty only for clean allows.")


def runtime_available(settings: Settings | None = None) -> bool:
    settings = settings or load_settings()
    return settings.openai_api_key_present


class _LiveRunHooks(RunHooks):
    """Mirror run lifecycle events into the live UI sink.

    run_sync drives its event loop in the calling thread, so these fire
    synchronously in the Streamlit script thread — mid-run rendering with no
    async rewrite.
    """

    async def on_agent_start(self, context, agent) -> None:
        live_events.emit(f"live:agent_start:{agent.name}")

    async def on_llm_start(self, context, agent, system_prompt, input_items) -> None:
        live_events.emit(f"live:thinking:{agent.name}")

    async def on_tool_start(self, context, agent, tool) -> None:
        live_events.emit(f"live:tool_call:{agent.name}:{getattr(tool, 'name', 'tool')}")

    async def on_tool_end(self, context, agent, tool, result) -> None:
        live_events.emit(f"live:tool_done:{agent.name}:{getattr(tool, 'name', 'tool')}")

    async def on_handoff(self, context, from_agent, to_agent) -> None:
        live_events.emit(f"live:handoff:{from_agent.name}->{to_agent.name}")

    async def on_agent_end(self, context, agent, output) -> None:
        live_events.emit(f"live:agent_end:{agent.name}")


def _run_hooks() -> RunHooks | None:
    return _LiveRunHooks() if live_events.sink_active() else None


def _taxonomy_block() -> str:
    return "\n".join(
        f"- {clause.category} (Tier {clause.tier}, clause {clause.clause_id}): {clause.summary}"
        for clause in POLICY_CLAUSES.values()
        if clause.category != "No Violation"
    )


_SHARED_RULES = (
    "Hard rules:\n"
    "1. Tier-1 (Child Exploitation; Terrorism & Violent Extremism): if the content may fall in either "
    "category, stop all analysis immediately and output that category with decision 'ambiguous', a "
    "high-level rationale only, and no content detail. A safety guardrail halts processing and routes "
    "the case to human review; you must never adjudicate Tier-1 content.\n"
    "2. Never quote, transcribe back, or otherwise reproduce user content in your output. Describe it "
    "at the level of policy signals only.\n"
    "3. Ground every verdict in the policy corpus: call retrieve_policy_tool with a description of the "
    "observed signal before deciding, and put the clause ids you relied on in cited_clauses.\n"
    "4. Before deciding a non-obvious case, call retrieve_precedents_tool with your candidate category "
    "to check how senior or human reviewers resolved similar cases. Call it at most once, for your "
    "single most likely category — do not sweep multiple categories.\n"
    "5. Use category 'No Violation' with decision 'allow' when no policy category applies.\n\n"
    "Decision guide:\n"
    "- Tier-3 categories: reject when the violation is clear.\n"
    "- Tier-2 categories are context-sensitive: decide only with strong contextual evidence; otherwise "
    "escalate.\n"
)


def _specialist_instructions(asset_type: str) -> str:
    return (
        f"You are Sentinel's {asset_type} moderation specialist, the first automated reviewer for "
        f"uploaded {asset_type} content on a platform with a young audience.\n\n"
        f"{_SHARED_RULES}"
        "- If you cannot reach a confident decision (confidence below 0.75) or the case hinges on "
        "social context you cannot verify, hand off to the Senior Reviewer instead of guessing.\n"
        "- For media assets, you may call hash_match_tool to check the known-violation hash list.\n\n"
        f"Policy taxonomy:\n{_taxonomy_block()}"
    )


def _senior_instructions() -> str:
    return (
        "You are Sentinel's Senior Policy Reviewer — the stricter, final automated reviewer. Cases "
        "reach you only when a specialist could not decide.\n\n"
        f"{_SHARED_RULES}"
        "- You must resolve the case to 'allow' or 'reject' whenever the policy corpus gives grounds "
        "for a decision; do not defer for mere discomfort.\n"
        "- Output 'ambiguous' only when human judgment is genuinely required: credible real-world harm "
        "signals, potential legal exposure, or context no automated system can verify. That escalates "
        "to a human review ticket.\n"
        "- Your resolution is stored as precedent and will guide future cases, so keep the rationale "
        "precise and clause-grounded.\n\n"
        f"Policy taxonomy:\n{_taxonomy_block()}"
    )


def build_senior_agent(settings: Settings) -> Agent[ModerationContext]:
    return Agent[ModerationContext](
        name="Senior Reviewer",
        handoff_description=(
            "Stricter escalation reviewer. Hand off ambiguous or context-sensitive cases you cannot "
            "decide confidently."
        ),
        instructions=_senior_instructions(),
        tools=[retrieve_policy_tool, retrieve_precedents_tool, hash_match_tool],
        model=settings.senior_model,
        model_settings=ModelSettings(temperature=0.1),
        output_type=AssessmentOutput,
        input_guardrails=[injection_input_guardrail],
        output_guardrails=[tier1_output_guardrail],
    )


def build_specialist_agent(asset_type: str, senior: Agent[ModerationContext], settings: Settings) -> Agent[ModerationContext]:
    return Agent[ModerationContext](
        name=f"{asset_type.title()} Specialist",
        instructions=_specialist_instructions(asset_type),
        tools=[retrieve_policy_tool, retrieve_precedents_tool, hash_match_tool],
        handoffs=[senior],
        model=settings.specialist_model,
        model_settings=ModelSettings(temperature=0.1),
        output_type=AssessmentOutput,
        input_guardrails=[injection_input_guardrail],
        output_guardrails=[tier1_output_guardrail],
    )


def _prepare_input(case: Case, client: Any) -> tuple[list[dict[str, Any]] | None, list[str]]:
    """Build Responses-API input items from the asset; returns (input_items, evidence_events)."""
    from sentinel.tools import production_analysis as pa

    events: list[str] = []
    content: list[dict[str, Any]] = []
    if case.asset_type == "image":
        content = [
            {"type": "input_text", "text": "Moderate this uploaded image against the policy taxonomy."},
            {"type": "input_image", "image_url": pa._data_url(case.asset_path, case.metadata.get("upload_content_type"))},
        ]
        events.append("evidence:image")
    elif case.asset_type == "audio":
        transcript = pa.transcribe_audio_asset(case, client)
        events.append(f"evidence:audio-transcript:{len(transcript)}chars")
        if not transcript.strip():
            return None, events
        content = [
            {
                "type": "input_text",
                "text": f"Moderate this audio upload. Transcript:\n{transcript[:MAX_TEXT_CHARS]}",
            }
        ]
    elif case.asset_type == "video":
        frames = pa.sample_video_frame_data_urls(case.asset_path)
        transcript = pa.extract_video_audio_transcript(case, client)
        events.append(f"evidence:video-frames:{len(frames)}")
        if transcript:
            events.append(f"evidence:video-transcript:{len(transcript)}chars")
        if not frames and not transcript:
            return None, events
        content = [
            {
                "type": "input_text",
                "text": "Moderate this uploaded video using the sampled frames and any extracted transcript.",
            }
        ]
        content.extend({"type": "input_image", "image_url": frame} for frame in frames)
        if transcript:
            content.append(
                {"type": "input_text", "text": f"Extracted audio transcript:\n{transcript[:MAX_TEXT_CHARS]}"}
            )
    else:
        text = pa.read_text_asset(case)
        events.append(f"evidence:text:{len(text)}chars")
        if not text.strip():
            return None, events
        content = [{"type": "input_text", "text": f"Moderate this text upload:\n\n{text[:MAX_TEXT_CHARS]}"}]
    return [{"role": "user", "content": content}], events


def _get_or_prepare_input(case: Case, client: Any) -> tuple[list[dict[str, Any]] | None, list[str]]:
    cached = case.metadata.get(EVIDENCE_CACHE_KEY)
    if cached is not None:
        return cached, []
    input_items, events = _prepare_input(case, client)
    if input_items is not None:
        case.metadata[EVIDENCE_CACHE_KEY] = input_items
    return input_items, events


def _no_evidence_assessment(case: Case, events: list[str]) -> ProductionAssessment:
    return ProductionAssessment(
        decision="ambiguous",
        category="No Violation",
        confidence=0.0,
        rationale=f"No reviewable content could be extracted from the {case.asset_type} asset.",
        evidence_summary="Evidence extraction produced nothing to review.",
        agent_events=events + ["evidence:empty"],
    )


def _extract_events(result: Any) -> list[str]:
    events: list[str] = []
    for item in getattr(result, "new_items", []) or []:
        item_type = getattr(item, "type", "")
        agent_name = getattr(getattr(item, "agent", None), "name", "agent")
        if item_type == "tool_call_item":
            tool_name = getattr(getattr(item, "raw_item", None), "name", "tool")
            events.append(f"tool_call:{agent_name}:{tool_name}")
        elif item_type == "handoff_output_item":
            source = getattr(getattr(item, "source_agent", None), "name", agent_name)
            target = getattr(getattr(item, "target_agent", None), "name", "agent")
            events.append(f"handoff:{source}->{target}")
        elif item_type == "message_output_item":
            events.append(f"verdict_drafted:{agent_name}")
    return events


def _reviewer_chain(result: Any) -> list[str]:
    chain: list[str] = []
    for item in getattr(result, "new_items", []) or []:
        name = getattr(getattr(item, "agent", None), "name", None)
        if name and (not chain or chain[-1] != name):
            chain.append(name)
    last = getattr(getattr(result, "last_agent", None), "name", None)
    if last and (not chain or chain[-1] != last):
        chain.append(last)
    return chain


def _injection_tripwire_assessment(events: list[str]) -> ProductionAssessment:
    return ProductionAssessment(
        decision="ambiguous",
        category="No Violation",
        confidence=0.99,
        rationale=(
            "Input guardrail detected an attempt to manipulate the moderator; "
            "routed to human review without adjudication."
        ),
        evidence_summary="Prompt-injection screen tripped before adjudication.",
        reviewer_chain=["guardrail"],
        agent_events=events + ["guardrail.input.injection"],
    )


def _tier1_tripwire_assessment(exc: OutputGuardrailTripwireTriggered, events: list[str]) -> ProductionAssessment:
    info = getattr(getattr(getattr(exc, "guardrail_result", None), "output", None), "output_info", None) or {}
    category = str(info.get("category", "")) if isinstance(info, dict) else ""
    if category not in TIER1_CATEGORIES:
        category = sorted(TIER1_CATEGORIES)[0]
    clause = get_clause_for_category(category)
    return ProductionAssessment(
        decision="ambiguous",
        category=category,
        confidence=0.99,
        rationale="Tier-1 guardrail tripwire halted the agent; automated adjudication is bypassed.",
        evidence_summary="Guardrail halt; no automated analysis retained.",
        reviewer_chain=["guardrail"],
        agent_events=events + [f"guardrail.tier1.tripwire:{clause.clause_id}"],
        cited_clauses=[clause.clause_id],
    )


def _usage_fields(result: Any) -> dict[str, int]:
    usage = getattr(getattr(result, "context_wrapper", None), "usage", None)
    if usage is None:
        return {}
    return {
        "usage_requests": int(getattr(usage, "requests", 0) or 0),
        "usage_input_tokens": int(getattr(usage, "input_tokens", 0) or 0),
        "usage_output_tokens": int(getattr(usage, "output_tokens", 0) or 0),
        "usage_total_tokens": int(getattr(usage, "total_tokens", 0) or 0),
    }


def _assessment_from_output(
    output: AssessmentOutput, chain: list[str], events: list[str], usage: dict[str, int] | None = None
) -> ProductionAssessment:
    category = output.category if output.category in POLICY_CLAUSES else "No Violation"
    return ProductionAssessment(
        decision=output.decision,
        category=category,
        confidence=max(0.0, min(float(output.confidence), 1.0)),
        rationale=output.rationale,
        evidence_summary=output.evidence_summary,
        reviewer_chain=chain,
        agent_events=events,
        cited_clauses=list(output.cited_clauses),
        **(usage or {}),
    )


def run_specialist_case(case: Case, db_path: str | Path | None = None, client: Any | None = None) -> ProductionAssessment:
    """Run the modality specialist agent (with senior handoff and Tier-1 tripwire) on a case."""
    from sentinel.tools.production_analysis import _openai_client

    settings = load_settings()
    client = client or _openai_client()
    input_items, events = _get_or_prepare_input(case, client)
    if input_items is None:
        return _no_evidence_assessment(case, events)

    context = ModerationContext(case=case, db_path=str(db_path) if db_path else None)
    senior = build_senior_agent(settings)
    asset_type = case.asset_type if case.asset_type in {"text", "image", "audio", "video"} else "text"
    specialist = build_specialist_agent(asset_type, senior, settings)
    events.append(f"agent_run:{specialist.name}:{settings.specialist_model}")
    try:
        result = Runner.run_sync(specialist, input_items, context=context, max_turns=MAX_AGENT_TURNS, hooks=_run_hooks())
    except InputGuardrailTripwireTriggered:
        return _injection_tripwire_assessment(events)
    except OutputGuardrailTripwireTriggered as exc:
        return _tier1_tripwire_assessment(exc, events + ["usage:unavailable:guardrail-halt"])
    events.extend(_extract_events(result))
    usage = {**_usage_fields(result), "usage_model": settings.specialist_model}
    return _assessment_from_output(result.final_output, _reviewer_chain(result), events, usage)


def run_senior_case(case: Case, initial_verdict: Verdict, db_path: str | Path | None = None) -> ProductionAssessment:
    """Run the senior reviewer agent on a case a specialist marked ambiguous."""
    from sentinel.tools.production_analysis import _openai_client

    settings = load_settings()
    client = _openai_client()
    input_items, events = _get_or_prepare_input(case, client)
    if input_items is None:
        return _no_evidence_assessment(case, events)

    escalation_note = {
        "role": "user",
        "content": [
            {
                "type": "input_text",
                "text": (
                    f"This case was escalated by the {initial_verdict.reviewer} reviewer with an "
                    f"ambiguous verdict.\n"
                    f"Initial category: {initial_verdict.category} ({initial_verdict.policy_clause}), "
                    f"confidence {initial_verdict.confidence:.2f}.\n"
                    f"Initial rationale: {initial_verdict.rationale}\n"
                    "Re-adjudicate strictly and produce a final decision."
                ),
            }
        ],
    }
    context = ModerationContext(case=case, db_path=str(db_path) if db_path else None)
    senior = build_senior_agent(settings)
    events.append(f"agent_run:{senior.name}:{settings.senior_model}")
    try:
        result = Runner.run_sync(senior, input_items + [escalation_note], context=context, max_turns=MAX_AGENT_TURNS, hooks=_run_hooks())
    except InputGuardrailTripwireTriggered:
        return _injection_tripwire_assessment(events)
    except OutputGuardrailTripwireTriggered as exc:
        return _tier1_tripwire_assessment(exc, events + ["usage:unavailable:guardrail-halt"])
    events.extend(_extract_events(result))
    usage = {**_usage_fields(result), "usage_model": settings.senior_model}
    return _assessment_from_output(result.final_output, _reviewer_chain(result), events, usage)
