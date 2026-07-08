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
