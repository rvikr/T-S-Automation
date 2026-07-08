from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

if __package__ is None or __package__ == "":
    _this_dir = Path(__file__).resolve().parent
    sys.path[:] = [p for p in sys.path if Path(p).resolve() != _this_dir]
    sys.path.insert(0, str(_this_dir.parent))

from sentinel.agents.orchestrator import run_batch, run_case
from sentinel.config import DEFAULT_DB_PATH
from sentinel.models import Case, CaseResult, Verdict
from sentinel.tools.audit_log import init_db, reset_db, write_audit
from sentinel.tools.media_utils import load_synthetic_cases
from sentinel.tools.policy_retrieval import get_clause_for_category
from sentinel.tools.precedent_memory import clear_precedents
from sentinel.tools.ticketing import create_human_ticket, list_human_tickets


def result_to_dict(result: CaseResult) -> dict:
    return {
        "case_id": result.case.id,
        "asset_type": result.case.asset_type,
        "verdict": asdict(result.verdict),
        "warning_message": result.warning_message,
        "ticket": asdict(result.ticket) if result.ticket else None,
        "quarantined": result.quarantined,
        "trace": result.trace,
    }


# Curated rows for a believable Logs page on demo day: varied decisions,
# categories, and reviewers, plus one human-ticket escalation.
# (case_id, decision, severity_tier, category, reviewer, rationale, needs_ticket)
DEMO_SEED_ROWS: list[tuple[str, str, int, str, str, str, bool]] = [
    ("upload-user-avatar-3841", "allow", 0, "No Violation", "specialist",
     "Avatar art with no policy signals; cited SAFE-ALLOW-000 after policy retrieval.", False),
    ("upload-chat-report-1207", "reject", 2, "Harassment & Discrimination", "specialist",
     "Identity-targeted insults in reported chat excerpt; matches SAF-HAR-001 examples.", False),
    ("upload-voice-note-0932", "allow", 0, "No Violation", "specialist",
     "Transcribed voice note is a benign gameplay question; no clause matched.", False),
    ("upload-market-listing-5514", "reject", 3, "Cheating & Scams", "specialist",
     "Listing promises free currency for account credentials; classic scam pattern under INT-SCAM-001.", False),
    ("upload-profile-banner-2276", "reject", 3, "Romantic/Sexual Content", "senior",
     "Specialist flagged borderline imagery; senior review confirmed CIV-RSX-001 violation.", False),
    ("upload-group-post-7743", "allow", 2, "Political Content", "senior",
     "Civic-information post without campaigning; senior review allowed under CIV-POL-001 context rules.", False),
    ("upload-clip-review-8830", "reject", 3, "Violent Content & Gore", "specialist",
     "Uploaded clip depicts graphic injury detail; unambiguous CIV-VCG-001 violation.", False),
    ("upload-dm-screenshot-4462", "ambiguous", 2, "Suicide/Self-Injury", "human",
     "Ambiguous self-harm phrasing in DM screenshot; routed to human review per SAF-SSI-001.", True),
    ("upload-shop-icon-9105", "reject", 3, "IP Violations", "specialist",
     "Shop icon reuses protected studio artwork without authorization; INT-IP-001.", False),
    ("upload-livestream-6689", "allow", 0, "No Violation", "specialist",
     "Livestream thumbnail shows standard gameplay; no policy category matched.", False),
]


def seed_demo_data(db_path: Path) -> int:
    """Populate the audit log with curated, production-looking rows for the demo."""
    init_db(db_path)
    for case_id, decision, tier, category, reviewer, rationale, needs_ticket in DEMO_SEED_ROWS:
        clause = get_clause_for_category(category)
        verdict = Verdict(
            case_id=case_id,
            decision=decision,  # type: ignore[arg-type]
            severity_tier=tier,
            category=category,
            policy_clause=clause.citation,
            confidence=0.93,
            rationale=rationale,
            reviewer=reviewer,
        )
        write_audit(verdict, db_path, tenant_name="Example Platform")
        if needs_ticket:
            case = Case(id=case_id, asset_type="text", asset_path="", metadata={"tenant_name": "Example Platform"})
            create_human_ticket(case, tier, category, db_path)
    return len(DEMO_SEED_ROWS)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run Sentinel synthetic moderation cases.")
    parser.add_argument("--case-id", help="Run one case from the synthetic manifest.")
    parser.add_argument("--repeat", type=int, default=1, help="Run the synthetic batch N times.")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="SQLite DB path.")
    parser.add_argument("--reset-db", action="store_true", help="Reset audit, ticket, and precedent tables first.")
    parser.add_argument("--clear-precedents", action="store_true", help="Clear precedent memory before running.")
    parser.add_argument("--seed-demo", action="store_true", help="Seed curated demo rows into the audit log, then exit.")
    parser.add_argument("--json", action="store_true", help="Print JSON instead of human-readable output.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    db_path = Path(args.db)
    if args.reset_db:
        reset_db(db_path)
    else:
        init_db(db_path)
    if args.clear_precedents:
        clear_precedents(db_path)
    if args.seed_demo:
        count = seed_demo_data(db_path)
        print(f"Seeded {count} demo moderation logs (1 human-ticket escalation) into {db_path}")
        return 0

    cases = load_synthetic_cases()
    if args.case_id:
        case_by_id = {case.id: case for case in cases}
        if args.case_id not in case_by_id:
            print(f"Unknown case id: {args.case_id}", file=sys.stderr)
            return 2
        result = run_case(case_by_id[args.case_id], db_path=db_path)
        payload = result_to_dict(result)
        print(json.dumps(payload, indent=2) if args.json else _format_result(payload))
        return 0

    runs = []
    for index in range(args.repeat):
        batch = run_batch(cases, db_path=db_path)
        runs.append(
            {
                "run": index + 1,
                "case_count": len(batch.results),
                "learning_escalation_rate": batch.escalation_rate,
                "results": [result_to_dict(result) for result in batch.results],
                "open_tickets": [asdict(ticket) for ticket in list_human_tickets(db_path)],
            }
        )
    if args.json:
        print(json.dumps(runs, indent=2))
    else:
        for run in runs:
            print(
                f"Run {run['run']}: {run['case_count']} cases, "
                f"non-Tier-1 escalation rate={run['learning_escalation_rate']:.2%}, "
                f"open tickets={len(run['open_tickets'])}"
            )
    return 0


def _format_result(payload: dict) -> str:
    verdict = payload["verdict"]
    lines = [
        f"Case: {payload['case_id']} ({payload['asset_type']})",
        f"Decision: {verdict['decision']} by {verdict['reviewer']}",
        f"Severity: Tier {verdict['severity_tier']}",
        f"Category: {verdict['category']}",
        f"Clause: {verdict['policy_clause']}",
        f"Rationale: {verdict['rationale']}",
    ]
    if payload["warning_message"]:
        lines.append(f"Warning: {payload['warning_message']}")
    if payload["ticket"]:
        lines.append(f"Ticket: {payload['ticket']['id']}")
    lines.append("Trace:")
    lines.extend(f"  - {event}" for event in payload["trace"])
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
