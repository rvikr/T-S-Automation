from __future__ import annotations

import json
import re
from pathlib import Path

from sentinel.config import UPLOADS_DIR
from sentinel.models import Case, ModerationLog
from sentinel.tools.media_utils import AUDIO_EXTENSIONS, IMAGE_EXTENSIONS, TEXT_EXTENSIONS, VIDEO_EXTENSIONS


MODERATION_VIEW_LABEL = "Moderation"
LOG_VIEW_LABEL = "Logs"

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
