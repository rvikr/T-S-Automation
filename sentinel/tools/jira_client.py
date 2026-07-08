"""Jira Cloud escalation for human-review tickets.

Enabled when JIRA_BASE_URL, JIRA_EMAIL, JIRA_API_TOKEN, and JIRA_PROJECT_KEY
are set (e.g. in .env.local). The local SQLite ticket is always created first
by the orchestrator; Jira is enrichment — any failure here degrades to
local-only ticketing and never loses an escalation.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import requests

from sentinel.config import load_settings
from sentinel.models import Case, Ticket, Verdict


REQUEST_TIMEOUT_SECONDS = 10

PRIORITY_BY_TIER = {1: "Highest", 2: "High"}
DEFAULT_PRIORITY = "Medium"


@dataclass(frozen=True)
class JiraConfig:
    base_url: str
    email: str
    api_token: str
    project_key: str


def load_jira_config() -> JiraConfig | None:
    load_settings()  # ensures .env.local / .env are loaded into the environment
    base_url = os.getenv("JIRA_BASE_URL", "").strip().rstrip("/")
    email = os.getenv("JIRA_EMAIL", "").strip()
    api_token = os.getenv("JIRA_API_TOKEN", "").strip()
    project_key = os.getenv("JIRA_PROJECT_KEY", "").strip()
    if not (base_url and email and api_token and project_key):
        return None
    return JiraConfig(base_url=base_url, email=email, api_token=api_token, project_key=project_key)


def jira_enabled() -> bool:
    return load_jira_config() is not None


def create_jira_issue(ticket: Ticket, case: Case, verdict: Verdict) -> tuple[str, str] | None:
    """Create a Jira issue for an escalated case. Returns (issue_key, browse_url) or None."""
    config = load_jira_config()
    if config is None:
        return None

    fields: dict[str, Any] = {
        "project": {"key": config.project_key},
        "issuetype": {"name": "Task"},
        "summary": f"[Sentinel] Tier-{ticket.severity} escalation: {ticket.category} (case {case.id})",
        "description": _adf_description(ticket, case, verdict),
        "labels": _labels(ticket, case),
        "priority": {"name": PRIORITY_BY_TIER.get(ticket.severity, DEFAULT_PRIORITY)},
    }
    response = _post_issue(config, fields)
    if response is None:
        return None
    if response.status_code == 400 and "priority" in response.text.lower():
        # Team-managed projects often exclude priority from the create screen.
        fields.pop("priority", None)
        response = _post_issue(config, fields)
        if response is None:
            return None
    if response.status_code not in (200, 201):
        return None
    key = str(response.json().get("key", "")).strip()
    if not key:
        return None
    return key, f"{config.base_url}/browse/{key}"


def _post_issue(config: JiraConfig, fields: dict[str, Any]) -> requests.Response | None:
    try:
        return requests.post(
            f"{config.base_url}/rest/api/3/issue",
            json={"fields": fields},
            auth=(config.email, config.api_token),
            headers={"Accept": "application/json"},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except requests.RequestException:
        return None


def _labels(ticket: Ticket, case: Case) -> list[str]:
    labels = ["sentinel", f"tier-{ticket.severity}", case.asset_type]
    source = str(case.metadata.get("source_system", "")).strip().lower()
    if source:
        labels.append(source.replace(" ", "-"))
    return labels


def _adf_description(ticket: Ticket, case: Case, verdict: Verdict) -> dict[str, Any]:
    lines = [
        f"Sentinel escalated case {case.id} for human review.",
        f"Local ticket: {ticket.id}",
        f"Asset type: {case.asset_type}",
        f"Category: {verdict.category} (severity tier {verdict.severity_tier})",
        f"Policy clause: {verdict.policy_clause}",
        f"Automated decision: {verdict.decision} (confidence {verdict.confidence:.2f}, reviewer {verdict.reviewer})",
        f"Rationale: {verdict.rationale}",
    ]
    citations = case.metadata.get("cited_clauses") or []
    if citations:
        lines.append(f"Cited clauses: {', '.join(str(c) for c in citations)}")
    external_reference = str(case.metadata.get("external_reference", "")).strip()
    if external_reference:
        lines.append(f"External reference: {external_reference}")
    tenant = str(case.metadata.get("tenant_name", "")).strip()
    if tenant:
        lines.append(f"Tenant: {tenant}")
    lines.append("Content is quarantined; review it in Sentinel, not in this ticket.")
    return {
        "type": "doc",
        "version": 1,
        "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": line}]}
            for line in lines
        ],
    }
