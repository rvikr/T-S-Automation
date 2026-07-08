from __future__ import annotations

from dataclasses import dataclass

try:
    from agents import function_tool
except ImportError:  # pragma: no cover - exercised when SDK is installed
    def function_tool(func):
        return func


ROBLOX_STANDARDS_URL = "https://about.roblox.com/community-standards"


@dataclass(frozen=True)
class PolicyClause:
    category: str
    pillar: str
    tier: int
    clause_id: str
    summary: str
    source_url: str = ROBLOX_STANDARDS_URL

    @property
    def citation(self) -> str:
        return f"{self.clause_id} ({self.pillar} / {self.category})"


POLICY_CLAUSES: dict[str, PolicyClause] = {
    "No Violation": PolicyClause(
        category="No Violation",
        pillar="General",
        tier=0,
        clause_id="SAFE-ALLOW-000",
        summary="Benign synthetic upload with no matched policy category.",
    ),
    "Child Exploitation": PolicyClause(
        category="Child Exploitation",
        pillar="Safety",
        tier=1,
        clause_id="SAF-CE-001",
        summary="Tier-1 child-safety stand-ins must be quarantined and routed to human review without automated adjudication.",
    ),
    "Terrorism & Violent Extremism": PolicyClause(
        category="Terrorism & Violent Extremism",
        pillar="Safety",
        tier=1,
        clause_id="SAF-TVE-001",
        summary="Tier-1 violent-extremism stand-ins must be quarantined and routed to human review without automated adjudication.",
    ),
    "Suicide/Self-Injury": PolicyClause(
        category="Suicide/Self-Injury",
        pillar="Safety",
        tier=2,
        clause_id="SAF-SSI-001",
        summary="Self-harm references require cautious review and escalation when context is unclear.",
    ),
    "Harassment & Discrimination": PolicyClause(
        category="Harassment & Discrimination",
        pillar="Safety",
        tier=2,
        clause_id="SAF-HAR-001",
        summary="Abusive or identity-targeted conduct is disallowed; ambiguous social context is senior-review eligible.",
    ),
    "Harmful Off-Platform Behavior": PolicyClause(
        category="Harmful Off-Platform Behavior",
        pillar="Safety",
        tier=2,
        clause_id="SAF-OFF-001",
        summary="Attempts to move users to unsafe off-platform conduct should be reviewed strictly.",
    ),
    "Violent Content & Gore": PolicyClause(
        category="Violent Content & Gore",
        pillar="Civility",
        tier=3,
        clause_id="CIV-VCG-001",
        summary="Graphic violence or gore is a clear content violation suitable for auto-reject when synthetic labels are unambiguous.",
    ),
    "Romantic/Sexual Content": PolicyClause(
        category="Romantic/Sexual Content",
        pillar="Civility",
        tier=3,
        clause_id="CIV-RSX-001",
        summary="Sexualized or romanticized content that violates platform age-appropriate boundaries is rejected.",
    ),
    "Illegal & Regulated Goods": PolicyClause(
        category="Illegal & Regulated Goods",
        pillar="Civility",
        tier=3,
        clause_id="CIV-IRG-001",
        summary="Promotion or facilitation of illegal or regulated goods is rejected.",
    ),
    "Profanity": PolicyClause(
        category="Profanity",
        pillar="Civility",
        tier=3,
        clause_id="CIV-PRO-001",
        summary="Explicit profanity in user-facing uploads can be rejected when the label is clear.",
    ),
    "Sensitive Events": PolicyClause(
        category="Sensitive Events",
        pillar="Civility",
        tier=2,
        clause_id="CIV-SEN-001",
        summary="References to sensitive events require context-aware review before final action.",
    ),
    "Political Content": PolicyClause(
        category="Political Content",
        pillar="Civility",
        tier=2,
        clause_id="CIV-POL-001",
        summary="Political persuasion or campaigning content is context-sensitive and may require senior review.",
    ),
    "Cheating & Scams": PolicyClause(
        category="Cheating & Scams",
        pillar="Integrity",
        tier=3,
        clause_id="INT-SCAM-001",
        summary="Scams, cheating, or deceptive claims are rejected when synthetic labels are clear.",
    ),
    "Spam": PolicyClause(
        category="Spam",
        pillar="Integrity",
        tier=3,
        clause_id="INT-SPAM-001",
        summary="Repeated low-value promotional or disruptive content is rejected.",
    ),
    "IP Violations": PolicyClause(
        category="IP Violations",
        pillar="Integrity",
        tier=3,
        clause_id="INT-IP-001",
        summary="Uploads labeled as unauthorized use of protected IP are rejected.",
    ),
    "Advertising": PolicyClause(
        category="Advertising",
        pillar="Integrity",
        tier=3,
        clause_id="INT-ADV-001",
        summary="Unapproved advertising or commercial solicitation is rejected.",
    ),
    "Sharing Personal Info": PolicyClause(
        category="Sharing Personal Info",
        pillar="Security",
        tier=3,
        clause_id="SEC-PII-001",
        summary="Sharing personal information is rejected to protect user privacy.",
    ),
    "Off-Platform Directing": PolicyClause(
        category="Off-Platform Directing",
        pillar="Security",
        tier=2,
        clause_id="SEC-OPD-001",
        summary="Attempts to direct users off-platform are escalated when intent or safety is unclear.",
    ),
    "System Misuse": PolicyClause(
        category="System Misuse",
        pillar="Security",
        tier=3,
        clause_id="SEC-MIS-001",
        summary="Abuse of platform systems, automation, or security boundaries is rejected.",
    ),
}

TIER1_CATEGORIES = {"Child Exploitation", "Terrorism & Violent Extremism"}


def get_clause_for_category(category: str) -> PolicyClause:
    return POLICY_CLAUSES.get(category, POLICY_CLAUSES["No Violation"])


def retrieve_policy(query: str, limit: int = 3) -> list[dict[str, str | int]]:
    query_lower = query.lower()
    ranked: list[PolicyClause] = []
    for clause in POLICY_CLAUSES.values():
        haystack = f"{clause.category} {clause.pillar} {clause.summary}".lower()
        if clause.category.lower() in query_lower or any(token in haystack for token in query_lower.split()):
            ranked.append(clause)
    if not ranked:
        ranked = [POLICY_CLAUSES["No Violation"]]
    return [
        {
            "category": clause.category,
            "pillar": clause.pillar,
            "severity_tier": clause.tier,
            "clause_id": clause.clause_id,
            "summary": clause.summary,
            "source_url": clause.source_url,
        }
        for clause in ranked[:limit]
    ]


@function_tool
def retrieve_policy_tool(query: str) -> str:
    """Retrieve the community-guideline clauses most relevant to a content signal.

    Args:
        query: Natural-language description of the observed content signal
            (e.g. "user telling another player to hurt themselves").
    """
    clauses = retrieve_policy(query)
    return "\n".join(
        f"{clause['clause_id']} [Tier {clause['severity_tier']}] {clause['pillar']} / {clause['category']}: "
        f"{clause['summary']}"
        for clause in clauses
    )
