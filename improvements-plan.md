# Sentinel Codebase Improvements Plan

## Top-Level Overview

This plan captures 8 targeted improvement areas for the Sentinel codebase. The goal is to improve maintainability, observability, and test confidence without altering core moderation logic or safety guarantees. All sub-tasks are ordered from highest-impact to lowest-impact and are designed to be implemented independently, one at a time.

No changes are made to the agent policy logic, verdict semantics, or safety rail invariants.

---

## Sub-Task 1 — Adopt Structured Logging (Replace `print` and Silent Swallows)

### Intent
Replace all `print()` calls and bare `except` blocks with Python's `logging` module. This gives operators control over verbosity and ensures failures leave a trace instead of disappearing silently.

### Expected Outcomes
- A single `logging` configuration entry point in `config.py` (log level controlled via `SENTINEL_LOG_LEVEL` env var, default `WARNING`).
- All `print()` statements in non-interactive modules replaced with appropriate log calls.
- All bare `except Exception: return/pass` blocks replaced with `logger.exception(...)` before returning the fallback value.
- CLI output in `main.py` and `run_eval.py` may keep `print()` since those are intentional user-facing outputs.

### Todo List
1. Add `SENTINEL_LOG_LEVEL` env var + `logging.basicConfig()` call to `sentinel/config.py`.
2. Add `logger = logging.getLogger(__name__)` to each affected module.
3. In `sentinel/tools/jira_client.py` lines 81–91: replace silent `except requests.RequestException: return None` with `logger.exception(...)` before returning.
4. In `sentinel/tools/jira_client.py` lines 65–74: add `logger.warning(...)` logging the status code and response body before returning `None` on non-201 responses.
5. In `sentinel/tools/production_analysis.py` line 110: add `logger.exception(...)` before returning fallback `ProductionAssessment`.
6. In `sentinel/tools/production_analysis.py` line 275: replace bare `except Exception: return ""` with `logger.exception(...)` before returning empty string.
7. In `sentinel/tools/production_analysis.py` lines 227–230: replace silent `ImportError` return with `logger.warning(...)` before returning `[]`.
8. In `sentinel/agents/senior_reviewer.py` lines 30–40: add `logger.exception(...)` before building fallback `Verdict`.
9. In `sentinel/tools/policy_index.py` line 97: replace `print(...)` with `logger.info(...)`.

### Relevant Context
- `sentinel/config.py` — add log level constant + `basicConfig()` here
- `sentinel/tools/jira_client.py:81-91` — `_post_issue()` silent swallow
- `sentinel/tools/jira_client.py:65-74` — `create_jira_issue()` non-201 silent return
- `sentinel/tools/production_analysis.py:108-119` — `analyze_asset()` broad except
- `sentinel/tools/production_analysis.py:275-276` — `extract_video_audio_transcript()` bare except
- `sentinel/tools/production_analysis.py:226-230` — `sample_video_frame_data_urls()` ImportError
- `sentinel/agents/senior_reviewer.py:28-40` — `_production_senior_review()` fallback Verdict
- `sentinel/tools/policy_index.py:97` — `print()` call
- **Do not** change `print()` calls in `sentinel/main.py` or `sentinel/eval/run_eval.py` — those are intentional CLI/user-facing outputs.

### Status
[x] done

---

## Sub-Task 2 — Extract Shared `_build_verdict()` Factory

### Intent
The pattern of (validate category → get clause → check Tier-1 → clamp confidence → construct `Verdict`) is duplicated across 3 files. Extracting it to a single factory in `models.py` removes the duplication and makes future policy logic changes a single-place edit.

### Expected Outcomes
- A new `build_verdict(decision, category, confidence, rationale, reviewer, initial_verdict=None)` function added to `sentinel/models.py`.
- The 3 existing verdict-construction call sites replaced with calls to `build_verdict()`.
- All existing tests continue to pass (no behavioural change).

### Todo List
1. In `sentinel/models.py`, add a `build_verdict()` function that:
   - Validates `category` against `POLICY_CLAUSES` (falls back to `"No Violation"`).
   - Calls `get_clause_for_category(category)` to retrieve clause.
   - Enforces Tier-1 → `decision="ambiguous"`, `confidence=max(0.95, ...)` invariant.
   - Clamps confidence: `max(0.0, min(float(confidence), 1.0))`.
   - Returns a `Verdict` dataclass instance.
2. In `sentinel/agents/common.py` lines 16–57: replace inline Verdict constructions with `build_verdict()` calls.
3. In `sentinel/agents/senior_reviewer.py` lines 42–66 and 70–99: replace inline Verdict constructions with `build_verdict()` calls.
4. In `sentinel/tools/production_analysis.py` lines 19–64: replace inline Verdict constructions with `build_verdict()` calls.
5. Run `pytest sentinel/tests -q` and confirm all 28 tests pass.

### Relevant Context
- `sentinel/models.py:24-33` — `Verdict` dataclass definition (fields: case_id, decision, severity_tier, category, policy_clause, confidence, rationale, reviewer)
- `sentinel/agents/common.py:16-57` — specialist verdict building (3 paths: precedent, tier-1, standard)
- `sentinel/agents/senior_reviewer.py:42-66` — production senior verdict building
- `sentinel/agents/senior_reviewer.py:70-99` — synthetic senior verdict building
- `sentinel/tools/production_analysis.py:19-64` — production specialist → Verdict builder
- `sentinel/tools/policy_retrieval.py` — `get_clause_for_category()`, `TIER1_CATEGORIES`, `POLICY_CLAUSES`

### Status
[x] done

---

## Sub-Task 3 — Centralise Hardcoded Constants in `config.py`

### Intent
Constants like `MAX_TEXT_CHARS`, `MAX_AGENT_TURNS`, agent `temperature`, Jira `REQUEST_TIMEOUT_SECONDS`, and model pricing are scattered and/or duplicated. Centralising them in `config.py` with env-var overrides makes the system configurable without code changes.

### Expected Outcomes
- All duplicated/scattered constants moved to `sentinel/config.py` with env-var override support.
- Each module imports its constants from `config.py` instead of defining them locally.
- `SEVERITY_TIERS_PATH` dead constant removed from `config.py`.
- All existing tests continue to pass.

### Todo List
1. Remove `SEVERITY_TIERS_PATH` from `sentinel/config.py` line 26 (dead code — never imported or used).
2. Add the following to `sentinel/config.py`:
   - `MAX_TEXT_CHARS = int(os.getenv("SENTINEL_MAX_TEXT_CHARS", "12000"))`
   - `MAX_AGENT_TURNS = int(os.getenv("SENTINEL_MAX_AGENT_TURNS", "10"))`
   - `MAX_VIDEO_FRAMES = int(os.getenv("SENTINEL_MAX_VIDEO_FRAMES", "4"))`
   - `AGENT_TEMPERATURE = float(os.getenv("SENTINEL_AGENT_TEMPERATURE", "0.1"))`
   - `JIRA_REQUEST_TIMEOUT = int(os.getenv("SENTINEL_JIRA_TIMEOUT", "10"))`
3. In `sentinel/agents/runtime.py`: remove `MAX_TEXT_CHARS = 12000` and `MAX_AGENT_TURNS = 10` (lines 34–35); import from `config`; replace `temperature=0.1` with `temperature=AGENT_TEMPERATURE`.
4. In `sentinel/tools/production_analysis.py`: remove `MAX_TEXT_CHARS = 12000` and `MAX_VIDEO_FRAMES = 4` (lines 15–16); import from `config`.
5. In `sentinel/tools/jira_client.py`: remove `REQUEST_TIMEOUT_SECONDS = 10` (line 21); import `JIRA_REQUEST_TIMEOUT` from `config` and use in HTTP calls.
6. In `sentinel/ui_uploads.py`: add a comment noting model prices are as of a specific date and are subject to change; keep the pricing dict as-is (no config move needed — it's presentational, not operational).

### Relevant Context
- `sentinel/config.py:26` — `SEVERITY_TIERS_PATH` (dead, remove)
- `sentinel/agents/runtime.py:34-35` — `MAX_TEXT_CHARS`, `MAX_AGENT_TURNS`
- `sentinel/agents/runtime.py:160,174` — `temperature=0.1` (two agent definitions)
- `sentinel/tools/production_analysis.py:15-16` — duplicate `MAX_TEXT_CHARS`, `MAX_VIDEO_FRAMES`
- `sentinel/tools/jira_client.py:21` — `REQUEST_TIMEOUT_SECONDS`
- `sentinel/ui_uploads.py:38-42` — model pricing (add comment only)

### Status
[x] done

---

## Sub-Task 4 — Decompose Long Functions

### Intent
Three functions are significantly longer than a single responsibility warrants. Breaking them into named helpers makes each path independently readable, testable, and editable.

### Expected Outcomes
- `_run_case_inner()` in `orchestrator.py` broken into focused helper functions for each decision path.
- `_prepare_input()` in `production_analysis.py` broken into per-modality helpers.
- `create_app()` in `api.py` broken into separate route-handler functions extracted out of the factory closure.
- All 28 existing tests continue to pass.

### Todo List
1. In `sentinel/agents/orchestrator.py` `_run_case_inner()` (lines 137–194), extract:
   - `_handle_tier1_guardrail(case, verdict, trace, db_path)` — quarantine, ticket, return
   - `_handle_injection_guardrail(case, trace, db_path)` — ticket, return
   - `_handle_ambiguous_escalation(case, initial_verdict, trace, db_path)` — senior run, conditional human ticket, return
   Keep `_run_case_inner()` as a thin dispatcher that calls these helpers in order.
2. In `sentinel/tools/production_analysis.py` `_prepare_input()` (lines 181–229), extract:
   - `_prepare_text_input(case)` → returns list of text input items
   - `_prepare_image_input(case)` → returns list of image input items (base64 data URL)
   - `_prepare_audio_input(case, client)` → returns list of audio input items (transcription)
   - `_prepare_video_input(case, client)` → returns list of video input items (frames + transcript)
   Keep `_prepare_input()` as a dispatcher that calls the correct helper based on `case.asset_type`.
3. In `sentinel/api.py` `create_app()` (lines 45–125), extract each route handler as a module-level function:
   - `handle_health()`, `handle_create_api_key()`, `handle_list_api_keys()`, `handle_revoke_api_key()`, `handle_moderate()`, `handle_get_logs()` (etc.)
   - Pass `db_path`, `upload_dir`, `admin_token` as parameters to each handler, or use FastAPI's `Depends()` injection.
   Keep `create_app()` as a thin factory that registers the extracted functions as routes.

### Relevant Context
- `sentinel/agents/orchestrator.py:137-194` — `_run_case_inner()` 58-line function
- `sentinel/tools/production_analysis.py:181-229` — `_prepare_input()` 49-line function
- `sentinel/api.py:45-125` — `create_app()` 80-line factory

### Status
[x] done

---

## Sub-Task 5 — Add Module Docstrings to All Public Modules

### Intent
Seven modules lack module-level docstrings, making it hard to understand a file's responsibility at a glance. Adding one-paragraph docstrings improves onboarding and tool-generated documentation.

### Expected Outcomes
- Each of the 7 listed modules has a module-level docstring as its first statement (after `from __future__` imports if present).
- Docstrings describe: what the module does, what it exports or registers, and any important design notes.

### Todo List
1. Add module docstring to `sentinel/api.py` — describe FastAPI app factory, route groups (admin, moderation, logs), auth mechanism.
2. Add module docstring to `sentinel/app.py` — describe Streamlit UI, tabs (live case, logs, metrics), live event streaming.
3. Add module docstring to `sentinel/models.py` — describe all exported dataclasses (Case, Verdict, ProductionAssessment, Ticket, Audit, ModerationLog, ApiKeyRecord) and the `Decision` type alias.
4. Add module docstring to `sentinel/tools/audit_log.py` — describe SQLite schema, schema migration helper, audit/ticket/precedent persistence.
5. Add module docstring to `sentinel/tools/api_keys.py` — describe API key lifecycle (generate, hash, validate, revoke), tenant scoping.
6. Add module docstring to `sentinel/tools/media_utils.py` — describe asset type detection, synthetic case loading, quarantine directory management.
7. Add module docstring to `sentinel/guardrails.py` — describe Tier-1 output guardrail, injection input guardrail, tripwire semantics, and the `INJECTION_PATTERNS` regex list purpose.
8. In `sentinel/guardrails.py`, add inline comments to each entry in `INJECTION_PATTERNS` explaining what attack pattern it catches.

### Relevant Context
- `sentinel/api.py:1-21` — no docstring, starts with `from __future__`
- `sentinel/app.py:1-21` — no docstring, starts with sys.path manipulation
- `sentinel/models.py:1-6` — no docstring
- `sentinel/tools/audit_log.py:1-10` — no docstring
- `sentinel/tools/api_keys.py:1-10` — no docstring
- `sentinel/tools/media_utils.py:1-10` — no docstring
- `sentinel/guardrails.py:1-6` — no docstring; `INJECTION_PATTERNS` list lacks inline comments

### Status
[x] done

---

## Sub-Task 6 — Remove Dead Code

### Intent
Two unused constants and three nearly-empty `__init__.py` files add noise without purpose. Removing them keeps the codebase honest about what is actually used.

### Expected Outcomes
- `SEVERITY_TIERS_PATH` removed from `sentinel/config.py`.
- `TICKETING_SYSTEMS` in `sentinel/api.py` either removed or documented as a purely informational constant (not operational logic).
- `sentinel/__init__.py`, `sentinel/agents/__init__.py`, `sentinel/tools/__init__.py` each have at minimum a module docstring so they are not completely empty.
- All 28 tests continue to pass.

### Todo List
1. Remove `SEVERITY_TIERS_PATH = POLICY_DIR / "severity_tiers.yaml"` from `sentinel/config.py` line 26. (This will also be done in Sub-Task 3 — skip if already done.)
2. In `sentinel/api.py` line 23: add a comment to `TICKETING_SYSTEMS` clarifying it is informational metadata only — no implementation exists for non-Jira systems.
3. Add a one-line docstring to `sentinel/__init__.py`.
4. Add a one-line docstring to `sentinel/agents/__init__.py`.
5. Add a one-line docstring to `sentinel/tools/__init__.py`.

### Relevant Context
- `sentinel/config.py:26` — `SEVERITY_TIERS_PATH`
- `sentinel/api.py:23` — `TICKETING_SYSTEMS`
- `sentinel/__init__.py` — near-empty (5 lines, version only)
- `sentinel/agents/__init__.py` — 1 line
- `sentinel/tools/__init__.py` — 1 line

### Status
[x] done

---

## Sub-Task 7 — Document API Contracts (`metadata` keys and `ModerationRequest`)

### Intent
`ModerationRequest.metadata` is typed as `dict[str, Any]` with no documentation of which keys are reserved or supported by Sentinel internally. Similarly, `case.metadata` accumulates internal keys (`_evidence_input`, `analysis_mode`, `synthetic_label`, `expected_decision`) with no schema. Documenting these contracts prevents accidental collisions and aids integration.

### Expected Outcomes
- A docstring or inline comment block in `sentinel/models.py` and/or `sentinel/api.py` lists all reserved `metadata` keys, their types, and their purpose.
- `ModerationRequest` Pydantic model has a `model_config` or `Field(description=...)` annotation documenting the `metadata` field.
- No behaviour changes.

### Todo List
1. In `sentinel/models.py`, add a module-level or class-level comment block listing all internally used `case.metadata` keys:
   - `analysis_mode` (`"synthetic"` | `"production"`) — controls agent dispatch
   - `synthetic_label` (str) — expected category for synthetic cases
   - `expected_decision` (str) — expected verdict for synthetic evaluation
   - `_evidence_input` (list) — cached prepared input for senior re-use
2. In `sentinel/api.py`, update the `ModerationRequest` Pydantic model's `metadata` field with a `Field(description=...)` listing the supported/reserved keys and noting that keys prefixed with `_` are reserved for internal use.
3. In `sentinel/api.py`, add a comment to the `TICKETING_SYSTEMS` constant (if still present after Sub-Task 6) noting which keys are actually implemented.

### Relevant Context
- `sentinel/models.py` — `Case` dataclass with `metadata: dict` field
- `sentinel/api.py` — `ModerationRequest` Pydantic model
- `sentinel/agents/orchestrator.py` — writes `_evidence_input`, reads `analysis_mode`
- `sentinel/agents/common.py` — reads `synthetic_label`, `expected_decision`

### Status
[x] done

---

## Sub-Task 8 — Fill Critical Test Coverage Gaps

### Intent
The existing 28 tests are hermetic and well-structured, but several important code paths have zero direct test coverage. This sub-task adds targeted unit tests for the highest-risk gaps: guardrail patterns, media extraction fallbacks, schema migrations, and hash-match tool.

### Expected Outcomes
- New test file `sentinel/tests/test_guardrail_patterns.py` — direct unit tests for each `INJECTION_PATTERNS` regex entry and `check_tier1_guardrail()` logic.
- New test file `sentinel/tests/test_media_extraction.py` — unit tests for `detect_asset_type()`, `extract_video_audio_transcript()` exception fallback (mocked cv2/moviepy ImportError), and `sample_video_frame_data_urls()` ImportError fallback.
- New test file `sentinel/tests/test_schema_migration.py` — tests that `_ensure_column()` is idempotent and that `init_db()` can run on an already-migrated DB without error.
- New test file `sentinel/tests/test_hash_match.py` — tests for `file_sha256()` correctness, `known_hash_match()` hit and miss, and graceful handling of a missing `known_hashes.txt`.
- All new tests must be offline/hermetic (no real API calls, no real files beyond tmp dirs).

### Todo List
1. Create `sentinel/tests/test_guardrail_patterns.py`:
   - Import `INJECTION_PATTERNS` from `guardrails.py`.
   - For each pattern, write one test that asserts a known-malicious string matches and one that asserts a benign string does not match.
   - Test `check_tier1_guardrail()` with a Tier-1 category and a non-Tier-1 category.
2. Create `sentinel/tests/test_media_extraction.py`:
   - Test `detect_asset_type()` with known extensions (`.mp4`, `.jpg`, `.mp3`, `.txt`) and an unknown extension.
   - Test `extract_video_audio_transcript()` when `moviepy` raises `ImportError` — assert returns `""` and does not raise.
   - Test `sample_video_frame_data_urls()` when `cv2` raises `ImportError` — assert returns `[]` and does not raise.
   - Use `unittest.mock.patch` to simulate the import failures without actually removing packages.
3. Create `sentinel/tests/test_schema_migration.py`:
   - Create a temporary SQLite DB and call `init_db(db_path)` twice — assert no error on the second call (idempotency).
   - Manually create a DB missing a column; call `_ensure_column()` and assert the column is present afterwards.
4. Create `sentinel/tests/test_hash_match.py`:
   - Write a temp file, compute its SHA-256, write the hash to a temp `known_hashes.txt`, call `known_hash_match()` — assert returns `True`.
   - Call `known_hash_match()` with a hash not in the list — assert returns `False`.
   - Call `known_hash_match()` when `known_hashes.txt` is missing — assert returns `False` (no exception).

### Relevant Context
- `sentinel/guardrails.py` — `INJECTION_PATTERNS`, `check_tier1_guardrail()`, `tier1_output_guardrail()`, `injection_input_guardrail()`
- `sentinel/tools/media_utils.py` — `detect_asset_type()`, quarantine helpers
- `sentinel/tools/production_analysis.py:226-276` — `sample_video_frame_data_urls()`, `extract_video_audio_transcript()`
- `sentinel/tools/audit_log.py` — `init_db()`, `_ensure_column()`
- `sentinel/tools/hash_match.py` — `file_sha256()`, `known_hash_match()`, `hash_match_tool()`
- `sentinel/tests/conftest.py` — follow existing pattern: scrub env vars, use `tempfile.TemporaryDirectory`

### Status
[x] done

---

## Implementation Order

```
Sub-Task 1  →  Sub-Task 2  →  Sub-Task 3  →  Sub-Task 4
                                                   ↓
Sub-Task 8  ←  Sub-Task 7  ←  Sub-Task 6  ←  Sub-Task 5
```

Sub-Tasks 1–4 make structural changes; Sub-Tasks 5–7 are documentation; Sub-Task 8 validates correctness. Each sub-task can be reviewed independently before the next begins.
