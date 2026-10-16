"""Exercise 5 — Streamlit approval UI for the HITL PR review agent.

Run with:
    uv run streamlit run app.py

Goal: wrap the LangGraph built in exercises 1–4 in a web UI that adapts to
the confidence bucket of each PR.

Routing thresholds (common/schemas.py):
    > 72%        auto_approve     UI shows a success card; reviewer does nothing
    58 – 72%     human_approval   UI shows Approve / Reject / Edit buttons
    <  58%       escalate         UI shows a question form for the reviewer
"""

from __future__ import annotations

import asyncio
import uuid

import streamlit as st
from dotenv import load_dotenv
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Command

from common.db import db_path
from exercises.exercise_4_audit import build_graph
from audit.replay import recent_threads
from common.github import parse_pr_url


load_dotenv()


# ─── Session state ─────────────────────────────────────────────────────────
if "thread_id" not in st.session_state:
    st.session_state.thread_id = None
if "pr_url" not in st.session_state:
    st.session_state.pr_url = ""
if "interrupt_payload" not in st.session_state:
    st.session_state.interrupt_payload = None
if "final" not in st.session_state:
    st.session_state.final = None


# ─── Page setup ────────────────────────────────────────────────────────────
st.set_page_config(page_title="HITL PR Review", layout="wide")
st.title("HITL PR Review Agent")


# ─── Sidebar — recent sessions ─────────────────────────────────────────────
with st.sidebar:
    st.header("Recent sessions")
    for row in asyncio.run(recent_threads()):
        st.caption(f"{row['pr_url']} · {row['worst_risk']} · {row['last_event']}")
        if st.button(row["thread_id"], key="open_" + row["thread_id"]):
            st.session_state.thread_id = row["thread_id"]
            st.session_state.pr_url = row["pr_url"]
            st.session_state.interrupt_payload = None
            st.session_state.final = None
            st.query_params["thread"] = row["thread_id"]
            st.rerun()


# ─── Top form — start a new review ─────────────────────────────────────────
with st.form("start"):
    pr_url = st.text_input(
        "PR URL", value=st.session_state.pr_url,
        placeholder="https://github.com/VinUni-AI20k/PR-Demo/pull/1",
    )
    submitted = st.form_submit_button("Run review")


# ─── Renderers per interrupt kind ──────────────────────────────────────────
def render_approval_card(payload: dict) -> dict | None:
    """58–72% bucket: show the LLM review + 3 buttons. Return resume dict or None."""
    conf = payload["confidence"]
    st.subheader(f"Approval requested — confidence {conf:.0%}")
    st.caption(payload["confidence_reasoning"])
    st.markdown(payload["summary"])

    for c in payload.get("comments", []):
        st.markdown(f"- **[{c['severity']}]** `{c['file']}:{c.get('line') or '?'}` — {c['body']}")

    with st.expander("Diff"):
        st.code(payload.get("diff_preview", ""), language="diff")

    feedback = st.text_area("Feedback / replacement review text for Edit", key="approval_feedback_" + st.session_state.thread_id)
    col1, col2, col3 = st.columns(3)
    if col1.button("Approve", type="primary"):
        return {"choice": "approve", "feedback": feedback}
    if col2.button("Reject"):
        return {"choice": "reject", "feedback": feedback}
    if col3.button("Edit"):
        if not feedback.strip():
            st.warning("Enter the replacement review text before choosing Edit.")
        else:
            return {"choice": "edit", "feedback": feedback}
    return None


def render_escalation_card(payload: dict) -> dict | None:
    """< 58% bucket: show risk factors + question form. Return {question: answer} or None."""
    conf = payload["confidence"]
    st.subheader(f"Strong escalation — confidence {conf:.0%}")
    st.caption(payload["confidence_reasoning"])
    if payload.get("risk_factors"):
        st.error("Risks: " + ", ".join(payload["risk_factors"]))
    st.markdown(payload["summary"])

    with st.form("escalation"):
        answers = {q: st.text_area(q, key=f"question_{i}_{st.session_state.thread_id}")
                   for i, q in enumerate(payload["questions"])}
        if st.form_submit_button("Submit answers"):
            if all(a.strip() for a in answers.values()):
                return answers
            st.warning("Please answer every question.")
    return None


# ─── Drive the graph ───────────────────────────────────────────────────────
async def run_graph(pr_url: str, thread_id: str, resume_value=None):
    """Invoke the graph once. Returns the final result or {'__interrupt__': ...}."""
    async with AsyncSqliteSaver.from_conn_string(db_path()) as cp:
        await cp.setup()
        app = build_graph(cp)
        cfg = {"configurable": {"thread_id": thread_id}}
        if resume_value is not None:
            return await app.ainvoke(Command(resume=resume_value), cfg)
        snapshot = await app.aget_state(cfg)
        if snapshot.values:
            result = dict(snapshot.values)
            pending = [i for task in snapshot.tasks for i in task.interrupts]
            if pending:
                result["__interrupt__"] = pending
            elif snapshot.next:
                result = await app.ainvoke(None, cfg)
            return result
        return await app.ainvoke({"pr_url": pr_url, "thread_id": thread_id}, cfg)


def save_result(result):
    st.session_state.interrupt_payload = (
        result["__interrupt__"][0].value if result.get("__interrupt__") else None)
    st.session_state.final = None if result.get("__interrupt__") else result


def invoke_safely(*args, **kwargs):
    try:
        return asyncio.run(run_graph(*args, **kwargs))
    except Exception as exc:
        # Provider errors may include request details; do not expose secrets in UI.
        st.error(f"Review stopped ({type(exc).__name__}). Check configuration or try again later.")
        st.stop()


# A URL query parameter restores durable state even after a browser refresh.
if st.session_state.thread_id is None and st.query_params.get("thread"):
    wanted = st.query_params["thread"]
    row = next((r for r in asyncio.run(recent_threads()) if r["thread_id"] == wanted), None)
    if row:
        st.session_state.thread_id = wanted
        st.session_state.pr_url = row["pr_url"]

if st.session_state.thread_id and st.session_state.final is None and st.session_state.interrupt_payload is None:
    save_result(invoke_safely(st.session_state.pr_url, st.session_state.thread_id))


# ─── Main flow ─────────────────────────────────────────────────────────────
if submitted and pr_url:
    try:
        parse_pr_url(pr_url)
    except ValueError:
        st.error("Enter a GitHub pull request URL.")
        st.stop()
    st.session_state.pr_url = pr_url
    st.session_state.thread_id = str(uuid.uuid4())
    st.query_params["thread"] = st.session_state.thread_id
    st.session_state.interrupt_payload = None
    st.session_state.final = None
    with st.spinner("Fetching PR + analyzing (reuses unchanged reviews)..."):
        save_result(invoke_safely(pr_url, st.session_state.thread_id))
    st.rerun()

# Render the current interrupt card, if any
payload = st.session_state.interrupt_payload
if payload is not None:
    kind = payload["kind"]
    answer = render_approval_card(payload) if kind == "approval_request" else render_escalation_card(payload)
    if answer is not None:
        with st.spinner("Resuming..."):
            result = invoke_safely(
                st.session_state.pr_url, st.session_state.thread_id, resume_value=answer)
        save_result(result)
        st.rerun()

# Render final state, if reached
if st.session_state.final is not None:
    final = st.session_state.final
    action = final.get("final_action", "?")
    if action in {"auto_committed", "committed"}:
        st.success(f"✓ {action} — comment posted to {st.session_state.pr_url}")
    elif action == "rejected":
        st.warning("Rejected — no comment posted")
    else:
        st.info(f"final_action = {action}")
    if final.get("analysis"):
        a = final["analysis"]
        st.metric("Confidence", f"{a.confidence:.0%}")
        st.markdown(a.summary)
    if action in {"auto_committed", "committed"}:
        st.link_button("View PR comments on GitHub", st.session_state.pr_url + "#issuecomment-new")
    st.caption(f"thread_id = {st.session_state.thread_id}  ·  replay: "
               f"`uv run python -m audit.replay --thread {st.session_state.thread_id}`")
