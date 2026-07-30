"""Sentinel FastAPI moderation service.

Exposes a vendor-neutral REST API for autonomous content enforcement.
Route groups:

* ``/health`` — liveness probe.
* ``/admin/api-keys`` — tenant API key management (requires admin token).
* ``/moderation/cases`` — submit an asset for moderation (requires API key).
* ``/moderation/logs`` — retrieve per-tenant audit logs (requires API key).

Create the ASGI app with :func:`create_app`. The module-level ``app`` instance
at the bottom of the file is used by uvicorn when running directly.
"""

from __future__ import annotations

import base64
import binascii
import hmac
import logging
import mimetypes
import os
import sqlite3
import time
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field

from sentinel.agents.orchestrator import run_case
from sentinel.config import (
    ADMIN_RATE_LIMIT_PER_MINUTE,
    DEFAULT_DB_PATH,
    MAX_UPLOAD_BYTES,
    RATE_LIMIT_PER_MINUTE,
    UPLOADS_DIR,
    load_settings,
)
from sentinel.models import ApiKeyRecord, Case, CaseResult, ModerationLog
from sentinel.tools.api_keys import (
    authenticate_api_key,
    create_api_key,
    list_api_keys,
    revoke_api_key,
    rotate_api_key,
)
from sentinel.tools.audit_log import init_db, list_moderation_logs
from sentinel.tools.rate_limit import RateLimiter
from sentinel.tools.run_budget import consume_daily_budget, seconds_until_utc_midnight
from sentinel.tools.webhook import deliver_webhook, validate_callback_url
from sentinel.ui_uploads import openai_trace_url, safe_upload_name

# Ticketing systems Sentinel can actually mirror escalations to. Only list an
# integration here once it is implemented — advertising unbuilt ones in every
# API response misrepresents the product to integrators.
TICKETING_SYSTEMS = ["jira"]
AssetType = Literal["text", "image", "audio", "video"]

API_VERSION = "1.0.0"

logger = logging.getLogger(__name__)


class ModerationRequest(BaseModel):
    case_id: str | None = None
    asset_type: AssetType = "text"
    content: str | None = None
    content_base64: str | None = None
    filename: str | None = None
    content_type: str | None = None
    source_system: str | None = None
    external_reference: str | None = None
    # Optional verdict delivery: the full result payload is POSTed here after
    # moderation. Host must be on SENTINEL_WEBHOOK_ALLOWED_HOSTS (SSRF guard).
    callback_url: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class CreateApiKeyRequest(BaseModel):
    tenant_name: str
    project_name: str
    environment: Literal["test", "live"] = "test"
    # Omit for a non-expiring key (legacy behavior); set to auto-expire.
    expires_in_days: int | None = Field(default=None, ge=1)


class RotateApiKeyRequest(BaseModel):
    expires_in_days: int | None = Field(default=None, ge=1)


# ---------------------------------------------------------------------------
# Route handlers (module-level; create_app wires them to the FastAPI router)
# ---------------------------------------------------------------------------

def _handle_health(db_path: Path) -> tuple[dict[str, Any], int]:
    """Liveness plus a database reachability check.

    Reports configuration as booleans only — never credential values. Returns
    503 when the audit database cannot answer a trivial query, because a
    service that cannot persist verdicts must not be routed traffic.
    """
    from sentinel.tools.audit_log import db_connection
    from sentinel.tools.jira_client import jira_enabled

    database = "ok"
    status_code = 200
    try:
        with db_connection(db_path) as conn:
            conn.execute("SELECT 1").fetchone()
    except sqlite3.Error:
        logger.exception("Health check: audit database unreachable at %s", db_path)
        database = "unreachable"
        status_code = 503
    from sentinel.config import load_settings

    payload = {
        "status": "ok" if status_code == 200 else "degraded",
        "version": API_VERSION,
        "database": database,
        "openai_configured": load_settings().openai_api_key_present,
        "jira_configured": jira_enabled(),
        "ticketing_systems": TICKETING_SYSTEMS,
    }
    return payload, status_code


def _handle_create_key(
    request: CreateApiKeyRequest,
    db_path: Path,
    admin_token: str | None,
    authorization: str | None,
) -> dict:
    _require_admin(authorization, admin_token)
    try:
        return create_api_key(
            db_path,
            tenant_name=request.tenant_name,
            project_name=request.project_name,
            environment=request.environment,
            expires_in_days=request.expires_in_days,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _handle_rotate_key(
    key_id: str,
    request: RotateApiKeyRequest | None,
    db_path: Path,
    admin_token: str | None,
    authorization: str | None,
) -> dict:
    _require_admin(authorization, admin_token)
    expires_in_days = request.expires_in_days if request else None
    replacement = rotate_api_key(db_path, key_id, expires_in_days=expires_in_days)
    if replacement is None:
        raise HTTPException(status_code=404, detail=f"No active API key to rotate: {key_id}")
    return replacement


def _handle_list_keys(db_path: Path, admin_token: str | None, authorization: str | None) -> dict:
    _require_admin(authorization, admin_token)
    keys = [_api_key_record_payload(record) for record in list_api_keys(db_path)]
    return {"count": len(keys), "keys": keys}


def _handle_revoke_key(
    key_id: str, db_path: Path, admin_token: str | None, authorization: str | None
) -> dict:
    _require_admin(authorization, admin_token)
    record = revoke_api_key(db_path, key_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"Unknown API key: {key_id}")
    return _api_key_record_payload(record)


def _handle_moderate_case(
    request: ModerationRequest,
    db_path: Path,
    upload_dir: Path,
    authorization: str | None,
    daily_limit: int | None = None,
) -> dict:
    api_key = _require_api_key(authorization, db_path)
    allowed, used = consume_daily_budget(db_path, limit=daily_limit)
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail=(
                f"Daily moderation budget exhausted ({used} runs today). "
                "Raise SENTINEL_DAILY_LIVE_RUN_LIMIT or retry after UTC midnight."
            ),
            headers={"Retry-After": str(seconds_until_utc_midnight())},
        )
    if request.callback_url:
        # Validate before running the case: rejecting a bad callback after the
        # (paid) moderation run would bill the caller for a result they asked
        # to receive somewhere unreachable.
        rejection = validate_callback_url(request.callback_url)
        if rejection:
            raise HTTPException(status_code=422, detail=rejection)
    case = _build_case_from_request(request, upload_dir, api_key)
    result = run_case(case, db_path=db_path)
    payload = _result_payload(result)
    if request.callback_url:
        delivered = deliver_webhook(request.callback_url, payload)
        payload["integration"]["webhook"] = {"url": request.callback_url, "delivered": delivered}
    return payload


def _handle_get_moderation_logs(
    db_path: Path,
    authorization: str | None,
    escalated: bool | None,
) -> dict:
    api_key = _require_api_key(authorization, db_path)
    logs = list_moderation_logs(db_path, tenant_name=api_key.tenant_name)
    if escalated is not None:
        logs = [log for log in logs if log.escalation_triggered is escalated]
    return {"count": len(logs), "logs": [_log_payload(log) for log in logs]}


def _handle_get_case_logs(case_id: str, db_path: Path, authorization: str | None) -> dict:
    api_key = _require_api_key(authorization, db_path)
    logs = [
        log
        for log in list_moderation_logs(db_path, tenant_name=api_key.tenant_name)
        if log.case_id == case_id
    ]
    if not logs:
        raise HTTPException(status_code=404, detail=f"No moderation logs found for case_id={case_id}")
    return {"count": len(logs), "logs": [_log_payload(log) for log in logs]}


# ---------------------------------------------------------------------------
# App factory — binds handlers to routes
# ---------------------------------------------------------------------------

def _init_error_tracking() -> None:
    """Initialise Sentry when SENTRY_DSN is configured; a no-op otherwise.

    sentry-sdk is an optional dependency: a deployment opts in by installing it
    and setting the DSN. Missing either half must never break the API.
    """
    dsn = os.getenv("SENTRY_DSN", "").strip()
    if not dsn:
        return
    try:
        import sentry_sdk
    except ImportError:
        logger.warning("SENTRY_DSN is set but sentry-sdk is not installed; error tracking disabled")
        return
    sentry_sdk.init(dsn=dsn, traces_sample_rate=float(os.getenv("SENTRY_TRACES_SAMPLE_RATE", "0")))
    logger.info("Sentry error tracking enabled")


def create_app(
    db_path: str | Path = DEFAULT_DB_PATH,
    upload_dir: str | Path = UPLOADS_DIR / "api",
    admin_token: str | None = None,
    rate_limit_per_minute: int | None = None,
    admin_rate_limit_per_minute: int | None = None,
    daily_live_run_limit: int | None = None,
) -> FastAPI:
    resolved_db_path = Path(db_path)
    resolved_upload_dir = Path(upload_dir)
    resolved_admin_token = admin_token if admin_token is not None else os.getenv("SENTINEL_ADMIN_TOKEN")
    init_db(resolved_db_path)
    _init_error_tracking()
    global_limiter = RateLimiter(
        RATE_LIMIT_PER_MINUTE if rate_limit_per_minute is None else rate_limit_per_minute
    )
    admin_limiter = RateLimiter(
        ADMIN_RATE_LIMIT_PER_MINUTE if admin_rate_limit_per_minute is None else admin_rate_limit_per_minute
    )

    app = FastAPI(
        title="Sentinel Autonomous Moderation API",
        version=API_VERSION,
        description=(
            "Vendor-neutral moderation API for autonomous enforcement and ticketing-tool "
            "integration with Jira or in-house queues."
        ),
    )

    @app.middleware("http")
    async def rate_limit(request: Request, call_next):
        """Per-client fixed-window limits; the admin routes get a stricter bucket.

        Keyed on the direct peer address only — see rate_limit.py for why
        X-Forwarded-For is deliberately not honoured.
        """
        from fastapi.responses import JSONResponse

        client_key = request.client.host if request.client else "unknown"
        limiter = admin_limiter if request.url.path.startswith("/admin") else global_limiter
        allowed, retry_after = limiter.check(client_key)
        if not allowed:
            logger.warning(
                "Rate limit exceeded: client=%s path=%s (limit=%s/min)",
                client_key,
                request.url.path,
                limiter.limit,
            )
            return JSONResponse(
                {"detail": "Rate limit exceeded. Retry later."},
                status_code=429,
                headers={"Retry-After": str(retry_after)},
            )
        return await call_next(request)

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        """Attach a correlation ID to every request and log its outcome.

        An inbound X-Request-ID is honoured so callers can stitch Sentinel's
        logs into their own traces; otherwise one is generated.
        """
        request_id = request.headers.get("X-Request-ID", "").strip() or uuid.uuid4().hex[:16]
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            logger.exception("request_id=%s %s %s raised", request_id, request.method, request.url.path)
            raise
        elapsed_ms = round((time.perf_counter() - started) * 1000)
        response.headers["X-Request-ID"] = request_id
        logger.info(
            "request_id=%s %s %s -> %s (%d ms)",
            request_id,
            request.method,
            request.url.path,
            response.status_code,
            elapsed_ms,
        )
        return response

    @app.get("/health")
    def health() -> Any:
        from fastapi.responses import JSONResponse

        payload, status_code = _handle_health(resolved_db_path)
        return JSONResponse(payload, status_code=status_code)

    @app.post("/admin/api-keys", status_code=201)
    def create_key(request: CreateApiKeyRequest, authorization: str | None = Header(default=None)) -> dict:
        return _handle_create_key(request, resolved_db_path, resolved_admin_token, authorization)

    @app.get("/admin/api-keys")
    def list_keys(authorization: str | None = Header(default=None)) -> dict:
        return _handle_list_keys(resolved_db_path, resolved_admin_token, authorization)

    @app.post("/admin/api-keys/{key_id}/revoke")
    def revoke_key(key_id: str, authorization: str | None = Header(default=None)) -> dict:
        return _handle_revoke_key(key_id, resolved_db_path, resolved_admin_token, authorization)

    @app.post("/admin/api-keys/{key_id}/rotate", status_code=201)
    def rotate_key(
        key_id: str,
        request: RotateApiKeyRequest | None = None,
        authorization: str | None = Header(default=None),
    ) -> dict:
        return _handle_rotate_key(key_id, request, resolved_db_path, resolved_admin_token, authorization)

    @app.post("/moderation/cases", status_code=201)
    def moderate_case(request: ModerationRequest, authorization: str | None = Header(default=None)) -> dict:
        return _handle_moderate_case(
            request, resolved_db_path, resolved_upload_dir, authorization, daily_limit=daily_live_run_limit
        )

    @app.get("/moderation/logs")
    def get_moderation_logs(
        escalated: bool | None = None,
        authorization: str | None = Header(default=None),
    ) -> dict:
        return _handle_get_moderation_logs(resolved_db_path, authorization, escalated)

    @app.get("/moderation/logs/{case_id}")
    def get_case_logs(case_id: str, authorization: str | None = Header(default=None)) -> dict:
        return _handle_get_case_logs(case_id, resolved_db_path, authorization)

    return app


def _build_case_from_request(request: ModerationRequest, upload_dir: Path, api_key: ApiKeyRecord) -> Case:
    case_id = safe_upload_name(request.case_id or f"api-{uuid.uuid4().hex[:12]}")
    payload = _decode_payload(request)
    submission_id = uuid.uuid4().hex
    submission_dir = upload_dir / api_key.key_id / submission_id
    submission_dir.mkdir(parents=True, exist_ok=False)
    filename = safe_upload_name(request.filename or f"{case_id}{_default_suffix(request.asset_type)}")
    asset_path = submission_dir / filename
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
            "submission_id": submission_id,
            "upload_filename": filename,
            "upload_content_type": _content_type_for_request(request, filename),
        }
    )
    return Case(id=case_id, asset_type=request.asset_type, asset_path=str(asset_path), metadata=metadata)


def _decode_payload(request: ModerationRequest) -> bytes:
    """Decode the submitted asset, rejecting anything over the size ceiling.

    Enforced here rather than at the write because this is the one point every
    submission passes through, and the check has to happen before the bytes
    reach disk. MAX_TEXT_CHARS only bounds what is sent to the model; without
    this, an oversized upload is stored in full and still costs an API call.
    """
    if request.content_base64:
        # Base64 inflates by 4/3, so the encoded length bounds the decoded size
        # without allocating the decode first.
        if (len(request.content_base64) // 4) * 3 > MAX_UPLOAD_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"Upload exceeds the maximum of {MAX_UPLOAD_BYTES} bytes",
            )
        try:
            payload = base64.b64decode(request.content_base64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise HTTPException(status_code=400, detail="content_base64 must be valid base64") from exc
    elif request.content is not None:
        payload = request.content.encode("utf-8")
    else:
        raise HTTPException(status_code=400, detail="Provide either content or content_base64")

    if len(payload) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Upload exceeds the maximum of {MAX_UPLOAD_BYTES} bytes",
        )
    return payload


def _default_suffix(asset_type: AssetType) -> str:
    if asset_type == "image":
        return ".png"
    if asset_type == "audio":
        return ".wav"
    if asset_type == "video":
        return ".mp4"
    return ".txt"


def _content_type_for_request(request: ModerationRequest, filename: str) -> str:
    if request.content_type:
        return request.content_type
    guessed, _ = mimetypes.guess_type(filename)
    if guessed:
        return guessed
    return {
        "image": "image/png",
        "audio": "audio/wav",
        "video": "video/mp4",
        "text": "text/plain",
    }[request.asset_type]


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
        "observability": _observability_payload(result),
    }


def _observability_payload(result: CaseResult) -> dict:
    trace_id = result.case.metadata.get("openai_trace_id")
    return {
        "openai_trace_id": trace_id,
        "openai_trace_url": openai_trace_url(trace_id) if trace_id else None,
        "latency_ms": result.case.metadata.get("latency_ms"),
        "token_usage": result.case.metadata.get("token_usage"),
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


# Honour SENTINEL_DB_PATH for the uvicorn entrypoint (containers point it at a
# volume); create_app's explicit argument still wins everywhere else (tests).
app = create_app(db_path=load_settings().db_path)
