from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

try:
    from agents import GuardrailFunctionOutput, input_guardrail, output_guardrail
except ImportError:  # pragma: no cover
    GuardrailFunctionOutput = None

    def output_guardrail(func):
        return func

    def input_guardrail(func=None, **_kwargs):
        if func is None:
            return lambda f: f
        return func

from sentinel.models import Verdict
from sentinel.tools.policy_retrieval import TIER1_CATEGORIES


@dataclass(frozen=True)
class Tier1GuardrailResult:
    triggered: bool
    reason: str


def check_tier1_guardrail(verdict: Verdict) -> Tier1GuardrailResult:
    if verdict.severity_tier == 1 or verdict.category in TIER1_CATEGORIES:
        return Tier1GuardrailResult(
            triggered=True,
            reason="Tier-1 synthetic stand-in requires hash-match, quarantine, and human review.",
        )
    return Tier1GuardrailResult(triggered=False, reason="")


# Deterministic screen for content that attacks the moderator itself.
# Deliberately not an LLM check: zero latency/cost, offline-testable, and the
# agents adjudicate content while the rails guard the agents.
INJECTION_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"ignore\s+(?:all\s+|any\s+)?(?:previous|prior|above|earlier)\s+instructions", re.IGNORECASE),
    re.compile(r"disregard\s+(?:the\s+)?(?:policy|policies|system\s+prompt|guidelines|instructions)", re.IGNORECASE),
    re.compile(r"forget\s+(?:your|all|the)\s+(?:instructions|rules|guidelines)", re.IGNORECASE),
    re.compile(r"you\s+are\s+(?:now\s+)?(?:dan|jailbroken|unfiltered|free\s+of\s+restrictions)", re.IGNORECASE),
    re.compile(r"act\s+as\s+.{0,24}?(?:dan|jailbroken|unfiltered)", re.IGNORECASE),
    re.compile(r"(?:reveal|show|print)\s+(?:your\s+)?system\s+prompt", re.IGNORECASE),
    re.compile(r"classify\s+this\s+as\s+(?:allow|allowed|no\s+violation)", re.IGNORECASE),
    re.compile(r"output\s+only\s+['\"]?allow", re.IGNORECASE),
    re.compile(r"override\s+(?:the\s+)?(?:moderation|guardrail|verdict|decision|safety)", re.IGNORECASE),
    re.compile(r"as\s+your\s+developer.{0,40}?(?:approve|allow|permit)", re.IGNORECASE),
]


@dataclass(frozen=True)
class InjectionScreenResult:
    triggered: bool
    matched: str


def check_prompt_injection(text: str) -> InjectionScreenResult:
    for pattern in INJECTION_PATTERNS:
        match = pattern.search(text)
        if match:
            return InjectionScreenResult(triggered=True, matched=match.group(0))
    return InjectionScreenResult(triggered=False, matched="")


def _flatten_input_text(agent_input: Any) -> str:
    if isinstance(agent_input, str):
        return agent_input
    chunks: list[str] = []
    for item in agent_input or []:
        content = item.get("content") if isinstance(item, dict) else None
        if isinstance(content, str):
            chunks.append(content)
            continue
        for part in content or []:
            if isinstance(part, dict) and part.get("type") == "input_text":
                chunks.append(str(part.get("text", "")))
    return "\n".join(chunks)


@input_guardrail(run_in_parallel=False)
async def injection_input_guardrail(ctx, agent, agent_input):
    """SDK tripwire: screen uploads for attempts to manipulate the moderator.

    Runs before the agent starts (run_in_parallel=False), so a hostile upload
    never reaches adjudication.
    """
    screen = check_prompt_injection(_flatten_input_text(agent_input))
    info = {"check": "prompt_injection", "matched": screen.matched}
    if GuardrailFunctionOutput is None:
        return {"tripwire_triggered": screen.triggered, "output_info": info}
    return GuardrailFunctionOutput(output_info=info, tripwire_triggered=screen.triggered)


@output_guardrail
async def tier1_output_guardrail(ctx, agent, output):
    """SDK tripwire: halt any agent whose final output lands in a Tier-1 category.

    Duck-typed so it accepts both runtime AssessmentOutput objects and Verdicts.
    """
    category = str(getattr(output, "category", "") or "")
    severity_tier = getattr(output, "severity_tier", None)
    triggered = category in TIER1_CATEGORIES or severity_tier == 1
    info = {"check": "tier1", "category": category}
    if GuardrailFunctionOutput is None:
        return {"tripwire_triggered": triggered, "output_info": info}
    return GuardrailFunctionOutput(output_info=info, tripwire_triggered=triggered)
