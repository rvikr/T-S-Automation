# Sentinel

Sentinel is a synthetic, multimodal Trust and Safety moderation demo. It routes image, audio, video, and text uploads to specialist agents, grounds decisions in a compact policy corpus, escalates ambiguity to a senior reviewer, forces Tier-1 synthetic stand-ins into human review and quarantine, and exposes autonomous enforcement decisions through an API.

No real illegal content is included. Tier-1 fixtures are benign placeholder labels used only to verify routing.

## Architecture

```mermaid
flowchart LR
  Upload["Upload / synthetic case"] --> Router["Orchestrator"]
  Router --> Image["Image agent"]
  Router --> Audio["Audio agent"]
  Router --> Video["Video agent"]
  Router --> Text["Text agent"]
  Image --> Policy["Policy retrieval"]
  Audio --> Policy
  Video --> Policy
  Text --> Policy
  Image --> Precedents["Precedent memory"]
  Audio --> Precedents
  Video --> Precedents
  Text --> Precedents
  Image --> Guardrail["Tier-1 guardrail"]
  Audio --> Guardrail
  Video --> Guardrail
  Text --> Guardrail
  Guardrail -->|Tier 1| Quarantine["Quarantine"]
  Quarantine --> Ticket["Human review ticket"]
  Guardrail -->|Ambiguous| Senior["Senior reviewer"]
  Senior --> Ticket
  Senior --> Precedents
  Guardrail -->|Clear reject| Reject["Reject with cited clause"]
  Guardrail -->|Clear allow| Allow["Allow"]
  Reject --> Audit["SQLite audit log"]
  Allow --> Audit
  Senior --> Audit
  Ticket --> Audit
  Audit --> Logs["Moderation logs"]
  Upload --> API["Autonomous moderation API"]
```

## Setup

```powershell
cd sentinel
python -m pip install -r requirements.txt
```

Create `.env.local` at the repository root with `OPENAI_API_KEY=...`. The deterministic demo path runs without live model calls, but the SDK-backed agent definitions and future live calls use that key.

## CLI Demo

```powershell
python sentinel/main.py --reset-db --clear-precedents --repeat 2
python sentinel/main.py --case-id img-gore-001
python sentinel/main.py --case-id tier1-child-standin-001
```

The repeated batch reports a non-Tier-1 escalation rate. Tier-1 cases are excluded from that learning metric because they must always escalate.

## Streamlit Demo

```powershell
streamlit run sentinel/app.py
```

The landing page shows production upload moderation, synthetic regression cases, verdicts, policy clauses, traces, and a batch-twice learning metric. The human-ticket queue is no longer shown on the landing page; click **Logs** in the sidebar to inspect moderation logs, escalation status, and escalation details.

## API

```powershell
$env:SENTINEL_ADMIN_TOKEN="replace-with-a-long-random-admin-secret"
uvicorn sentinel.api:app --reload
```

Generate a customer/project API key with the admin token:

```powershell
Invoke-RestMethod `
  -Method Post `
  -Uri "http://127.0.0.1:8000/admin/api-keys" `
  -Headers @{ Authorization = "Bearer $env:SENTINEL_ADMIN_TOKEN" } `
  -ContentType "application/json" `
  -Body '{"tenant_name":"Example Platform","project_name":"Production Moderation","environment":"live"}'
```

The response includes an `api_key` such as `sent_live_...`. Sentinel shows that raw key once and stores only its hash. Use the key from the customer platform or ticketing workflow:

```powershell
Invoke-RestMethod `
  -Method Post `
  -Uri "http://127.0.0.1:8000/moderation/cases" `
  -Headers @{ Authorization = "Bearer sent_live_replace_with_generated_key" } `
  -ContentType "application/json" `
  -Body '{"case_id":"ZD-123","asset_type":"text","content":"content to moderate","source_system":"zendesk","external_reference":"ZD-123"}'
```

Use `POST /moderation/cases` to submit text or base64-encoded media for autonomous moderation. The response includes the final verdict, enforcement action (`allow`, `reject`, or `escalate`), whether escalation was triggered, and a normalized `ticketing_payload` that can be mapped into Jira, ServiceNow, Zendesk, or an in-house ticketing tool.

Use `GET /moderation/logs` with the same bearer API key to list that tenant's decisions with escalation status and details. Use `GET /moderation/logs?escalated=true` for only escalated cases, or `GET /moderation/logs/{case_id}` for one external case.

Admin operators can inspect key metadata with `GET /admin/api-keys` and revoke a key with `POST /admin/api-keys/{key_id}/revoke`. Revoked keys immediately receive `401` on moderation and logs endpoints.

## Acceptance Notes

- Router dispatch is implemented in `agents/orchestrator.py`.
- Reject warnings cite exact clause IDs such as `CIV-VCG-001`.
- Ambiguous Tier-2 cases hand off to the senior reviewer.
- Tier-1 synthetic stand-ins always hash-match, quarantine, create a human ticket, and bypass automated adjudication.
- Senior resolutions are stored in precedent memory so repeated non-Tier-1 batches escalate less often.
- Every final decision is written to SQLite and exposed through moderation logs.
- External platforms can integrate with the API without adopting Sentinel's local ticket store; ticketing tools consume the normalized ticket payload and keep their own issue IDs.
- API keys are tenant-scoped, stored hashed, returned only once at creation time, and required for moderation and log endpoints.
