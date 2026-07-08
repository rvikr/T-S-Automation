from __future__ import annotations

import json
import re
from pathlib import Path

from sentinel.config import EVAL_RUNS_DIR, UPLOADS_DIR
from sentinel.models import Case, ModerationLog
from sentinel.tools.media_utils import AUDIO_EXTENSIONS, IMAGE_EXTENSIONS, TEXT_EXTENSIONS, VIDEO_EXTENSIONS


MODERATION_VIEW_LABEL = "Moderation"
LOG_VIEW_LABEL = "Logs"
METRICS_VIEW_LABEL = "Metrics"

FLOW_STAGES = [
    "Ingest upload",
    "Detect modality",
    "Retrieve policy",
    "Retrieve precedents",
    "Specialist review",
    "Guardrail check",
    "Senior or human escalation",
    "Audit log",
]

UPLOAD_EXTENSIONS = sorted(IMAGE_EXTENSIONS | VIDEO_EXTENSIONS | AUDIO_EXTENSIONS | {".txt", ".md", ".json"})

OPENAI_TRACES_URL_TEMPLATE = "https://platform.openai.com/traces/trace?trace_id={trace_id}"


def openai_trace_url(trace_id: str) -> str:
    return OPENAI_TRACES_URL_TEMPLATE.format(trace_id=trace_id)

DEMO_POLICY_SIGNALS = {
    "Benign upload": ("No Violation", "allow", "benign synthetic demo upload"),
    "Clear reject": (
        "Violent Content & Gore",
        "reject",
        "synthetic demo label for gore category, no depiction",
    ),
    "Ambiguous senior review": (
        "Harassment & Discrimination",
        "ambiguous",
        "synthetic borderline insult between friends",
    ),
    "Tier-1 human route": (
        "Child Exploitation",
        "allow",
        "synthetic tier-1 stand-in label only for human routing",
    ),
}


def format_moderation_log_rows(logs: list[ModerationLog]) -> list[dict[str, str | int]]:
    rows: list[dict[str, str | int]] = []
    for log in logs:
        rows.append(
            {
                "ID": log.id,
                "Time": log.timestamp,
                "Case": log.case_id,
                "Decision": log.decision,
                "Reviewer": log.reviewer,
                "Escalated": "Yes" if log.escalation_triggered else "No",
                "Escalation Type": log.escalation_type or "",
                "Escalation Details": _format_escalation_details(log.escalation_details),
                "Clause": log.clause,
                "Rationale": log.rationale,
            }
        )
    return rows


def _format_escalation_details(details: dict) -> str:
    if not details:
        return ""
    return json.dumps(details, sort_keys=True)


def infer_upload_asset_type(filename: str, content_type: str | None = None) -> str:
    mime = (content_type or "").lower()
    if mime.startswith("image/"):
        return "image"
    if mime.startswith("video/"):
        return "video"
    if mime.startswith("audio/"):
        return "audio"
    if mime.startswith("text/"):
        return "text"

    suffix = Path(filename).suffix.lower()
    if suffix in IMAGE_EXTENSIONS:
        return "image"
    if suffix in VIDEO_EXTENSIONS:
        return "video"
    if suffix in AUDIO_EXTENSIONS:
        return "audio"
    return "text"


def safe_upload_name(filename: str) -> str:
    base = Path(filename).name or "upload.bin"
    return re.sub(r"[^A-Za-z0-9._-]", "_", base)


def build_uploaded_case(
    name: str,
    content_type: str | None,
    payload: bytes,
    category: str,
    decision: str,
    label: str,
    upload_dir: str | Path = UPLOADS_DIR,
) -> Case:
    target_dir = Path(upload_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    safe_name = safe_upload_name(name)
    asset_path = target_dir / safe_name
    asset_path.write_bytes(payload)
    asset_type = infer_upload_asset_type(safe_name, content_type)
    return Case(
        id=f"upload-{asset_path.stem}",
        asset_type=asset_type,
        asset_path=str(asset_path),
        metadata={
            "synthetic_label": label,
            "expected_category": category,
            "expected_decision": decision,
            "upload_filename": safe_name,
            "upload_content_type": content_type or "",
        },
    )


def describe_trace_event(event: str) -> tuple[str, str]:
    """Map a raw pipeline trace entry to an (icon, human-readable text) pair."""
    if event.startswith("ingest:"):
        return "📥", f"Case ingested: {event.split(':', 1)[1]}"
    if event.startswith("trace.openai:"):
        return "🛰️", f"OpenAI platform trace started: {event.split(':', 1)[1]}"
    if event.startswith("orchestrator.detect_asset_type:"):
        return "🧭", f"Modality detected: {event.rsplit(':', 1)[1]}"
    if event == "production_analysis:enabled":
        return "⚡", "Live agent analysis enabled"
    if event.startswith("handoff:senior-reviewer:in-run"):
        return "🔀", "Specialist handed off to Senior Reviewer during the run"
    if event == "handoff:senior-reviewer":
        return "🔼", "Escalated to Senior Reviewer"
    if event.startswith("handoff:"):
        return "🧭", f"Routed to {event.split(':', 1)[1].replace('-', ' ')}"
    if event.startswith("agent.evidence:"):
        return "🧾", f"Evidence prepared ({event.split(':', 1)[1].removeprefix('evidence:')})"
    if event.startswith("agent.agent_run:"):
        _, name, model = event.split(":", 2)
        return "🤖", f"{name} agent started ({model})"
    if event.startswith("agent.tool_call:"):
        _, agent, tool = event.split(":", 2)
        return "🔧", f"{agent} called {tool}"
    if event.startswith("agent.handoff:"):
        return "🔀", f"Agent handoff: {event.split(':', 1)[1].replace('->', ' → ')}"
    if event.startswith("agent.verdict_drafted:"):
        return "📝", f"{event.split(':', 1)[1]} drafted a verdict"
    if event.startswith("agent.guardrail.tier1.tripwire:"):
        return "🚨", f"Tier-1 guardrail halted the agent mid-run ({event.rsplit(':', 1)[1]})"
    if event == "guardrail.tier1.triggered":
        return "🚨", "Tier-1 safety rail engaged: automated adjudication bypassed"
    if event.startswith("hash_match.flag:"):
        return "#️⃣", f"Known-hash list check: {'match' if event.endswith('True') else 'no match'}"
    if event.startswith("specialist.verdict:"):
        _, decision, clause = event.split(":", 2)
        return "⚖️", f"Specialist verdict: {decision} under {clause}"
    if event.startswith("senior.verdict:"):
        _, decision, clause = event.split(":", 2)
        return "⚖️", f"Senior verdict: {decision} under {clause}"
    if event == "human_ticket.created":
        return "🎫", "Human review ticket created"
    if event.startswith("ticket.external:jira:"):
        return "🟦", f"Jira issue created: {event.rsplit(':', 1)[1]}"
    if event == "ticket.external:local-only":
        return "🎫", "Ticket kept local (Jira not configured or unavailable)"
    if event == "quarantine.completed":
        return "🔒", "Content quarantined"
    if event.startswith("agent.agent_runtime.error:"):
        return "⚠️", f"Agent runtime error — failed closed to review ({event.rsplit(':', 1)[1]})"
    if event.startswith("latency:"):
        return "⏱️", f"End-to-end latency: {event.split(':', 1)[1]}"
    if event == "agent.usage:unavailable:guardrail-halt":
        return "⏱️", "Token usage unavailable: the guardrail halted the run before completion"
    return "•", event


def describe_live_event(event: str) -> str:
    """Map a live:* runtime event to a status line rendered mid-run."""
    if event.startswith("live:agent_start:"):
        return f"🤖 {event.split(':', 2)[2]} is on the case"
    if event.startswith("live:thinking:"):
        return f"💭 {event.split(':', 2)[2]} is reasoning over the evidence"
    if event.startswith("live:tool_call:"):
        _, _, agent, tool = event.split(":", 3)
        return f"🔧 {agent} called `{tool}`"
    if event.startswith("live:tool_done:"):
        _, _, agent, tool = event.split(":", 3)
        return f"✅ `{tool}` returned to {agent}"
    if event.startswith("live:handoff:"):
        return f"🔀 Handoff: {event.split(':', 2)[2].replace('->', ' → ')}"
    if event.startswith("live:agent_end:"):
        return f"📝 {event.split(':', 2)[2]} finalized its assessment"
    return event


def list_eval_runs(eval_dir: str | Path = EVAL_RUNS_DIR) -> list[Path]:
    root = Path(eval_dir)
    if not root.exists():
        return []
    return sorted(
        (path for path in root.iterdir() if path.is_dir() and (path / "results.json").exists()),
        key=lambda path: path.name,
        reverse=True,
    )


def load_eval_run(run_dir: str | Path) -> dict:
    return json.loads((Path(run_dir) / "results.json").read_text(encoding="utf-8"))


def build_production_uploaded_case(
    name: str,
    content_type: str | None,
    payload: bytes,
    upload_dir: str | Path = UPLOADS_DIR,
) -> Case:
    target_dir = Path(upload_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    safe_name = safe_upload_name(name)
    asset_path = target_dir / safe_name
    asset_path.write_bytes(payload)
    asset_type = infer_upload_asset_type(safe_name, content_type)
    return Case(
        id=f"upload-{asset_path.stem}",
        asset_type=asset_type,
        asset_path=str(asset_path),
        metadata={
            "analysis_mode": "production",
            "upload_filename": safe_name,
            "upload_content_type": content_type or "",
        },
    )
