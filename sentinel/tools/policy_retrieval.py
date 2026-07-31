from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    from agents import function_tool
except ImportError:  # pragma: no cover - exercised when SDK is installed
    def function_tool(func):  # type: ignore[no-redef]
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


# The built-in reference taxonomy (modelled on a Roblox-style community
# standard). A deployment brings its own policy by pointing SENTINEL_POLICY_FILE
# at a YAML/JSON file — see sentinel/policy/policy.example.yaml.
_DEFAULT_POLICY_CLAUSES: dict[str, PolicyClause] = {
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

# Models do not reliably reproduce taxonomy labels verbatim. An unrecognised
# label previously collapsed to "No Violation" (tier 0), which meant a near-miss
# on the *string* silently disarmed the Tier-1 rail for the most dangerous
# categories in the taxonomy. Map the plausible aliases back onto the canonical
# label; anything still unrecognised is handled by failing closed at the call
# site rather than being treated as benign.
_DEFAULT_CATEGORY_ALIASES: dict[str, str] = {
    "csam": "Child Exploitation",
    "csae": "Child Exploitation",
    "csem": "Child Exploitation",
    "child sexual abuse material": "Child Exploitation",
    "child sexual abuse": "Child Exploitation",
    "child sexual exploitation": "Child Exploitation",
    "child sexual exploitation material": "Child Exploitation",
    "child exploitation material": "Child Exploitation",
    "child safety": "Child Exploitation",
    "child abuse": "Child Exploitation",
    "child endangerment": "Child Exploitation",
    "grooming": "Child Exploitation",
    "child grooming": "Child Exploitation",
    "minor safety": "Child Exploitation",
    "minor sexualization": "Child Exploitation",
    "sexualization of minors": "Child Exploitation",
    "terrorism": "Terrorism & Violent Extremism",
    "violent extremism": "Terrorism & Violent Extremism",
    "terrorism and violent extremism": "Terrorism & Violent Extremism",
    "terrorism/violent extremism": "Terrorism & Violent Extremism",
    "terrorist content": "Terrorism & Violent Extremism",
    "terrorist propaganda": "Terrorism & Violent Extremism",
    "violent extremist content": "Terrorism & Violent Extremism",
    "glorification of terrorism": "Terrorism & Violent Extremism",
    "extremism": "Terrorism & Violent Extremism",
}


# ---------------------------------------------------------------------------
# Bring-your-own-policy: SENTINEL_POLICY_FILE
# ---------------------------------------------------------------------------

POLICY_FILE_ENV = "SENTINEL_POLICY_FILE"

_REQUIRED_CLAUSE_FIELDS = ("category", "pillar", "tier", "clause_id", "summary")


def build_taxonomy(payload: Any) -> tuple[dict[str, PolicyClause], dict[str, str]]:
    """Validate a parsed policy file and construct (clauses, aliases).

    Fails loudly on any structural problem: enforcing content policy under a
    taxonomy *other than the one the operator configured* is worse than
    refusing to start. A ``"No Violation"`` tier-0 entry is required by the
    verdict pipeline, so one is injected if the file omits it.
    """
    if not isinstance(payload, dict) or not isinstance(payload.get("clauses"), list):
        raise ValueError("Policy file must be a mapping with a 'clauses' list")

    clauses: dict[str, PolicyClause] = {}
    for index, item in enumerate(payload["clauses"]):
        if not isinstance(item, dict):
            raise ValueError(f"Policy clause #{index} must be a mapping")
        missing = [field for field in _REQUIRED_CLAUSE_FIELDS if field not in item]
        if missing:
            raise ValueError(f"Policy clause #{index} is missing fields: {', '.join(missing)}")
        category = str(item["category"]).strip()
        if not category:
            raise ValueError(f"Policy clause #{index} has an empty category")
        if category in clauses:
            raise ValueError(f"Duplicate policy category: {category!r}")
        try:
            tier = int(item["tier"])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Policy clause {category!r} has a non-integer tier: {item['tier']!r}") from exc
        if not 0 <= tier <= 3:
            raise ValueError(f"Policy clause {category!r} tier must be 0-3, got {tier}")
        clauses[category] = PolicyClause(
            category=category,
            pillar=str(item["pillar"]).strip(),
            tier=tier,
            clause_id=str(item["clause_id"]).strip(),
            summary=str(item["summary"]).strip(),
            source_url=str(item.get("source_url", "") or ""),
        )

    if "No Violation" not in clauses:
        clauses["No Violation"] = _DEFAULT_POLICY_CLAUSES["No Violation"]
    elif clauses["No Violation"].tier != 0:
        raise ValueError("The 'No Violation' clause must be tier 0")

    aliases_raw = payload.get("aliases") or {}
    if not isinstance(aliases_raw, dict):
        raise ValueError("Policy file 'aliases' must be a mapping of alias -> category")
    aliases: dict[str, str] = {}
    for alias, target in aliases_raw.items():
        target_name = str(target).strip()
        if target_name not in clauses:
            raise ValueError(f"Alias {alias!r} points at unknown category {target_name!r}")
        aliases[str(alias).strip().lower()] = target_name
    return clauses, aliases


def load_policy_file(path: str | Path) -> tuple[dict[str, PolicyClause], dict[str, str]]:
    """Parse a YAML or JSON policy file into (clauses, aliases)."""
    file_path = Path(path)
    text = file_path.read_text(encoding="utf-8")
    if file_path.suffix.lower() in {".yaml", ".yml"}:
        import yaml

        payload = yaml.safe_load(text)
    else:
        payload = json.loads(text)
    return build_taxonomy(payload)


def _assemble_taxonomy() -> tuple[dict[str, PolicyClause], dict[str, str]]:
    policy_file = os.getenv(POLICY_FILE_ENV, "").strip()
    if not policy_file:
        return dict(_DEFAULT_POLICY_CLAUSES), dict(_DEFAULT_CATEGORY_ALIASES)
    clauses, file_aliases = load_policy_file(policy_file)
    # Built-in aliases are kept only when their target category still exists —
    # an alias resolving to a category outside the taxonomy would crash clause
    # lookup downstream. File-provided aliases win on collision.
    merged = {alias: target for alias, target in _DEFAULT_CATEGORY_ALIASES.items() if target in clauses}
    merged.update(file_aliases)
    return clauses, merged


POLICY_CLAUSES, CATEGORY_ALIASES = _assemble_taxonomy()

# Derived, not hardcoded: a custom taxonomy's tier-1 clauses get the same
# never-adjudicated-by-AI treatment as the built-in ones.
TIER1_CATEGORIES = {category for category, clause in POLICY_CLAUSES.items() if clause.tier == 1}

# Agents are asked for a category *name* but also carry clause ids in
# `cited_clauses`, and they sometimes answer with the id ("CIV-POL-001") or the
# full citation ("CIV-POL-001 (Civility / Political Content)"). Either form
# identifies a category unambiguously, so resolving it is strictly safer than
# failing closed: the old behaviour turned a formatting slip into an
# over-escalation, spending human review on a case the model had actually
# decided. Derived from the active taxonomy, so custom policies work too.
CLAUSE_ID_TO_CATEGORY: dict[str, str] = {
    clause.clause_id.lower(): clause.category for clause in POLICY_CLAUSES.values()
}


def normalize_category(category: str) -> str | None:
    """Resolve a model-supplied category to a canonical taxonomy label.

    Accepts the canonical name, a known alias, a clause id, or a full citation
    string. Returns ``None`` when the label cannot be resolved, so callers can
    decide how to fail — deliberately *not* defaulting to ``"No Violation"``
    here, because a silent downgrade to tier 0 is exactly the failure mode this
    guards against.
    """
    if not category:
        return None
    if category in POLICY_CLAUSES:
        return category

    key = category.strip().lower()
    for canonical in POLICY_CLAUSES:
        if canonical.lower() == key:
            return canonical
    alias_match = CATEGORY_ALIASES.get(key)
    if alias_match:
        return alias_match
    # Clause id, bare or as the leading token of a full citation.
    clause_key = key.split("(", 1)[0].strip()
    return CLAUSE_ID_TO_CATEGORY.get(clause_key)


def get_clause_for_category(category: str) -> PolicyClause:
    resolved = normalize_category(category)
    if resolved is None:
        return POLICY_CLAUSES["No Violation"]
    return POLICY_CLAUSES[resolved]


def retrieve_policy(query: str, limit: int = 3) -> list[dict[str, str | int]]:
    from sentinel.tools.policy_index import semantic_policy_search

    semantic = semantic_policy_search(query, limit)
    if semantic is not None:
        return semantic
    return _keyword_retrieve_policy(query, limit)


# Words that carry no policy signal. The previous implementation matched raw
# query tokens as *substrings* of the clause text, so "is"/"to"/"in"/"me" hit
# nearly every clause ("in" is inside "including", "to" inside "protect"). With
# no scoring on top, results fell back to dict-insertion order — which put the
# two Tier-1 clauses first for almost any query, handing the specialist agent
# "Child Exploitation" as the most relevant policy for benign content.
_STOPWORDS = frozenset(
    """
    the a an and or but if then than that this these those there here
    is are was were be been being am do does did doing done
    have has had having will would shall should can could may might must
    for from with without within into onto upon about above below under over
    out off across through during before after again once
    not no nor only own same so too very just also
    you your yours they them their our ours we us
    who whom whose which what when where why how
    all any both each few more most other some such
    please help want need get got make made like
    """.split()
)

# Longest clause-name token is what a query most plausibly names outright, so an
# explicit category mention outranks any amount of incidental summary overlap.
_CATEGORY_NAME_WEIGHT = 10
_CATEGORY_TOKEN_WEIGHT = 3
_PILLAR_TOKEN_WEIGHT = 2
_SUMMARY_TOKEN_WEIGHT = 1


def _stem(token: str) -> str:
    """Strip the plural/participle suffixes that split obvious matches.

    Deliberately crude — the taxonomy is a closed vocabulary of ~19 clauses, so
    this only has to reconcile "cheats"/"cheating" and "scam"/"scams", not
    survive general English. The length guard keeps short words intact.
    """
    for suffix in ("ing", "ed", "es", "s"):
        if token.endswith(suffix) and len(token) - len(suffix) >= 3:
            return token[: -len(suffix)]
    return token


def _significant_tokens(text: str) -> set[str]:
    """Word-boundary, stemmed tokens worth matching on, lowercased.

    Word boundaries (not substrings) and a stopword filter are both required:
    either one alone still lets a common word match an unrelated clause.
    """
    return {
        _stem(token)
        for token in re.findall(r"[a-z0-9]+", text.lower())
        if len(token) > 2 and token not in _STOPWORDS
    }


def _clause_payload(clause: PolicyClause) -> dict[str, str | int]:
    return {
        "category": clause.category,
        "pillar": clause.pillar,
        "severity_tier": clause.tier,
        "clause_id": clause.clause_id,
        "summary": clause.summary,
        "source_url": clause.source_url,
    }


def _keyword_retrieve_policy(query: str, limit: int = 3) -> list[dict[str, str | int]]:
    """Rank clauses by weighted token overlap with the query.

    Fallback for when the semantic index is unavailable. It is a keyword scorer,
    not a retriever — it is expected to be worse than embeddings, but it must not
    be *actively misleading*, which unranked substring matching was.
    """
    query_lower = query.lower()
    query_tokens = _significant_tokens(query)

    scored: list[tuple[int, int, PolicyClause]] = []
    for clause in POLICY_CLAUSES.values():
        score = _CATEGORY_NAME_WEIGHT if clause.category.lower() in query_lower else 0
        score += _CATEGORY_TOKEN_WEIGHT * len(query_tokens & _significant_tokens(clause.category))
        score += _PILLAR_TOKEN_WEIGHT * len(query_tokens & _significant_tokens(clause.pillar))
        score += _SUMMARY_TOKEN_WEIGHT * len(query_tokens & _significant_tokens(clause.summary))
        if score > 0:
            # Tier ascends as severity falls, so it breaks ties toward the more
            # severe clause without ever outranking a better keyword match.
            scored.append((-score, clause.tier, clause))

    if not scored:
        return [_clause_payload(POLICY_CLAUSES["No Violation"])]
    scored.sort(key=lambda entry: (entry[0], entry[1]))
    return [_clause_payload(clause) for _, _, clause in scored[:limit]]


@function_tool
def retrieve_policy_tool(query: str) -> str:
    """Retrieve the community-guideline clauses most relevant to a content signal.

    Args:
        query: Natural-language description of the observed content signal
            (e.g. "user telling another player to hurt themselves").
    """
    clauses = retrieve_policy(query)
    lines = []
    for clause in clauses:
        relevance = clause.get("relevance")
        suffix = f" (relevance {relevance})" if relevance is not None else ""
        lines.append(
            f"{clause['clause_id']} [Tier {clause['severity_tier']}] {clause['pillar']} / {clause['category']}: "
            f"{clause['summary']}{suffix}"
        )
    return "\n".join(lines)
