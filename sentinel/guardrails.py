from __future__ import annotations

from dataclasses import dataclass

try:
    from agents import GuardrailFunctionOutput, output_guardrail
except ImportError:  # pragma: no cover
    GuardrailFunctionOutput = None

    def output_guardrail(func):
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


@output_guardrail
async def tier1_output_guardrail(ctx, agent, output):  # pragma: no cover - SDK integration hook
    verdict = output if isinstance(output, Verdict) else None
    triggered = bool(verdict and check_tier1_guardrail(verdict).triggered)
    if GuardrailFunctionOutput is None:
        return {"tripwire_triggered": triggered, "output_info": "tier1-check"}
    return GuardrailFunctionOutput(output_info="tier1-check", tripwire_triggered=triggered)
