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
from sentinel.models import CaseResult
from sentinel.tools.audit_log import init_db, reset_db
from sentinel.tools.media_utils import load_synthetic_cases
from sentinel.tools.precedent_memory import clear_precedents
from sentinel.tools.ticketing import list_human_tickets


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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run Sentinel synthetic moderation cases.")
    parser.add_argument("--case-id", help="Run one case from the synthetic manifest.")
    parser.add_argument("--repeat", type=int, default=1, help="Run the synthetic batch N times.")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="SQLite DB path.")
    parser.add_argument("--reset-db", action="store_true", help="Reset audit, ticket, and precedent tables first.")
    parser.add_argument("--clear-precedents", action="store_true", help="Clear precedent memory before running.")
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
