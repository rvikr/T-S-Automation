"""Fuzz the Tier-1 category-normalization surface.

Tier-1 *recall* rests on ``normalize_category`` catching whatever wording a
model emits for child-safety or extremism content. These tests sweep the
plausible phrasings and pin the two halves of the contract:

1. Known synonyms and their casing/punctuation variants resolve to the
   canonical Tier-1 category (which the rails then quarantine + escalate).
2. Anything unresolvable **fails closed** in ``build_verdict``: a non-allow
   decision with an unknown label routes to human review — it must never be
   relabelled as an allow or reach tier 0 with an enforcement decision intact.
"""

import pytest

from sentinel.models import build_verdict
from sentinel.tools.policy_retrieval import CATEGORY_ALIASES, TIER1_CATEGORIES, normalize_category

# Model-plausible phrasings that MUST land on a Tier-1 category.
_CHILD_SAFETY_PHRASINGS = [
    "CSAM",
    "csem",
    "Child Sexual Abuse Material",
    "child sexual exploitation",
    "Child Exploitation",
    "CHILD EXPLOITATION",
    "  child exploitation  ",
    "grooming",
    "Child Grooming",
    "sexualization of minors",
    "minor sexualization",
    "child endangerment",
]

_EXTREMISM_PHRASINGS = [
    "terrorism",
    "Terrorism & Violent Extremism",
    "terrorism and violent extremism",
    "Terrorism/Violent Extremism",
    "terrorist content",
    "Terrorist Propaganda",
    "violent extremist content",
    "glorification of terrorism",
    "EXTREMISM",
]


@pytest.mark.parametrize("phrase", _CHILD_SAFETY_PHRASINGS)
def test_child_safety_phrasings_resolve_to_tier1(phrase):
    resolved = normalize_category(phrase)
    assert resolved == "Child Exploitation", f"{phrase!r} resolved to {resolved!r}"
    assert resolved in TIER1_CATEGORIES


@pytest.mark.parametrize("phrase", _EXTREMISM_PHRASINGS)
def test_extremism_phrasings_resolve_to_tier1(phrase):
    resolved = normalize_category(phrase)
    assert resolved == "Terrorism & Violent Extremism", f"{phrase!r} resolved to {resolved!r}"
    assert resolved in TIER1_CATEGORIES


def test_clause_ids_resolve_to_their_category():
    """A model answering with the clause id must not cost an over-escalation."""
    from sentinel.tools.policy_retrieval import POLICY_CLAUSES

    for category, clause in POLICY_CLAUSES.items():
        assert normalize_category(clause.clause_id) == category
        assert normalize_category(clause.clause_id.lower()) == category
        # The full citation form the agents also emit.
        assert normalize_category(clause.citation) == category


def test_clause_id_for_a_tier1_clause_still_gets_the_rail():
    verdict = build_verdict(
        case_id="clause-id-1",
        decision="reject",
        category="SAF-CE-001",
        confidence=0.7,
        rationale="agent answered with the clause id",
        reviewer="specialist",
    )
    assert verdict.category == "Child Exploitation"
    assert verdict.severity_tier == 1
    assert verdict.decision == "ambiguous"
    assert verdict.confidence >= 0.95


def test_every_alias_targets_an_existing_category():
    # An alias pointing outside the taxonomy would crash clause lookup the
    # moment a model used it.
    from sentinel.tools.policy_retrieval import POLICY_CLAUSES

    for alias, target in CATEGORY_ALIASES.items():
        assert target in POLICY_CLAUSES, f"alias {alias!r} -> unknown {target!r}"


@pytest.mark.parametrize(
    "unknown_label",
    [
        # Dangerous-sounding but unmapped: must fail CLOSED, never to allow.
        "exploitation of a minor",
        "abuse imagery",
        "radicalization",
        "incitement to violence",
        "sexual content involving anyone underage",
        "harmful content",
        "",
    ],
)
def test_unmapped_labels_fail_closed_never_to_allow(unknown_label):
    assert normalize_category(unknown_label) is None

    verdict = build_verdict(
        case_id="fuzz-1",
        decision="reject",
        category=unknown_label,
        confidence=0.9,
        rationale="model verdict with unmapped label",
        reviewer="specialist",
    )
    # The one unacceptable outcome is an allow (or a surviving enforcement
    # decision at tier 0). Fail-closed means: route to human review.
    assert verdict.decision == "ambiguous"
    assert "human review" in verdict.rationale.lower()


def test_tier1_verdict_from_alias_gets_the_full_rail_treatment():
    verdict = build_verdict(
        case_id="fuzz-2",
        decision="reject",
        category="grooming",
        confidence=0.6,
        rationale="specialist verdict using a synonym",
        reviewer="specialist",
    )
    assert verdict.category == "Child Exploitation"
    assert verdict.severity_tier == 1
    assert verdict.decision == "ambiguous"  # never auto-adjudicated
    assert verdict.confidence >= 0.95
