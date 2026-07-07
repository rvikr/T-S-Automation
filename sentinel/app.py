from __future__ import annotations

from dataclasses import asdict

import streamlit as st

from sentinel.agents.orchestrator import run_batch, run_case
from sentinel.config import DEFAULT_DB_PATH
from sentinel.tools.audit_log import init_db, list_moderation_logs
from sentinel.tools.media_utils import load_synthetic_cases
from sentinel.tools.precedent_memory import clear_precedents
from sentinel.ui_uploads import (
    FLOW_STAGES,
    LOG_VIEW_LABEL,
    MODERATION_VIEW_LABEL,
    UPLOAD_EXTENSIONS,
    build_production_uploaded_case,
    format_moderation_log_rows,
)


def render_flow(trace: list[str]) -> None:
    trace_text = "\n".join(trace)
    for index, stage in enumerate(FLOW_STAGES, start=1):
        if stage == "Senior or human escalation":
            active = any(marker in trace_text for marker in ["senior", "human_ticket", "quarantine"])
        elif stage == "Guardrail check":
            active = "guardrail" in trace_text or "specialist.verdict" in trace_text
        else:
            active = True
        state = "complete" if active else "not needed"
        st.write(f"{index}. {stage} - {state}")


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


def render_result(result) -> None:
    verdict = result.verdict
    top = st.columns(4)
    top[0].metric("Decision", verdict.decision)
    top[1].metric("Tier", verdict.severity_tier)
    top[2].metric("Confidence", f"{verdict.confidence:.0%}")
    top[3].metric("Reviewer", verdict.reviewer)

    if result.warning_message:
        st.warning(result.warning_message)
    if result.ticket:
        st.error(f"Human review ticket created: {result.ticket.id}")

    st.subheader("Autonomous Moderation Flow")
    render_flow(result.trace)

    left, right = st.columns(2)
    with left:
        st.subheader("Verdict")
        st.json(asdict(verdict))
    with right:
        st.subheader("Matched Policy Clause")
        st.code(verdict.policy_clause)
        st.subheader("Reasoning Trace")
        for event in result.trace:
            st.write(event)


def render_learning_metric(cases) -> None:
    st.subheader("Learning Metric")
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
    st.subheader("Moderation Logs")
    logs = list_moderation_logs(DEFAULT_DB_PATH)
    if not logs:
        st.caption("No moderation logs yet.")
        return

    st.dataframe(format_moderation_log_rows(logs), use_container_width=True)
    labels = [f"#{log.id} - {log.case_id}" for log in logs]
    selected_label = st.selectbox("Log details", labels)
    selected_log = logs[labels.index(selected_label)]
    st.json(asdict(selected_log))


st.set_page_config(page_title="Sentinel Moderation QA", layout="wide")
st.title("Sentinel")

init_db(DEFAULT_DB_PATH)
cases = load_synthetic_cases()
case_by_label = {f"{case.id} - {case.metadata.get('expected_category')}": case for case in cases}

view = st.sidebar.radio("View", [MODERATION_VIEW_LABEL, LOG_VIEW_LABEL])

if view == LOG_VIEW_LABEL:
    render_logs_page()
else:
    upload_tab, synthetic_tab = st.tabs(["Production upload", "Synthetic library"])

    with upload_tab:
        st.caption(
            "Production mode: uploads are analyzed directly against the policy taxonomy. "
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
            result = run_case(selected_case, db_path=DEFAULT_DB_PATH)
            render_result(result)

    with synthetic_tab:
        st.caption("Synthetic labeled cases remain available for safe demos and regression checks.")
        selected_label = st.selectbox("Synthetic case", list(case_by_label.keys()))
        selected_case = case_by_label[selected_label]
        if st.button("Run synthetic case", key="run-synthetic"):
            result = run_case(selected_case, db_path=DEFAULT_DB_PATH)
            render_result(result)

    st.divider()
    render_learning_metric(cases)
