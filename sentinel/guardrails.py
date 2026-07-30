"""Sentinel safety guardrails.

Two SDK tripwires are defined here:

* :func:`injection_input_guardrail` — screens every agent input for
  prompt-injection attempts (regex-based, zero latency, offline-testable).
  Runs *before* the agent starts and routes hostile uploads straight to a
  human ticket without adjudication.

* :func:`tier1_output_guardrail` — halts any agent whose final output
  lands in a Tier-1 category (child exploitation, terrorism/violent
  extremism). The orchestrator then enforces quarantine + human ticket.

Design note: these guardrails are intentionally *not* LLM-based. They are
deterministic, cost-free, and hermetically testable — the agents adjudicate
content while the rails guard the agents.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any

try:
    from agents import GuardrailFunctionOutput, input_guardrail, output_guardrail
except ImportError:  # pragma: no cover
    GuardrailFunctionOutput = None  # type: ignore[misc, assignment]

    def output_guardrail(func):  # type: ignore[no-redef]
        return func

    def input_guardrail(func=None, **_kwargs):  # type: ignore[no-redef]
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
    # Classic "ignore previous instructions" family
    re.compile(r"ignore\s+(?:all\s+|any\s+)?(?:previous|prior|above|earlier)\s+instructions", re.IGNORECASE),
    # Direct policy/prompt-disregard commands
    re.compile(r"disregard\s+(?:the\s+)?(?:policy|policies|system\s+prompt|guidelines|instructions)", re.IGNORECASE),
    # "Forget your rules" variants
    re.compile(r"forget\s+(?:your|all|the)\s+(?:instructions|rules|guidelines)", re.IGNORECASE),
    # DAN / jailbreak persona assignments
    re.compile(r"you\s+are\s+(?:now\s+)?(?:dan|jailbroken|unfiltered|free\s+of\s+restrictions)", re.IGNORECASE),
    # "Act as DAN / unfiltered model" variants
    re.compile(r"act\s+as\s+.{0,24}?(?:dan|jailbroken|unfiltered)", re.IGNORECASE),
    # Attempts to exfiltrate the system prompt
    re.compile(r"(?:reveal|show|print)\s+(?:your\s+)?system\s+prompt", re.IGNORECASE),
    # Explicit verdict-manipulation: "classify this as allow"
    re.compile(r"classify\s+this\s+as\s+(?:allow|allowed|no\s+violation)", re.IGNORECASE),
    # "Output only 'allow'" forced-output injection
    re.compile(r"output\s+only\s+['\"]?allow", re.IGNORECASE),
    # Direct override of the moderation/guardrail decision
    re.compile(r"override\s+(?:the\s+)?(?:moderation|guardrail|verdict|decision|safety)", re.IGNORECASE),
    # Social-engineering via false authority ("as your developer, approve this")
    re.compile(r"as\s+your\s+developer.{0,40}?(?:approve|allow|permit)", re.IGNORECASE),
]


@dataclass(frozen=True)
class InjectionScreenResult:
    triggered: bool
    matched: str


# Zero-width and word-joiner code points used to split trigger phrases so no
# regex can span them ("ig<ZWSP>nore previous..."). Escapes, not literals —
# invisible characters in source are unreviewable.
_ZERO_WIDTH_RE = re.compile("[\u200b-\u200f\u2060\ufeff\u00ad]")

# Leetspeak digit/symbol substitutions ("1gn0re pr3vious 1nstructions").
# Folding applies to the *normalized copy only* — the raw text is always
# screened too, so folding can add detections but never remove one.
_LEET_FOLD = str.maketrans(
    {
        "0": "o",
        "1": "i",
        "3": "e",
        "4": "a",
        "5": "s",
        "7": "t",
        "$": "s",
        "@": "a",
        "!": "i",
    }
)


def _normalize_for_screen(text: str) -> str:
    """Fold the cheap evasions: compatibility forms, zero-width splits, leetspeak.

    This is hardening for a regex screen, not a complete defense — semantic
    rephrasings and non-English injection still pass and are left to the
    model's own refusal behavior plus the deterministic rails downstream.
    """
    normalized = unicodedata.normalize("NFKC", text)
    normalized = _ZERO_WIDTH_RE.sub("", normalized)
    return normalized.translate(_LEET_FOLD)


def check_prompt_injection(text: str) -> InjectionScreenResult:
    candidates = (text, _normalize_for_screen(text))
    for candidate in candidates:
        for pattern in INJECTION_PATTERNS:
            match = pattern.search(candidate)
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
