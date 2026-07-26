"""The keyword retrieval fallback must rank, not just filter.

The original implementation matched raw query tokens as substrings against the
clause text and returned whatever matched in dict-insertion order. Stopwords
("is", "to", "in") are substrings of nearly every clause summary, so almost any
query "matched" almost every clause — and insertion order put the two Tier-1
clauses first. The specialist agent calls this as `retrieve_policy_tool` to
ground its verdict, so a benign upload was handed "Child Exploitation" as its
most relevant policy whenever the semantic index was unavailable.
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pytest

from sentinel.tools.policy_retrieval import (
    POLICY_CLAUSES,
    TIER1_CATEGORIES,
    _keyword_retrieve_policy,
    _significant_tokens,
)


def _categories(query: str, limit: int = 3) -> list[str]:
    return [str(clause["category"]) for clause in _keyword_retrieve_policy(query, limit)]


# ---------------------------------------------------------------------------
# The regression itself: stopwords must not surface Tier-1 clauses
# ---------------------------------------------------------------------------

STOPWORD_ONLY_QUERIES = [
    "to",
    "is in the to me",
    "you are the one who is in it",
    "and then it was over",
]


@pytest.mark.parametrize("query", STOPWORD_ONLY_QUERIES)
def test_stopword_queries_never_surface_tier1(query: str):
    categories = _categories(query)
    leaked = set(categories) & TIER1_CATEGORIES
    assert not leaked, f"{query!r} surfaced Tier-1 clause(s) {leaked} with no real signal"


@pytest.mark.parametrize("query", STOPWORD_ONLY_QUERIES)
def test_stopword_queries_report_no_match(query: str):
    """A query with no policy signal should say so, not guess."""
    assert _categories(query) == ["No Violation"]


def test_unmatched_query_falls_back_to_no_violation():
    assert _categories("zzzzz qqqqq") == ["No Violation"]


# ---------------------------------------------------------------------------
# Ranking quality
# ---------------------------------------------------------------------------

def test_explicit_category_name_ranks_first():
    for category in POLICY_CLAUSES:
        assert _categories(category)[0] == category, f"naming {category!r} did not rank it first"


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("sharing my home address publicly", "Sharing Personal Info"),
        ("this post is campaigning for a political candidate", "Political Content"),
        ("explicit profanity in the caption", "Profanity"),
        ("advertising my own shop", "Advertising"),
    ],
)
def test_content_queries_rank_the_right_clause_first(query: str, expected: str):
    assert _categories(query)[0] == expected


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("selling cheats and hacked accounts", "Cheating & Scams"),  # cheats -> cheat
        ("scam listing promising free currency", "Cheating & Scams"),  # scam -> scams
    ],
)
def test_stemming_reconciles_plural_and_participle_forms(query: str, expected: str):
    assert expected in _categories(query)


def test_limit_is_respected():
    assert len(_keyword_retrieve_policy("content violation policy review", limit=2)) <= 2


def test_results_are_deterministic():
    query = "graphic violence and gore in the uploaded clip"
    assert _categories(query) == _categories(query)


# ---------------------------------------------------------------------------
# Tokenizer contract
# ---------------------------------------------------------------------------

def test_tokenizer_drops_stopwords_and_short_tokens():
    tokens = _significant_tokens("It is in the to me at a chat")
    assert tokens == {"chat"}


def test_tokenizer_matches_on_word_boundaries_not_substrings():
    """'in' must not match inside 'benign' — the original substring bug."""
    assert "in" not in _significant_tokens("benign")
