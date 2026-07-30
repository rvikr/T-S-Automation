from __future__ import annotations

import sys
from pathlib import Path

_this_dir = Path(__file__).resolve().parent
sys.path[:] = [p for p in sys.path if Path(p).resolve() != _this_dir]
sys.path.insert(0, str(_this_dir.parent))

import os
from dataclasses import asdict

import streamlit as st

try:
    for _key, _value in st.secrets.items():
        os.environ.setdefault(str(_key), str(_value))
except FileNotFoundError:
    pass

from sentinel.agents import live_events
from sentinel.agents.orchestrator import run_batch, run_case
from sentinel.config import DEFAULT_DB_PATH, DEMO_SAMPLES_DIR, SYNTHETIC_CASES_DIR, load_settings
from sentinel.tools.audit_log import init_db, list_moderation_logs
from sentinel.tools.media_utils import load_synthetic_cases
from sentinel.tools.policy_retrieval import get_clause_for_category
from sentinel.tools.precedent_memory import clear_precedents
from sentinel.tools.ticketing import list_human_tickets, resolve_ticket
from sentinel.ui_uploads import (
    LOG_VIEW_LABEL,
    METRICS_VIEW_LABEL,
    MODERATION_VIEW_LABEL,
    REVIEW_QUEUE_VIEW_LABEL,
    UPLOAD_EXTENSIONS,
    build_production_uploaded_case,
    describe_live_event,
    describe_trace_event,
    estimate_cost_usd,
    format_moderation_log_rows,
    list_eval_runs,
    load_eval_run,
    openai_trace_url,
    ui_live_run_cap,
    ui_password,
    verify_ui_password,
)


def production_access_granted() -> bool:
    """Gate the paid live-agent surface behind SENTINEL_UI_PASSWORD when set.

    Without a configured password the tab stays open (local development), but a
    deployment with live credentials gets a visible warning: this surface runs
    model calls billed to the operator, so a public URL must not expose it
    unauthenticated.
    """
    if not ui_password():
        if load_settings().openai_api_key_present:
            st.warning(
                "This tab runs paid model calls and is currently unprotected. "
                "Set SENTINEL_UI_PASSWORD before sharing this deployment publicly."
            )
        return True
    if st.session_state.get("ui_unlocked"):
        return True
    st.info("Production moderation runs live agents (paid model calls) and requires a password.")
    candidate = st.text_input("Access password", type="password", key="ui-password")
    if st.button("Unlock", key="ui-unlock"):
        if verify_ui_password(candidate):
            st.session_state["ui_unlocked"] = True
            st.rerun()
        else:
            st.error("Incorrect password.")
    return False


def _consume_live_run_budget() -> bool:
    """Enforce the session cap and the global daily budget on live runs.

    The session cap is a soft brake (resets on refresh); the daily budget is
    the hard ceiling, shared with the API and stored in SQLite.
    """
    from sentinel.tools.run_budget import consume_daily_budget

    cap = ui_live_run_cap()
    used = int(st.session_state.get("live_runs_used", 0))
    if cap > 0 and used >= cap:
        st.error(
            f"This session reached its limit of {cap} live moderation runs. "
            "Refresh the page to start a new session, or raise SENTINEL_UI_MAX_LIVE_RUNS."
        )
        return False
    allowed, used_today = consume_daily_budget(DEFAULT_DB_PATH)
    if not allowed:
        st.error(
            f"The global daily moderation budget is exhausted ({used_today} runs today). "
            "Raise SENTINEL_DAILY_LIVE_RUN_LIMIT or retry after UTC midnight."
        )
        return False
    if cap > 0:
        st.session_state["live_runs_used"] = used + 1
    return True


def run_case_with_live_status(selected_case):
    """Run a case while streaming agent tool calls and handoffs into st.status.

    Returns None when the session's live-run budget is exhausted — the budget
    only applies to production cases, which are the ones that cost money.
    """
    if selected_case.metadata.get("analysis_mode") == "production" and not _consume_live_run_budget():
        return None
    with st.status("Moderation agents are reviewing the upload...", expanded=True) as status:
        def _render(event: str) -> None:
            status.write(describe_live_event(event))

        with live_events.event_sink(_render):
            result = run_case(selected_case, db_path=DEFAULT_DB_PATH)
        for event in result.trace:
            icon, text = describe_trace_event(event)
            status.write(f"{icon} {text}")
        status.update(label="Review complete", state="complete", expanded=False)
    return result


DECISION_BADGES = {
    "allow": ("✅ ALLOW", st.success),
    "reject": ("⛔ REJECT", st.error),
    "ambiguous": ("🧑‍⚖️ HUMAN REVIEW", st.warning),
}


def render_preview(upload) -> None:
    if not upload:
        return
    mime = upload.type or ""
    if mime.startswith("image/"):
        st.image(upload.getvalue(), caption=upload.name)
    elif mime.startswith("video/"):
        st.video(upload.getvalue())
    elif mime.startswith("audio/"):
        st.audio(upload.getvalue())
    else:
        text = upload.getvalue().decode("utf-8", errors="ignore")
        st.text_area("Text preview", text[:4000], height=180, disabled=True)


def render_verdict_card(result) -> None:
    verdict = result.verdict
    badge, banner = DECISION_BADGES.get(verdict.decision, (verdict.decision.upper(), st.info))
    banner(f"{badge} — {verdict.category} (severity tier {verdict.severity_tier})")

    columns = st.columns(4)
    columns[0].metric("Decision", verdict.decision)
    columns[1].metric("Severity tier", verdict.severity_tier)
    columns[2].metric("Confidence", f"{verdict.confidence:.0%}")
    columns[3].metric("Reviewer", verdict.reviewer)

    latency_ms = result.case.metadata.get("latency_ms")
    usage = result.case.metadata.get("token_usage") or {}
    if latency_ms is not None or usage:
        perf = st.columns(4)
        if latency_ms is not None:
            label = f"{latency_ms / 1000:.1f} s" if latency_ms >= 1000 else f"{latency_ms} ms"
            perf[0].metric("Latency", label)
        if usage:
            perf[1].metric("Tokens (total)", f"{usage.get('total_tokens', 0):,}")
            perf[2].metric("Tokens in / out", f"{usage.get('input_tokens', 0):,} / {usage.get('output_tokens', 0):,}")
            perf[3].metric("LLM requests", usage.get("requests", 0))
        cost = estimate_cost_usd(usage)
        if cost is not None:
            st.caption(f"Est. cost this case: **${cost:.4f}** (at published per-token rates)")

    clause = get_clause_for_category(verdict.category)
    st.markdown(f"**Policy clause:** `{verdict.policy_clause}`")
    st.caption(clause.summary)
    st.markdown(f"**Rationale:** {verdict.rationale}")

    citations = result.case.metadata.get("cited_clauses") or []
    if citations:
        st.markdown("**Clauses cited by the agents:** " + ", ".join(f"`{code}`" for code in citations))

    if result.warning_message:
        st.warning(result.warning_message)
    if result.quarantined:
        st.info("🔒 Content quarantined pending human review.")
    if result.ticket:
        ticket = result.ticket
        if ticket.external_url:
            st.error(f"🎫 Human review ticket {ticket.id} escalated to Jira as **{ticket.external_key}**")
            st.link_button(f"Open {ticket.external_key} in Jira", ticket.external_url)
        else:
            st.error(f"🎫 Human review ticket created: {ticket.id}")

    trace_id = result.case.metadata.get("openai_trace_id")
    if trace_id:
        st.link_button("🛰️ Open the OpenAI trace for this run", openai_trace_url(trace_id))
        st.caption(f"Agents SDK trace `{trace_id}` — tool calls, handoffs, and guardrail spans.")


def render_trace_timeline(trace: list[str]) -> None:
    st.subheader("Agent trace")
    for event in trace:
        icon, text = describe_trace_event(event)
        st.markdown(f"{icon} {text}")


def render_result(result) -> None:
    render_verdict_card(result)
    render_trace_timeline(result.trace)
    with st.expander("Raw verdict and trace"):
        st.json(asdict(result.verdict))
        st.json({"trace": result.trace})


# Committed, clearly-labeled text stand-in (no depiction) — the same case the
# reference live eval scored with Tier-1 recall 1.0.
TIER1_DEMO_ASSET = SYNTHETIC_CASES_DIR / "tier1-child-standin-002.synthetic"

# Committed benign media so the multimodal claim is one click, not a file hunt.
DEMO_SAMPLES = [
    ("🖼️ Try the sample image", "sample-image.png", "image/png"),
    ("🎙️ Try the sample voice note", "sample-audio.wav", "audio/wav"),
    ("🎬 Try the sample video", "sample-video.mp4", "video/mp4"),
]


def render_demo_samples() -> None:
    st.markdown("**Or moderate a bundled sample** — every modality in one click:")
    sample_columns = st.columns(len(DEMO_SAMPLES))
    for column, (label, filename, mime) in zip(sample_columns, DEMO_SAMPLES):
        sample_path = DEMO_SAMPLES_DIR / filename
        if sample_path.exists() and column.button(label, key=f"sample-{filename}"):
            production_case = build_production_uploaded_case(
                name=filename,
                content_type=mime,
                payload=sample_path.read_bytes(),
            )
            result = run_case_with_live_status(production_case)
            if result is not None:
                render_result(result)


def render_tier1_demo() -> None:
    with st.container(border=True):
        st.markdown("**🚨 Tier-1 guardrail demo** — the line AI must not cross")
        st.caption(
            "One click runs a committed, clearly-labeled Tier-1 stand-in through the live agents. "
            "The SDK output guardrail halts the run mid-flight, the upload is quarantined, and a "
            "human review ticket is opened (mirrored to Jira when configured). The agents have no "
            "ticketing tool, so the AI cannot skip the escalation. Requires OPENAI_API_KEY; "
            "offline, run the same case from the Synthetic library tab."
        )
        # Without credentials this case cannot reach the agents, so the rail it
        # is meant to demonstrate never fires — the run falls through to the
        # generic fail-closed path and renders "No Violation / tier 0" for a
        # Tier-1 stand-in, i.e. the exact opposite of the point. Disable rather
        # than show a verdict that contradicts the asset's own label.
        live_agents_available = load_settings().openai_api_key_present
        if not live_agents_available:
            st.warning(
                "OPENAI_API_KEY is not configured, so this case cannot reach the live agents. "
                "Run `tier1-child-standin-001` from the Synthetic library below to see the "
                "Tier-1 rail quarantine and ticket the case offline."
            )
        if st.button(
            "Run the Tier-1 guardrail demo",
            key="run-tier1-demo",
            disabled=not live_agents_available,
        ):
            production_case = build_production_uploaded_case(
                name="tier1-guardrail-demo.txt",
                content_type="text/plain",
                payload=TIER1_DEMO_ASSET.read_bytes(),
            )
            result = run_case_with_live_status(production_case)
            if result is not None:
                render_result(result)


def render_learning_metric(cases) -> None:
    st.subheader("Learning metric")
    st.caption("Senior resolutions are stored as precedents; a second pass over the same batch escalates less.")
    if st.button("Run batch twice"):
        clear_precedents(DEFAULT_DB_PATH)
        first = run_batch(cases, db_path=DEFAULT_DB_PATH)
        second = run_batch(cases, db_path=DEFAULT_DB_PATH)
        st.metric(
            "Non-Tier-1 escalation rate",
            f"{second.escalation_rate:.0%}",
            delta=f"{second.escalation_rate - first.escalation_rate:.0%}",
        )
        st.write({"first_pass": first.escalation_rate, "second_pass": second.escalation_rate})


def _escalation_context(case_id: str) -> list:
    """Audit rows for the escalated case — the 'why' a reviewer needs."""
    from sentinel.tools.audit_log import fetch_audit_entries

    return [audit for audit in fetch_audit_entries(DEFAULT_DB_PATH) if audit.case_id == case_id]


def render_review_queue_page() -> None:
    st.subheader("Human review queue")
    st.caption(
        "Escalations the rails routed to a human. Your decision here is final, is recorded in the "
        "audit log under the original moderation run, and closes the ticket."
    )
    flash = st.session_state.pop("queue_flash", None)
    if flash:
        level, message = flash
        (st.success if level == "success" else st.error)(message)
    tickets = list_human_tickets(DEFAULT_DB_PATH)
    open_tickets = [ticket for ticket in tickets if ticket.status == "open"]
    resolved_tickets = [ticket for ticket in tickets if ticket.status != "open"]

    # Queue health at a glance.
    tier1_open = sum(1 for ticket in open_tickets if ticket.severity == 1)
    stats = st.columns(4)
    stats[0].metric("Open", len(open_tickets))
    stats[1].metric("Tier-1 open", tier1_open)
    stats[2].metric("Resolved", len(resolved_tickets))
    oldest = min((ticket.created_at for ticket in open_tickets), default=None)
    stats[3].metric("Oldest open", oldest[:10] if oldest else "—")

    if not open_tickets:
        st.success("No open escalations — the queue is clear.")
    else:
        tiers = sorted({ticket.severity for ticket in open_tickets})
        tier_choice = st.selectbox(
            "Filter by severity tier",
            ["All"] + [f"Tier {tier}" for tier in tiers],
            help="Tier 1 always sorts first — it is the queue's legal-exposure work.",
        )
        visible = [
            ticket
            for ticket in open_tickets
            if tier_choice == "All" or f"Tier {ticket.severity}" == tier_choice
        ]
        # Severity-first, then oldest-first: the triage order a T&S lead expects.
        visible.sort(key=lambda ticket: (ticket.severity, ticket.created_at))

        labels = [
            f"{ticket.id} — Tier {ticket.severity} · {ticket.category} · case {ticket.case_id}"
            for ticket in visible
        ]
        selected_label = st.selectbox(f"Open tickets ({len(visible)})", labels)
        ticket = visible[labels.index(selected_label)]

        columns = st.columns(4)
        columns[0].metric("Severity tier", ticket.severity)
        columns[1].metric("Category", ticket.category)
        columns[2].metric("Case", ticket.case_id)
        columns[3].metric("Opened", ticket.created_at[:19])

        context = _escalation_context(ticket.case_id)
        if context:
            with st.container(border=True):
                st.markdown("**Why this escalated** — from the audit trail:")
                for audit in context:
                    st.markdown(
                        f"- `{audit.timestamp[:19]}` **{audit.reviewer}** → {audit.decision} "
                        f"under `{audit.clause}`: {audit.rationale}"
                    )
        if ticket.severity == 1:
            st.warning(
                "Tier-1 escalation: the content is quarantined and was never adjudicated by AI. "
                "Follow your organisation's Tier-1 handling procedure before resolving."
            )
        if ticket.external_url:
            st.link_button(f"Open {ticket.external_key} in Jira", ticket.external_url)

        decision = st.radio("Final decision", ["allow", "reject"], horizontal=True, key=f"decision-{ticket.id}")
        rationale = st.text_area(
            "Rationale (required — written to the audit log)",
            key=f"rationale-{ticket.id}",
            placeholder="Why this decision is correct under the cited policy clause.",
        )
        if st.button("Resolve ticket", key=f"resolve-{ticket.id}", type="primary"):
            if not rationale.strip():
                st.error("A rationale is required: the audit trail must record why a human decided.")
            else:
                resolved = resolve_ticket(ticket.id, decision, rationale, DEFAULT_DB_PATH)
                if resolved is None:
                    st.session_state["queue_flash"] = (
                        "error",
                        "This ticket was already resolved (possibly by another reviewer).",
                    )
                else:
                    st.session_state["queue_flash"] = (
                        "success",
                        f"Ticket {resolved.id} resolved: {decision}. Audit log updated.",
                    )
                st.rerun()

    if resolved_tickets:
        with st.expander(f"Recently resolved ({len(resolved_tickets)})"):
            st.dataframe(
                [
                    {
                        "Ticket": ticket.id,
                        "Case": ticket.case_id,
                        "Tier": ticket.severity,
                        "Category": ticket.category,
                        "Status": ticket.status,
                        "Opened": ticket.created_at,
                    }
                    for ticket in resolved_tickets
                ],
                width="stretch",
            )


def render_logs_page() -> None:
    st.subheader("Moderation logs")
    logs = list_moderation_logs(DEFAULT_DB_PATH)
    if not logs:
        st.caption("No moderation logs yet.")
        return

    st.dataframe(format_moderation_log_rows(logs), width="stretch")
    labels = [f"#{log.id} - {log.case_id}" for log in logs]
    selected_label = st.selectbox("Log details", labels)
    selected_log = logs[labels.index(selected_label)]
    ticket = (selected_log.escalation_details or {}).get("ticket") or {}
    if ticket.get("external_url"):
        st.link_button(f"Open {ticket.get('external_key')} in Jira", ticket["external_url"])
    st.json(asdict(selected_log))


def render_metrics_page() -> None:
    st.subheader("Golden-set evaluation")
    runs = list_eval_runs()
    if not runs:
        st.caption(
            "No evaluation runs yet. Generate one with "
            "`python -m sentinel.eval.run_eval` (offline) or `--live` (real agents)."
        )
        return

    run_labels = [run.name for run in runs]
    selected = st.selectbox("Evaluation run", run_labels)
    data = load_eval_run(runs[run_labels.index(selected)])
    metrics = data["metrics"]

    columns = st.columns(4)
    columns[0].metric("Outcome accuracy", f"{metrics['accuracy']:.1%}")
    tier1 = metrics.get("tier1_recall")
    columns[1].metric("Tier-1 recall", f"{tier1:.0%}" if tier1 is not None else "n/a")
    fpr = metrics.get("benign_false_positive_rate")
    columns[2].metric("Benign FPR", f"{fpr:.1%}" if fpr is not None else "n/a")
    columns[3].metric("Escalation rate", f"{metrics['escalation_rate']:.1%}")

    latency = metrics.get("latency_ms")
    if latency:
        tokens = metrics.get("total_tokens", 0)
        cost_mean = metrics.get("est_cost_usd_mean")
        st.caption(
            f"⏱️ Latency per case: mean {latency['mean']} ms, p95 {latency['p95']} ms"
            + (f" · 🔢 total tokens {tokens:,}" if tokens else "")
            + (f" · 💲 est. ${cost_mean:.4f}/case at published rates" if cost_mean else "")
        )

    per_modality = metrics.get("per_modality")
    if per_modality:
        st.markdown("**Per-modality**")
        st.dataframe(
            [{"modality": modality, **row} for modality, row in per_modality.items()],
            width="stretch",
        )

    st.markdown("**Per-outcome precision / recall / F1**")
    per_class = metrics["per_class"]
    st.dataframe(
        [
            {"outcome": outcome, **{k: v for k, v in row.items()}}
            for outcome, row in per_class.items()
        ],
        width="stretch",
    )

    st.markdown("**Confusion matrix** (rows = expected, columns = predicted)")
    confusion = metrics["confusion_matrix"]
    st.dataframe(
        [{"expected": expected, **predictions} for expected, predictions in confusion.items()],
        width="stretch",
    )

    misses = [case for case in data["cases"] if not case["correct"]]
    st.markdown(f"**Misses ({len(misses)})**")
    if misses:
        st.dataframe(misses, width="stretch")
    else:
        st.caption("No misses in this run.")


st.set_page_config(page_title="Sentinel — Trust & Safety Moderation", page_icon="🛡️", layout="wide")
st.title("🛡️ Sentinel")
st.caption("Agentic content moderation on deterministic rails: agents judge the content, code guards the agents.")

with st.expander("New here? How Sentinel works", expanded=False):
    st.markdown(
        "1. **Moderation** — upload content (or run a bundled sample). A specialist agent grounds its "
        "verdict in the policy corpus; ambiguous cases escalate to a stricter senior agent.\n"
        "2. **Deterministic rails** — Tier-1 signals (child safety, terrorism) always quarantine and open "
        "a human ticket; the agents have no ticketing tool, so the AI can neither create nor skip an escalation.\n"
        "3. **Review queue** — human reviewers resolve escalated tickets; every resolution lands in the audit log.\n"
        "4. **Logs & Metrics** — the full audit trail and the golden-set evaluation results."
    )

init_db(DEFAULT_DB_PATH)
cases = load_synthetic_cases()
case_by_label = {f"{case.id} - {case.metadata.get('expected_category')}": case for case in cases}

_open_ticket_count = sum(1 for ticket in list_human_tickets(DEFAULT_DB_PATH) if ticket.status == "open")
_queue_label = (
    f"{REVIEW_QUEUE_VIEW_LABEL} ({_open_ticket_count})" if _open_ticket_count else REVIEW_QUEUE_VIEW_LABEL
)
_view_labels = [MODERATION_VIEW_LABEL, _queue_label, LOG_VIEW_LABEL, METRICS_VIEW_LABEL]
view = st.sidebar.radio(
    "View",
    _view_labels,
    help="Moderation runs cases; Review queue resolves escalations; Logs and Metrics are read-only evidence.",
)
if view == _queue_label:
    view = REVIEW_QUEUE_VIEW_LABEL

if view == REVIEW_QUEUE_VIEW_LABEL:
    render_review_queue_page()
elif view == LOG_VIEW_LABEL:
    render_logs_page()
elif view == METRICS_VIEW_LABEL:
    render_metrics_page()
else:
    upload_tab, synthetic_tab = st.tabs(["Production upload", "Synthetic library"])

    with upload_tab:
        if production_access_granted():
            st.caption(
                "Production mode: uploads are reviewed by live moderation agents against the policy taxonomy. "
                "Do not upload illegal material; Tier-1 signals are routed to human review without detailed automated analysis."
            )
            upload = st.file_uploader(
                "Upload image, video, audio, or text",
                type=[ext.removeprefix(".") for ext in UPLOAD_EXTENSIONS],
                accept_multiple_files=False,
                help="The file is reviewed by live moderation agents; supported types are listed in the picker.",
            )
            render_preview(upload)
            if upload and st.button("Run production moderation", key="run-upload"):
                selected_case = build_production_uploaded_case(
                    name=upload.name,
                    content_type=upload.type,
                    payload=upload.getvalue(),
                )
                result = run_case_with_live_status(selected_case)
                if result is not None:
                    render_result(result)

            st.divider()
            render_demo_samples()

            st.divider()
            render_tier1_demo()

    with synthetic_tab:
        st.caption("Synthetic labeled cases remain available for safe demos and regression checks.")
        selected_label = st.selectbox("Synthetic case", list(case_by_label.keys()))
        selected_case = case_by_label[selected_label]
        if st.button("Run synthetic case", key="run-synthetic"):
            result = run_case(selected_case, db_path=DEFAULT_DB_PATH)
            render_result(result)

    st.divider()
    render_learning_metric(cases)
