# Sentinel Accuracy & Feedback Loop Plan

## Top-Level Overview

Three connected improvements are in scope:

1. **Automated accuracy improvement loop** — a scheduled background eval runner that executes the golden-set harness in live mode on a nightly cadence and writes results to `eval_runs/`, plus an on-demand admin API endpoint (`POST /admin/eval/run`) for post-deploy verification. When accuracy drops below a configurable threshold, the result is flagged in the run report and logged at WARNING level.

2. **Human-in-the-loop feedback loop** — a `POST /moderation/tickets/{ticket_id}/resolve` API endpoint that lets any human reviewer (Sentinel UI or direct API call) submit a final decision and rationale, which is immediately written to the precedent store. A companion `POST /webhooks/jira` listener closes the same loop for reviewers working in Jira: when a Jira issue transitions to a terminal done/resolved state, Sentinel reads the native Jira resolution field (`"Won't Do"` / `"Won't Fix"` → `allow`; all other resolutions → `reject`) and writes the precedent automatically.

3. **Tier-1 detection seam documentation** — no code change to Tier-1 routing (synthetic stand-ins remain correct for now). The existing `known_hash_match()` seam in `sentinel/tools/hash_match.py` is documented with a clear integration guide so a future engineer knows exactly how to plug in PDQ perceptual hashing or NCMEC hash database lookup when the platform moves to production content.

No changes are made to the Tier-1 safety rails, the agent policy logic, verdict semantics, or the golden-set fixtures themselves.

---

## Sub-Task 1 — Scheduled Eval Runner (Background Loop + On-Demand Endpoint)

### Intent
Give operators continuous visibility into pipeline accuracy. The eval harness already exists and produces timestamped run directories; what is missing is an automatic trigger and an alert when the score falls below the acceptable floor.

### Expected Outcomes
- A new config constant `SENTINEL_EVAL_ACCURACY_FLOOR` (default `0.88`) — when a completed run's accuracy falls below this value, the run is flagged and a `WARNING` log is emitted.
- A new config constant `SENTINEL_EVAL_SCHEDULE_HOUR` (default `2`) — the UTC hour at which the nightly background eval fires (0–23).
- A new module `sentinel/eval/scheduler.py` that:
  - Exposes a `start_eval_scheduler()` function — starts a `threading.Thread` that sleeps until the next scheduled UTC hour, runs `run_golden_set(live=True)` (live mode — real agent calls against the text golden cases, same as the reference run), calls `compute_metrics()` and `write_report()`, then checks the accuracy floor and logs a warning if breached.
  - The thread is a daemon thread so it does not block process shutdown.
  - Only one scheduler thread runs at a time (guarded by a module-level `threading.Event`).
- `sentinel/app.py` calls `start_eval_scheduler()` at startup (after the Streamlit page config, before any UI rendering) so the background loop runs whenever the app is live.
- A new admin API route `POST /admin/eval/run` in `sentinel/api.py` that:
  - Requires the admin token (same auth as existing admin routes).
  - Accepts an optional JSON body `{"mode": "offline" | "live", "live_all": false}` (defaults to `"live"`).
  - Runs the eval synchronously (acceptable because admin-only, not in the hot path).
  - Returns JSON: `{"run_dir": "...", "accuracy": 0.xx, "tier1_recall": 1.0, "benign_fpr": 0.0, "cases": 18, "below_floor": false}`.
  - Logs a WARNING if accuracy is below the floor.

### Todo List
1. In `sentinel/config.py`, add:
   - `EVAL_ACCURACY_FLOOR = float(os.getenv("SENTINEL_EVAL_ACCURACY_FLOOR", "0.88"))`
   - `EVAL_SCHEDULE_HOUR = int(os.getenv("SENTINEL_EVAL_SCHEDULE_HOUR", "2"))`
2. Create `sentinel/eval/scheduler.py` with `start_eval_scheduler()` function:
   - Uses `threading.Thread(target=_eval_loop, daemon=True)`.
   - `_eval_loop()` calculates seconds until next `EVAL_SCHEDULE_HOUR` UTC, sleeps, then calls the eval harness in live mode: `run_golden_set(live=True)`.
   - After each run, checks `metrics["accuracy"]` against `EVAL_ACCURACY_FLOOR`; if below, calls `logger.warning("Eval accuracy %.1%% below floor %.1%%", ...)`.
   - Loops indefinitely (sleep → run → sleep …).
3. In `sentinel/app.py`, import `start_eval_scheduler` from `sentinel.eval.scheduler` and call it once at startup (wrap in `if __name__ != "__main__"` guard to avoid double-start under Streamlit's reloader).
4. In `sentinel/api.py`, add `POST /admin/eval/run`:
   - Define a `EvalRunRequest` Pydantic model with `mode: str = "live"` and `live_all: bool = False`.
   - Define a `_handle_run_eval()` handler function (following the existing handler pattern).
   - The handler calls `run_golden_set(live=mode=="live", live_all=live_all)`, then `compute_metrics()`, then `write_report()`.
   - Returns the summary dict described above.
   - Register the route in `create_app()` alongside the other admin routes.
5. Write `sentinel/tests/test_eval_scheduler.py`:
   - Test that `start_eval_scheduler()` starts a daemon thread (assert `thread.daemon == True`).
   - Test that the accuracy floor warning is logged when `metrics["accuracy"]` is below `EVAL_ACCURACY_FLOOR` (mock `run_golden_set` and `compute_metrics`).
   - Test the `POST /admin/eval/run` endpoint with the test client (mock `run_golden_set` to return a minimal fixture so no real agent calls are made).

### Relevant Context
- `sentinel/eval/run_eval.py` — `run_golden_set()`, `compute_metrics()`, `write_report()` — all reusable as-is
- `sentinel/config.py` — add two new constants following the existing `int(os.getenv(...))` pattern
- `sentinel/app.py` — Streamlit startup; existing pattern is `st.set_page_config(...)` at the top
- `sentinel/api.py` — `_handle_metrics()`, `_handle_create_key()` are the reference pattern for admin handler functions
- `sentinel/tests/conftest.py` — single autouse fixture scrubs Jira/OpenAI; new test must monkeypatch `run_golden_set`
- `sentinel/eval_runs/` — output directory; existing naming convention `YYYYMMDDTHHMMSSz-{mode}`

### Status
[x] done

---

## Sub-Task 2 — Human Feedback API: Ticket Resolve Endpoint

### Intent
Close the loop between a human reviewer's decision and the precedent memory store. Today, when a human resolves a Jira ticket, that decision is never written back to Sentinel's precedent DB, so future agents cannot learn from it. This sub-task adds the direct API endpoint half of the feedback loop.

### Expected Outcomes
- A new route `POST /moderation/tickets/{ticket_id}/resolve` in `sentinel/api.py`:
  - Requires the caller's API key (standard `Authorization: Bearer <key>` header, same as `/moderation/cases`).
  - Accepts JSON body: `{"decision": "allow" | "reject", "rationale": "...", "category": "..."}`.
  - Looks up the ticket in the `tickets` table to get `case_id` and `category`.
  - Calls `write_precedent()` with a synthetic `Case` (reconstructed from ticket data) and a `Verdict` with `reviewer="human"`.
  - Updates the ticket's `status` to `"resolved"` in the `tickets` table.
  - Returns `{"ticket_id": "...", "precedent_written": true, "decision": "..."}`.
  - If the ticket does not exist → 404. If already resolved → 409 (idempotency guard).
  - Tier-1 tickets are rejected with 403: `"Tier-1 verdicts cannot be resolved via API"`.
- A new helper `resolve_ticket()` in `sentinel/tools/ticketing.py` that handles the DB update (status → "resolved") and `write_precedent()` call — keeps the API handler thin.

### Todo List
1. In `sentinel/tools/audit_log.py`, add a `get_ticket(ticket_id, db_path)` function that fetches one ticket row by `id` and returns a `Ticket` or `None`.
2. In `sentinel/tools/ticketing.py`, add `resolve_ticket(ticket_id, decision, rationale, category, db_path)`:
   - Calls `get_ticket()` — returns `None` if not found, raises `ValueError` if already resolved.
   - Refuses Tier-1 categories (raises `ValueError`).
   - Reconstructs a minimal `Case(id=ticket.case_id, asset_type="unknown", asset_path="", metadata={})`.
   - Builds a `Verdict` via `build_verdict(case_id=ticket.case_id, decision=decision, category=category, confidence=1.0, rationale=rationale, reviewer="human")`.
   - Calls `write_precedent(case, verdict, db_path)`.
   - Updates `tickets.status = "resolved"` in the DB.
   - Returns the updated `Ticket`.
3. In `sentinel/api.py`, define `ResolveTicketRequest` Pydantic model and `_handle_resolve_ticket()` handler:
   - Uses `get_ticket()` for the 404 check.
   - Calls `resolve_ticket()`.
   - Maps `ValueError` to 403 (Tier-1) or 409 (already resolved).
4. Register `POST /moderation/tickets/{ticket_id}/resolve` in `create_app()`.
5. Write `sentinel/tests/test_feedback_loop.py`:
   - Test successful resolve: POST with valid decision → ticket status updated, precedent written.
   - Test 404 for unknown ticket ID.
   - Test 409 for double-resolve.
   - Test 403 for Tier-1 category.
   - All tests use a temp SQLite DB; no real API calls.

### Relevant Context
- `sentinel/tools/precedent_memory.py` — `write_precedent(case, verdict, db_path)` — the gate is `reviewer in {"senior", "human"}` and `category not in TIER1_CATEGORIES`
- `sentinel/tools/ticketing.py` — `create_human_ticket()` — reference pattern for ticket DB writes
- `sentinel/tools/audit_log.py` — `db_connection()`, `init_db()` — use for `get_ticket()`
- `sentinel/models.py` — `build_verdict()`, `Ticket`, `Case`, `Precedent`
- `sentinel/api.py` — `_handle_get_case_logs()` uses `Authorization` header; follow same auth pattern
- `sentinel/tools/policy_retrieval.py` — `TIER1_CATEGORIES` for the Tier-1 guard

### Status
[x] done

---

## Sub-Task 3 — Jira Webhook Listener (Feedback Loop, Second Half)

### Intent
Reviewers working in Jira should not need to also open the Sentinel UI to close the feedback loop. When a Jira issue created by Sentinel transitions to a done/resolved state, Jira can POST a webhook event to Sentinel, which then calls the same `resolve_ticket()` logic added in Sub-Task 2.

### Expected Outcomes
- A new route `POST /webhooks/jira` in `sentinel/api.py`:
  - Does **not** require an API key — it is authenticated by HMAC-SHA256 signature or a shared secret in the `X-Hub-Signature` header (Jira supports this via "Secret" field on outgoing webhooks).
  - Parses the Jira webhook payload to extract: `issue.key` (the external Jira key), the transition (`changelog.items[*].field == "status"` where `toString in {"Done", "Resolved"}`), and the resolution field (`issue.fields.resolution.name`).
  - Looks up the Sentinel ticket by `external_key = issue.key` (requires one new DB query helper).
  - If the transition is to a terminal done state, calls `resolve_ticket()` with `decision` inferred from the resolution field: `"Won't Do"` and `"Won't Fix"` → `allow` (content was benign); all other resolution values (including `"Done"`, `"Fixed"`, `"Duplicate"`, or `None`) → `reject`. No special comment format is required — the reviewer just resolves the issue normally in Jira.
  - Returns `{"status": "accepted"}` on success, `{"status": "skipped", "reason": "..."}` when the event is not a terminal transition.
  - Signature verification: if `SENTINEL_JIRA_WEBHOOK_SECRET` env var is set, verify the `X-Hub-Signature` header; if not set, accept without verification (dev mode).
- New config constant `SENTINEL_JIRA_WEBHOOK_SECRET = os.getenv("SENTINEL_JIRA_WEBHOOK_SECRET", "")`.
- New helper `get_ticket_by_external_key(external_key, db_path)` in `sentinel/tools/audit_log.py`.

### Todo List
1. In `sentinel/config.py`, add `JIRA_WEBHOOK_SECRET = os.getenv("SENTINEL_JIRA_WEBHOOK_SECRET", "")`.
2. In `sentinel/tools/audit_log.py`, add `get_ticket_by_external_key(external_key, db_path) -> Ticket | None` that queries `tickets WHERE external_key = ?`.
3. In `sentinel/api.py`:
   - Add `_verify_jira_signature(raw_body, signature_header, secret)` helper — HMAC-SHA256 comparison using `hmac.compare_digest`.
   - Add `_parse_jira_event(payload)` helper — extracts `(external_key, is_terminal, decision, rationale)` from the webhook JSON. Terminal = transition to "Done" or "Resolved" status. Decision = `"allow"` if `issue.fields.resolution.name` is `"Won't Do"` or `"Won't Fix"`, otherwise `"reject"`. Rationale = `issue.fields.resolution.name` or `"Resolved in Jira"` as fallback.
   - Add `_handle_jira_webhook()` handler function.
   - Register `POST /webhooks/jira` in `create_app()`.
4. Write `sentinel/tests/test_jira_webhook.py`:
   - Test that a well-formed "issue_generic" done-transition payload triggers `resolve_ticket()`.
   - Test that a non-terminal transition returns `{"status": "skipped"}`.
   - Test that a missing/invalid signature (when secret is set) returns 401.
   - Test that an unknown `external_key` returns `{"status": "skipped", "reason": "ticket not found"}`.
   - All tests use a temp SQLite DB pre-seeded with a ticket row.

### Relevant Context
- `sentinel/tools/ticketing.py` — `resolve_ticket()` added in Sub-Task 2
- `sentinel/tools/audit_log.py` — `get_ticket()` added in Sub-Task 2; `get_ticket_by_external_key()` added here
- `sentinel/tools/webhook.py` — `sign_payload()` uses the same HMAC-SHA256 pattern; reuse `hmac.compare_digest`
- `sentinel/tools/jira_client.py` — `attach_external_reference()` stores `external_key` in the tickets table
- `sentinel/config.py` — follow existing `os.getenv(...)` pattern
- `sentinel/api.py` — existing `/webhooks/` route group can be registered separately in `create_app()`
- Jira webhook docs: outgoing webhook payload shape is `{"issue": {"key": "...", "fields": {...}}, "changelog": {...}, "issue_event_type_name": "..."}`

### Status
[x] done

---

## Sub-Task 4 — Tier-1 Detection Seam Documentation

### Intent
The SHA-256 hash list is the right architecture for today (synthetic stand-ins). Document exactly what an engineer would need to change to add perceptual hashing (PDQ) or NCMEC database lookup in the future, without touching any safety logic now.

### Expected Outcomes
- `sentinel/tools/hash_match.py` has an expanded docstring on `known_hash_match()` explaining:
  - **Current behaviour:** exact SHA-256 match against `data/known_hashes.txt`.
  - **Short-term (no code change):** add real SHA-256 hashes of known-violation files to `known_hashes.txt`; the pipeline already handles it.
  - **Medium-term (PDQ):** replace or wrap `file_sha256()` with a PDQ perceptual hash call (`pdqhash` PyPI package); store PDQ hashes in `known_hashes.txt` with a `pdq:` prefix; update the comparison logic in `known_hash_match()` accordingly. No external service required.
  - **Long-term (NCMEC / PhotoDNA):** register as an Electronic Service Provider (ESP) with NCMEC; replace the local list lookup with an HTTP call to the NCMEC hash-check API. The function signature stays the same — only the body changes. Azure Content Safety (PhotoDNA) is the alternative for image-only perceptual matching with no ESP registration required.
- `sentinel/policy/corpus.md` has a new section `## Tier-1 Detection Seam` summarising the same integration ladder.
- No functional code changes; no test changes.

### Todo List
1. Expand the `known_hash_match()` docstring in `sentinel/tools/hash_match.py` with the three-level integration guide (Short-term / Medium-term / Long-term) described above.
2. Add a `## Tier-1 Detection Seam` section to `sentinel/policy/corpus.md` with a concise version of the same guide.
3. Add a comment in `sentinel/data/known_hashes.txt` (create the file if it does not exist) with the format specification: one SHA-256 hex digest per line, lines starting with `#` are comments, blank lines ignored.

### Relevant Context
- `sentinel/tools/hash_match.py:30-47` — `known_hash_match()` function and its existing "integration seam" comment
- `sentinel/policy/corpus.md` — human-readable policy mirror; Tier-1 section already describes routing
- `sentinel/data/known_hashes.txt` — path referenced by `KNOWN_HASHES_PATH = DATA_DIR / "known_hashes.txt"`

### Status
[x] done

---

## Implementation Order

```
Sub-Task 1 (Eval loop + API)
        ↓
Sub-Task 2 (Resolve endpoint + precedent write)
        ↓
Sub-Task 3 (Jira webhook listener — depends on resolve_ticket from Sub-Task 2)
        ↓
Sub-Task 4 (Documentation only — can be done any time)
```

Sub-Tasks 1 and 4 are independent of each other and can be parallelised.
Sub-Task 3 hard-depends on the `resolve_ticket()` helper introduced in Sub-Task 2.
