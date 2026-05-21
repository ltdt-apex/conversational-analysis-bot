"""FastAPI surface for the conversation analysis bot.

Single endpoint: ``POST /ask`` returns an :class:`AnswerEnvelope`.

Run locally:
    uv run uvicorn backend.api:app --reload --port 8000

The OpenAPI spec is auto-generated at /docs (Swagger UI) and /openapi.json.
"""
from __future__ import annotations

import logging

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from backend import agent
from backend.config import Config
from backend.schemas import AskRequest, AskResponse


logger = logging.getLogger("backend.api")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


app = FastAPI(
    title="Conversation Analysis Bot",
    version="0.1.0",
    description=(
        "Analyst-facing API for the contact-centre conversation analytics "
        "prototype. POST a question to /ask and receive a structured "
        "evidence-backed answer."
    ),
)

# CORS — Streamlit ships at a different origin by default, and curl ignores it
# anyway, so allow-all is the right default for a local prototype.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
def health() -> dict[str, str]:
    """Liveness probe."""
    return {"status": "ok", "service": "conversation-analysis-bot"}


@app.post("/ask", response_model=AskResponse)
def ask(req: AskRequest) -> AskResponse:
    """Answer one analyst question.

    Accepts an optional ``session_id`` for follow-up continuity. If omitted,
    a fresh session id is minted and returned in the response so the caller
    can thread subsequent questions.
    """
    cfg = Config.load()
    try:
        cfg.require_api_key()
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e)) from e

    question = (req.question or "").strip()
    if not question:
        raise HTTPException(status_code=400, detail="question is required")

    logger.info("ask session=%s len=%d", req.session_id, len(question))
    try:
        session_id, envelope = agent.answer_question(
            question, cfg, session_id=req.session_id
        )
    except Exception as e:  # noqa: BLE001 — surface to the client
        logger.exception("agent failure")
        raise HTTPException(status_code=500, detail=f"agent error: {e}") from e
    logger.info(
        "answered session=%s tools=%d evidence=%d",
        session_id, len(envelope.tool_calls), len(envelope.evidence),
    )
    return AskResponse(session_id=session_id, envelope=envelope)
