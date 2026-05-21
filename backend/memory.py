"""Session-scoped memory for follow-up questions.

Persisted to the ``sessions`` SQLite table (schema in :mod:`backend.db`).
Each row stores one analyst question + the structured answer envelope
emitted by the agent. Expires after 24h by default.

Anti-leak design:
  - **Session-scoped:** keyed by ``session_id``; rows for one session never
    surface in another.
  - **TTL:** rows past ``expires_at`` are filtered out on read and lazily
    deleted on write.
  - **PII redaction:** before persisting, we run a light regex pass to
    replace email addresses, phone-like digit strings, and credit-card-like
    numeric runs with placeholders. The dataset itself uses synthetic
    customer/agent ids (``CustXXX`` / ``AgentXXX``) which are NOT redacted
    because they are not real PII and are needed for follow-up continuity.
"""
from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timedelta, timezone

from backend.config import Config
from backend.db import connect, init_schema, transaction
from backend.schemas import AnswerEnvelope


_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_PHONE_RE = re.compile(r"\b\+?\d[\d\s().-]{7,}\d\b")
_CC_RE = re.compile(r"\b(?:\d[ -]?){13,19}\b")


def redact_pii(text: str) -> str:
    """Replace common PII shapes with placeholders. Idempotent."""
    if not text:
        return text
    out = _EMAIL_RE.sub("[REDACTED_EMAIL]", text)
    out = _CC_RE.sub("[REDACTED_CARD]", out)
    out = _PHONE_RE.sub("[REDACTED_PHONE]", out)
    return out


def new_session_id() -> str:
    return uuid.uuid4().hex[:16]


def save_turn(
    cfg: Config,
    session_id: str,
    question: str,
    envelope: AnswerEnvelope,
) -> int:
    """Append a question/answer pair to ``session_id``'s history. Returns turn_idx."""
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(hours=cfg.memory_ttl_hours)
    with connect(cfg.sqlite_path) as conn:
        init_schema(conn)
        # Purge expired rows opportunistically.
        conn.execute(
            "DELETE FROM sessions WHERE expires_at < ?", (now.isoformat(),)
        )
        next_idx = conn.execute(
            "SELECT COALESCE(MAX(turn_idx), -1) + 1 FROM sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()[0]
        redacted_q = redact_pii(question)
        envelope_json = envelope.model_dump_json()
        # Redact PII inside the serialised envelope too (answer + evidence quotes).
        redacted_envelope = redact_pii(envelope_json)
        with transaction(conn):
            conn.execute(
                """INSERT INTO sessions
                     (session_id, turn_idx, created_at, expires_at,
                      question, answer_json)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    session_id,
                    next_idx,
                    now.isoformat(),
                    expires_at.isoformat(),
                    redacted_q,
                    redacted_envelope,
                ),
            )
        return next_idx


def load_recent(
    cfg: Config, session_id: str, *, n: int = 3
) -> list[tuple[str, AnswerEnvelope]]:
    """Return the last ``n`` (question, envelope) pairs for ``session_id``.

    Expired rows are filtered out. Order is chronological (oldest first).
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    with connect(cfg.sqlite_path) as conn:
        init_schema(conn)
        rows = conn.execute(
            """SELECT question, answer_json FROM sessions
                WHERE session_id = ? AND expires_at >= ?
                ORDER BY turn_idx DESC LIMIT ?""",
            (session_id, now_iso, n),
        ).fetchall()
    out: list[tuple[str, AnswerEnvelope]] = []
    for r in reversed(rows):
        try:
            env = AnswerEnvelope.model_validate(json.loads(r["answer_json"]))
        except (json.JSONDecodeError, ValueError):
            continue
        out.append((r["question"], env))
    return out
