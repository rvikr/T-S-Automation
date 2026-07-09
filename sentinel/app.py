from __future__ import annotations

import sys
from pathlib import Path

_this_dir = Path(__file__).resolve().parent
sys.path[:] = [p for p in sys.path if Path(p).resolve() != _this_dir]
sys.path.insert(0, str(_this_dir.parent))

from dataclasses import asdict

import streamlit as st

from sentinel.agents import live_events
from sentinel.agents.orchestrator import run_batch, run_case
from sentinel.config import DEFAULT_DB_PATH, SYNTHETIC_CASES_DIR
from sentinel.tools.audit_log import init_db, list_moderation_logs
from sentinel.tools.media_utils import load_synthetic_cases
from sentinel.tools.policy_retrieval import get_clause_for_category
from sentinel.tools.precedent_memory import clear_precedents
from sentinel.ui_uploads import (
    LOG_VIEW_LABEL,
    METRICS_VIEW_LABEL,
    MODERATION_VIEW_LABEL,
    UPLOAD_EXTENSIONS,
    build_production_uploaded_case,
    describe_live_event,
    describe_trace_event,
    format_moderation_log_rows,
    list_eval_runs,
    load_eval_run,
    openai_trace_url,
)


def run_case_with_live_status(selected_case):
    """Run a case while streaming agent tool calls and handoffs into st.status."""
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
        if st.button("Run the Tier-1 guardrail demo", key="run-tier1-demo"):
            production_case = build_production_uploaded_case(
                name="tier1-guardrail-demo.txt",
                content_type="text/plain",
                payload=TIER1_DEMO_ASSET.read_bytes(),
            )
            result = run_case_with_live_status(production_case)
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
        st.caption(
            f"⏱️ Latency per case: mean {latency['mean']} ms, p95 {latency['p95']} ms"
            + (f" · 🔢 total tokens {tokens:,}" if tokens else "")
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


st.set_page_config(page_title="Sentinel Moderation QA", layout="wide")
st.title("Sentinel")
st.caption("Agentic content moderation: specialist agents, senior review, Tier-1 human-only rails.")

init_db(DEFAULT_DB_PATH)
cases = load_synthetic_cases()
case_by_label = {f"{case.id} - {case.metadata.get('expected_category')}": case for case in cases}

view = st.sidebar.radio("View", [MODERATION_VIEW_LABEL, LOG_VIEW_LABEL, METRICS_VIEW_LABEL])

if view == LOG_VIEW_LABEL:
    render_logs_page()
elif view == METRICS_VIEW_LABEL:
    render_metrics_page()
else:
    upload_tab, synthetic_tab = st.tabs(["Production upload", "Synthetic library"])

    with upload_tab:
        st.caption(
            "Production mode: uploads are reviewed by live moderation agents against the policy taxonomy. "
            "Do not upload illegal material; Tier-1 signals are routed to human review without detailed automated analysis."
        )
        upload = st.file_uploader(
            "Upload image, video, audio, or text",
            type=[ext.removeprefix(".") for ext in UPLOAD_EXTENSIONS],
            accept_multiple_files=False,
        )
        render_preview(upload)
        if upload and st.button("Run production moderation", key="run-upload"):
            selected_case = build_production_uploaded_case(
                name=upload.name,
                content_type=upload.type,
                payload=upload.getvalue(),
            )
            result = run_case_with_live_status(selected_case)
            render_result(result)

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
