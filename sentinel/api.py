from __future__ import annotations

import base64
import binascii
import hmac
import os
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from sentinel.agents.orchestrator import run_case
from sentinel.config import DEFAULT_DB_PATH, UPLOADS_DIR
from sentinel.models import ApiKeyRecord, Case, CaseResult, ModerationLog
from sentinel.tools.audit_log import init_db, list_moderation_logs
from sentinel.tools.api_keys import authenticate_api_key, create_api_key, list_api_keys, revoke_api_key
from sentinel.ui_uploads import safe_upload_name


TICKETING_SYSTEMS = ["jira", "servicenow", "zendesk", "webhook"]
AssetType = Literal["text", "image", "audio", "video"]


class ModerationRequest(BaseModel):
    case_id: str | None = None
    asset_type: AssetType = "text"
    content: str | None = None
    content_base64: str | None = None
    filename: str | None = None
    content_type: str | None = None
    source_system: str | None = None
    external_reference: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class CreateApiKeyRequest(BaseModel):
    tenant_name: str
    project_name: str
    environment: Literal["test", "live"] = "test"


def create_app(
    db_path: str | Path = DEFAULT_DB_PATH,
    upload_dir: str | Path = UPLOADS_DIR / "api",
    admin_token: str | None = None,
) -> FastAPI:
    resolved_db_path = Path(db_path)
    resolved_upload_dir = Path(upload_dir)
    resolved_admin_token = admin_token if admin_token is not None else os.getenv("SENTINEL_ADMIN_TOKEN")
    init_db(resolved_db_path)

    app = FastAPI(
        title="Sentinel Autonomous Moderation API",
        version="1.0.0",
        description=(
            "Vendor-neutral moderation API for autonomous enforcement and ticketing-tool "
            "integration with Jira, ServiceNow, Zendesk, or in-house queues."
        ),
    )

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/admin/api-keys", status_code=201)
    def create_key(request: CreateApiKeyRequest, authorization: str | None = Header(default=None)) -> dict:
        _require_admin(authorization, resolved_admin_token)
        try:
            return create_api_key(
                resolved_db_path,
                tenant_name=request.tenant_name,
                project_name=request.project_name,
                environment=request.environment,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/admin/api-keys")
    def list_keys(authorization: str | None = Header(default=None)) -> dict:
        _require_admin(authorization, resolved_admin_token)
        keys = [_api_key_record_payload(record) for record in list_api_keys(resolved_db_path)]
        return {"count": len(keys), "keys": keys}

    @app.post("/admin/api-keys/{key_id}/revoke")
    def revoke_key(key_id: str, authorization: str | None = Header(default=None)) -> dict:
        _require_admin(authorization, resolved_admin_token)
        record = revoke_api_key(resolved_db_path, key_id)
        if record is None:
            raise HTTPException(status_code=404, detail=f"Unknown API key: {key_id}")
        return _api_key_record_payload(record)

    @app.post("/moderation/cases", status_code=201)
    def moderate_case(request: ModerationRequest, authorization: str | None = Header(default=None)) -> dict:
        api_key = _require_api_key(authorization, resolved_db_path)
        case = _build_case_from_request(request, resolved_upload_dir, api_key)
        result = run_case(case, db_path=resolved_db_path)
        return _result_payload(result)

    @app.get("/moderation/logs")
    def get_moderation_logs(
        escalated: bool | None = None,
        authorization: str | None = Header(default=None),
    ) -> dict:
        api_key = _require_api_key(authorization, resolved_db_path)
        logs = list_moderation_logs(resolved_db_path, tenant_name=api_key.tenant_name)
        if escalated is not None:
            logs = [log for log in logs if log.escalation_triggered is escalated]
        return {"count": len(logs), "logs": [_log_payload(log) for log in logs]}

    @app.get("/moderation/logs/{case_id}")
    def get_case_logs(case_id: str, authorization: str | None = Header(default=None)) -> dict:
        api_key = _require_api_key(authorization, resolved_db_path)
        logs = [
            log
            for log in list_moderation_logs(resolved_db_path, tenant_name=api_key.tenant_name)
            if log.case_id == case_id
        ]
        if not logs:
            raise HTTPException(status_code=404, detail=f"No moderation logs found for case_id={case_id}")
        return {"count": len(logs), "logs": [_log_payload(log) for log in logs]}

    return app


def _build_case_from_request(request: ModerationRequest, upload_dir: Path, api_key: ApiKeyRecord) -> Case:
    case_id = safe_upload_name(request.case_id or f"api-{uuid.uuid4().hex[:12]}")
    payload = _decode_payload(request)
    upload_dir.mkdir(parents=True, exist_ok=True)
    filename = safe_upload_name(request.filename or f"{case_id}{_default_suffix(request.asset_type)}")
    asset_path = upload_dir / safe_upload_name(f"{case_id}-{filename}")
    asset_path.write_bytes(payload)

    metadata = dict(request.metadata)
    metadata.update(
        {
            "analysis_mode": "production",
            "api_key_id": api_key.key_id,
            "tenant_name": api_key.tenant_name,
            "project_name": api_key.project_name,
            "api_environment": api_key.environment,
            "source_system": request.source_system or "",
            "external_reference": request.external_reference or "",
            "upload_filename": filename,
            "upload_content_type": request.content_type or "",
        }
    )
    return Case(id=case_id, asset_type=request.asset_type, asset_path=str(asset_path), metadata=metadata)


def _decode_payload(request: ModerationRequest) -> bytes:
    if request.content_base64:
        try:
            return base64.b64decode(request.content_base64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise HTTPException(status_code=400, detail="content_base64 must be valid base64") from exc
    if request.content is not None:
        return request.content.encode("utf-8")
    raise HTTPException(status_code=400, detail="Provide either content or content_base64")


def _default_suffix(asset_type: AssetType) -> str:
    if asset_type == "image":
        return ".bin"
    if asset_type == "audio":
        return ".bin"
    if asset_type == "video":
        return ".bin"
    return ".txt"


def _require_admin(authorization: str | None, admin_token: str | None) -> None:
    if not admin_token:
        raise HTTPException(status_code=503, detail="Set SENTINEL_ADMIN_TOKEN before generating API keys")
    token = _extract_bearer_token(authorization)
    if token is None or not hmac.compare_digest(token, admin_token):
        raise HTTPException(status_code=401, detail="Invalid admin token", headers={"WWW-Authenticate": "Bearer"})


def _require_api_key(authorization: str | None, db_path: Path) -> ApiKeyRecord:
    token = _extract_bearer_token(authorization)
    if token is None:
        raise HTTPException(status_code=401, detail="Missing API key", headers={"WWW-Authenticate": "Bearer"})
    api_key = authenticate_api_key(db_path, token)
    if api_key is None:
        raise HTTPException(status_code=401, detail="Invalid or revoked API key", headers={"WWW-Authenticate": "Bearer"})
    return api_key


def _extract_bearer_token(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


def _api_key_record_payload(record: ApiKeyRecord) -> dict:
    return asdict(record)


def _result_payload(result: CaseResult) -> dict:
    escalation = _result_escalation(result)
    action = _enforcement_action(result)
    return {
        "case_id": result.case.id,
        "asset_type": result.case.asset_type,
        "verdict": asdict(result.verdict),
        "warning_message": result.warning_message,
        "quarantined": result.quarantined,
        "trace": result.trace,
        "enforcement": {
            "mode": "autonomous",
            "action": action,
            "should_block": action in {"reject", "escalate"} or result.quarantined,
            "escalation_triggered": escalation is not None,
            "escalation": escalation,
        },
        "integration": {
            "ticketing_systems": TICKETING_SYSTEMS,
            "ticketing_payload": _ticketing_payload(result, action, escalation),
            "jira": _jira_reference(result),
        },
    }


def _jira_reference(result: CaseResult) -> dict | None:
    ticket = result.ticket
    if ticket is None or not ticket.external_key:
        return None
    return {"key": ticket.external_key, "url": ticket.external_url}


def _result_escalation(result: CaseResult) -> dict | None:
    if result.ticket is not None:
        return {
            "type": "human_ticket",
            "reason": "Case requires human review before final platform enforcement.",
            "ticket": asdict(result.ticket),
        }
    if result.verdict.reviewer == "senior":
        return {
            "type": "senior_review",
            "reason": "Ambiguous case required senior reviewer resolution.",
            "reviewer": "senior",
            "status": "resolved",
        }
    if result.verdict.reviewer == "human":
        return {
            "type": "human_review",
            "reason": "Case is human-only and automated adjudication was bypassed.",
            "reviewer": "human",
            "status": "pending",
        }
    return None


def _enforcement_action(result: CaseResult) -> str:
    if result.ticket is not None or result.verdict.reviewer == "human" or result.verdict.decision == "ambiguous":
        return "escalate"
    if result.verdict.decision == "reject":
        return "reject"
    return "allow"


def _ticketing_payload(result: CaseResult, action: str, escalation: dict | None) -> dict:
    metadata = result.case.metadata
    verdict = result.verdict
    escalation_type = escalation["type"] if escalation else None
    ticket_id = result.ticket.id if result.ticket else None
    return {
        "summary": f"Sentinel moderation {action}: {result.case.id}",
        "description": (
            f"Decision={verdict.decision}; category={verdict.category}; "
            f"clause={verdict.policy_clause}; reviewer={verdict.reviewer}; "
            f"rationale={verdict.rationale}"
        ),
        "priority": _priority(verdict.severity_tier, action, escalation_type),
        "labels": ["sentinel", "moderation", action, _slug(verdict.category)],
        "fields": {
            "case_id": result.case.id,
            "source_system": metadata.get("source_system", ""),
            "external_reference": metadata.get("external_reference", ""),
            "decision": verdict.decision,
            "category": verdict.category,
            "severity_tier": verdict.severity_tier,
            "policy_clause": verdict.policy_clause,
            "confidence": verdict.confidence,
            "reviewer": verdict.reviewer,
            "escalation_triggered": escalation is not None,
            "escalation_type": escalation_type,
            "ticket_id": ticket_id,
            "quarantined": result.quarantined,
        },
    }


def _priority(severity_tier: int, action: str, escalation_type: str | None) -> str:
    if severity_tier <= 1 or escalation_type == "human_ticket":
        return "critical"
    if action in {"reject", "escalate"}:
        return "high"
    return "normal"


def _slug(value: str) -> str:
    return "".join(char.lower() if char.isalnum() else "-" for char in value).strip("-")


def _log_payload(log: ModerationLog) -> dict:
    return asdict(log)


app = create_app()
