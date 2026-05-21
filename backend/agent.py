"""Planner-Synthesizer agent loop.

Single Anthropic call orchestrates everything: Sonnet picks tools, reads
their results, decides whether to call more tools or finalise. The final
answer is emitted via a synthetic ``emit_answer`` tool whose ``input_schema``
matches :class:`backend.schemas.AnswerEnvelope` — that forces the model to
return structured JSON rather than free prose.

Design knobs:
  - max 8 tool iterations (prevents runaway loops)
  - tool result payloads truncated when very large (keeps context window
    manageable)
  - the system prompt embeds: the analytical agent's role, the bilingual
    handling contract, the "prefer concrete examples over caveats"
    preference saved to memory, and a thumbnail of the 15-category
    taxonomy so the planner doesn't always need to call list_topics
"""
from __future__ import annotations

import json
from typing import Any

from anthropic import Anthropic

from backend import memory, tools
from backend import taxonomy as tax
from backend.config import Config
from backend.schemas import (
    AnswerEnvelope,
    EvidenceItem,
    ToolCallTrace,
)


MAX_TOOL_ITERATIONS = 8
MAX_TOOL_RESULT_CHARS = 8_000  # truncate huge results before showing to the LLM


# ---------------------------------------------------------------------------
# emit_answer — the structured-output sink
# ---------------------------------------------------------------------------


def _emit_answer_definition() -> dict[str, Any]:
    """The tool the model calls to deliver its final structured answer."""
    return {
        "name": "emit_answer",
        "description": (
            "Emit the final answer to the analyst. Call this exactly ONCE, "
            "when you have gathered enough evidence. Do NOT call any other "
            "tool after this. Populate every field; use a one-sentence "
            "reasoning_brief; include 1-3 EvidenceItem entries with verbatim "
            "quote text whenever you have retrieved conversation content."
        ),
        "input_schema": AnswerEnvelope.model_json_schema(),
    }


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------


def _system_prompt(cfg: Config) -> str:
    taxonomy = tax.load(cfg.data_dir / "topic_taxonomy.yaml")
    topic_lines = "\n".join(
        f"  - {c.id:26s} {c.label}" for c in taxonomy.categories
    )
    return f"""\
You are a contact-centre analytics assistant. Analysts ask you questions about
a 3,000-conversation customer-support dataset and you return clear,
evidence-backed answers.

DATASET FACTS (do not ask the user about these):
  - 3,000 conversations, 41,965 turns, dated 2025-08-01 to 2025-12-02.
  - "Last month in the dataset" = November 2025.
  - Bilingual: ~84% English, ~15% Hindi/Hinglish, ~1% mixed. Preserve the
    original language in evidence quotes; only translate when the question is
    explicitly English-scoped.
  - Per-agent conversation count is sparse: 2,974 agents handle 1 conversation
    each, 13 handle 2, none handle 3+. For agent-ranking questions, prefer
    surfacing a concrete illustrative example over statistical disclaimers.
  - Topic taxonomy is closed-set (15 ids):
{topic_lines}

YOUR WORKFLOW:
  1. PLAN: read the question, decide what kind of data answers it (aggregate
     counts? specific conversation arc? evidence quotes?).
  2. RETRIEVE: call the tools below to gather data. Combine SQL aggregation
     with semantic_search to get both numbers AND example conversations.
  3. SYNTHESIZE: call emit_answer once with the final structured envelope.

TOOL-USE RULES:
  - Call tools in a single chain — do not ask the user for clarifications.
  - Use list_topics when you need valid topic ids.
  - When a question asks for examples/evidence/why, use semantic_search and
    then get_conversation to read a representative conversation.
  - For trajectory questions (how did sentiment change in conv X), use
    get_trajectory rather than get_conversation.
  - When grouping by topic and looking for "concerning" topics, combine
    count, avg_sentiment, and escalation_rate metrics.
  - Maximum {MAX_TOOL_ITERATIONS} tool calls before you MUST emit_answer.

ANSWER STYLE:
  - 1–3 short paragraphs in the `answer` field.
  - 1–3 entries in `evidence`, each with a verbatim quote in the conversation's
    original language (you can also paraphrase in English in the `relevance`
    note).
  - `reasoning_brief`: one sentence describing how you arrived at the answer.
  - `uncertainty`: only set when data is genuinely thin or contradictory; do
    NOT use it to disclaim agent sample sizes — the user prefers a concrete
    example to a statistical caveat.
"""


# ---------------------------------------------------------------------------
# Conversation context
# ---------------------------------------------------------------------------


def _build_messages(
    question: str, history: list[tuple[str, AnswerEnvelope]]
) -> list[dict[str, Any]]:
    """Prepend recent session history as alternating user/assistant turns."""
    msgs: list[dict[str, Any]] = []
    for prior_q, prior_env in history:
        msgs.append({"role": "user", "content": prior_q})
        # The assistant's prior turn is just the answer text — the structured
        # fields are useful to the human caller but bloat the conversation.
        msgs.append({"role": "assistant", "content": prior_env.answer})
    msgs.append({"role": "user", "content": question})
    return msgs


# ---------------------------------------------------------------------------
# Tool result formatting
# ---------------------------------------------------------------------------


def _serialise_tool_result(result: Any) -> str:
    """Convert tool output to a compact JSON string for the LLM."""
    from pydantic import BaseModel

    def _coerce(obj: Any) -> Any:
        if isinstance(obj, BaseModel):
            return obj.model_dump()
        if isinstance(obj, list):
            return [_coerce(x) for x in obj]
        if isinstance(obj, dict):
            return {k: _coerce(v) for k, v in obj.items()}
        return obj

    s = json.dumps(_coerce(result), ensure_ascii=False, default=str)
    if len(s) > MAX_TOOL_RESULT_CHARS:
        s = s[:MAX_TOOL_RESULT_CHARS] + f"... [truncated, total {len(s)} chars]"
    return s


def _summarise_for_trace(result: Any) -> str:
    """Short human-readable summary stored in the response envelope."""
    if isinstance(result, list):
        return f"{len(result)} row(s)"
    from pydantic import BaseModel

    if isinstance(result, BaseModel):
        name = type(result).__name__
        if hasattr(result, "conv_id"):
            return f"{name}(conv_id={result.conv_id})"
        return name
    return str(result)[:120]


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------


def answer_question(
    question: str,
    cfg: Config,
    *,
    session_id: str | None = None,
) -> tuple[str, AnswerEnvelope]:
    """Run the planner-synthesizer loop. Returns (session_id, envelope)."""
    if not session_id:
        session_id = memory.new_session_id()

    client = Anthropic(api_key=cfg.require_api_key())
    history = memory.load_recent(cfg, session_id, n=3)
    messages = _build_messages(question, history)
    tool_defs = tools.all_definitions() + [_emit_answer_definition()]
    trace: list[ToolCallTrace] = []

    final_envelope: AnswerEnvelope | None = None

    for iteration in range(MAX_TOOL_ITERATIONS + 1):
        resp = client.messages.create(
            model=cfg.planner_model,
            max_tokens=4096,
            system=_system_prompt(cfg),
            tools=tool_defs,
            messages=messages,
        )

        # Collect any tool uses from this turn.
        tool_uses: list[Any] = [b for b in resp.content if b.type == "tool_use"]

        # No tool use → the model returned prose without calling emit_answer.
        # That's a model failure; we wrap whatever it said.
        if not tool_uses:
            prose = "".join(b.text for b in resp.content if b.type == "text").strip()
            final_envelope = AnswerEnvelope(
                answer=prose or "I couldn't gather enough data to answer.",
                evidence=[],
                tool_calls=trace,
                reasoning_brief="Model returned prose without calling emit_answer.",
                uncertainty="The agent loop did not produce a structured answer.",
            )
            break

        # Append the assistant turn verbatim so the next round of tool_result
        # blocks line up with the tool_use ids.
        messages.append({"role": "assistant", "content": resp.content})

        tool_results: list[dict[str, Any]] = []
        emitted = False
        for tu in tool_uses:
            if tu.name == "emit_answer":
                final_envelope = AnswerEnvelope.model_validate(tu.input)
                final_envelope.tool_calls = trace
                emitted = True
                break
            try:
                result = tools.call(tu.name, tu.input or {}, cfg)
                payload = _serialise_tool_result(result)
                summary = _summarise_for_trace(result)
                trace.append(
                    ToolCallTrace(
                        tool=tu.name,
                        arguments=dict(tu.input or {}),
                        result_summary=summary,
                    )
                )
            except Exception as e:  # noqa: BLE001 — tool errors must be visible to the model
                payload = json.dumps({"error": f"{type(e).__name__}: {e}"})
                trace.append(
                    ToolCallTrace(
                        tool=tu.name,
                        arguments=dict(tu.input or {}),
                        result_summary=f"ERROR: {e}",
                    )
                )
            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": tu.id,
                    "content": payload,
                }
            )
        if emitted:
            break
        messages.append({"role": "user", "content": tool_results})

        # Hard cap: if we've hit max iterations without emit_answer, force one
        # last turn where the model MUST emit. We re-issue with a nudge.
        if iteration == MAX_TOOL_ITERATIONS - 1:
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "You have reached the maximum number of tool calls. "
                        "Call emit_answer NOW with whatever data you have. "
                        "Note any limitations in the `uncertainty` field."
                    ),
                }
            )

    if final_envelope is None:
        final_envelope = AnswerEnvelope(
            answer="The agent loop exhausted its iteration budget without producing an answer.",
            evidence=[],
            tool_calls=trace,
            reasoning_brief="Iteration cap reached without emit_answer.",
            uncertainty="Increase MAX_TOOL_ITERATIONS or simplify the question.",
        )

    # Persist to session memory for follow-ups.
    memory.save_turn(cfg, session_id, question, final_envelope)
    return session_id, final_envelope


# Compatibility shim — keeps the EvidenceItem name re-exported for FastAPI.
__all__ = ["answer_question", "AnswerEnvelope", "EvidenceItem"]
