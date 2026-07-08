from __future__ import annotations

from pathlib import Path

from sentinel.agents.common import specialist_review
from sentinel.models import Case, Verdict


def review_case(case: Case, db_path: str | Path) -> Verdict:
    return specialist_review(case, "text-specialist", db_path)
