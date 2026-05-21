"""Streamlit analyst chat for the conversation analysis bot.

This UI is a thin shell over the FastAPI ``/ask`` endpoint:

  1. The analyst types a question.
  2. We POST to ``/ask``; the agent loop runs (10-30 s).
  3. We render the structured :class:`AnswerEnvelope`: prose answer,
     evidence cards, and the tool-call trace (collapsed by default).

Session continuity: we thread the ``session_id`` returned by the API into
subsequent requests so follow-up questions inherit prior context.

Run locally:
    uv run streamlit run ui/app.py
The backend must already be running:
    uv run uvicorn backend.api:app --reload --port 8000
"""
from __future__ import annotations

import os
import time
from typing import Any

import httpx
import streamlit as st


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

API_URL = os.getenv("ASK_API_URL", "http://127.0.0.1:8000")
REQUEST_TIMEOUT_S = 180.0  # generous; some Q2-style multi-tool answers can run >60s

SAMPLE_QUESTIONS = [
    "What are the top 5 topics with the most negative sentiment in November 2025?",
    "Which support agents may benefit from coaching on empathy? Show me evidence.",
    "How did customer sentiment change across conversation C0009233?",
    "Which recurring customer concerns should leadership review first?",
    "Find Hindi conversations where customers are frustrated about app crashes.",
]


# ---------------------------------------------------------------------------
# Streamlit page
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="Conversation Analysis Bot",
    page_icon="💬",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

if "messages" not in st.session_state:
    st.session_state.messages = []
if "session_id" not in st.session_state:
    st.session_state.session_id = None
if "pending_question" not in st.session_state:
    st.session_state.pending_question = None


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------

with st.sidebar:
    st.header("Session")
    if st.session_state.session_id:
        st.code(st.session_state.session_id, language=None)
        st.caption(f"{len(st.session_state.messages) // 2} turn(s)")
    else:
        st.caption("No active session yet — ask a question to begin.")

    if st.button("Reset conversation", use_container_width=True):
        st.session_state.messages = []
        st.session_state.session_id = None
        st.rerun()

    st.divider()
    st.header("Try a sample question")
    for sq in SAMPLE_QUESTIONS:
        if st.button(sq, use_container_width=True, key=f"sample_{hash(sq)}"):
            st.session_state.pending_question = sq
            st.rerun()

    st.divider()
    st.caption(f"API: `{API_URL}`")


# ---------------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------------

st.title("💬 Conversation Analysis Bot")
st.caption(
    "Ask analytical questions about 3,000 customer-support conversations "
    "(English + Hindi/Hinglish, Aug–Dec 2025)."
)


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------


def _render_envelope(env: dict[str, Any], elapsed: float | None = None) -> None:
    """Render an AnswerEnvelope payload inside the current chat container."""
    st.markdown(env["answer"])

    if env.get("evidence"):
        st.markdown("**Evidence**")
        for ev in env["evidence"]:
            header = f"`{ev['conv_id']}`"
            if ev.get("similarity") is not None:
                header += f" · sim {ev['similarity']:.2f}"
            with st.container(border=True):
                st.markdown(header)
                st.code(ev["quote"], language=None)
                st.caption(ev["relevance"])

    if env.get("uncertainty"):
        st.warning(f"⚠️ {env['uncertainty']}")

    footer = env.get("reasoning_brief", "")
    if elapsed is not None:
        footer = f"{footer}  _({elapsed:.1f}s)_"
    with st.expander("How this answer was built", expanded=False):
        if footer:
            st.caption(footer)
        if env.get("tool_calls"):
            st.markdown("**Tool calls**")
            for t in env["tool_calls"]:
                arg_keys = ", ".join(t["arguments"].keys()) or "—"
                st.markdown(
                    f"- `{t['tool']}` ({arg_keys}) → {t['result_summary']}"
                )


def _call_api(question: str) -> tuple[dict[str, Any], float] | None:
    """POST to /ask and return (envelope, elapsed). On error, return None."""
    start = time.time()
    try:
        with httpx.Client(timeout=REQUEST_TIMEOUT_S) as client:
            resp = client.post(
                f"{API_URL}/ask",
                json={
                    "question": question,
                    "session_id": st.session_state.session_id,
                },
            )
        resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        st.error(f"API returned {e.response.status_code}: {e.response.text[:400]}")
        return None
    except httpx.HTTPError as e:
        st.error(
            f"Could not reach the API at `{API_URL}`. "
            f"Is the backend running? (`uv run uvicorn backend.api:app --port 8000`)\n\n"
            f"Error: {e}"
        )
        return None

    data = resp.json()
    st.session_state.session_id = data["session_id"]
    return data["envelope"], time.time() - start


# ---------------------------------------------------------------------------
# Render history
# ---------------------------------------------------------------------------

for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        if msg["role"] == "user":
            st.write(msg["content"])
        else:
            _render_envelope(msg["envelope"], msg.get("elapsed"))


# ---------------------------------------------------------------------------
# Input — either typed or via sample-question click
# ---------------------------------------------------------------------------

typed = st.chat_input("Ask a question…")
question = typed or st.session_state.pending_question
if st.session_state.pending_question:
    st.session_state.pending_question = None

if question:
    st.session_state.messages.append({"role": "user", "content": question})
    with st.chat_message("user"):
        st.write(question)

    with st.chat_message("assistant"):
        with st.spinner("Planning, retrieving, and synthesising (10-30 s)…"):
            result = _call_api(question)
        if result is None:
            st.session_state.messages.pop()  # drop the user turn — the API call failed
        else:
            envelope, elapsed = result
            _render_envelope(envelope, elapsed)
            st.session_state.messages.append(
                {"role": "assistant", "envelope": envelope, "elapsed": elapsed}
            )
