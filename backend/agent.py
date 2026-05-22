"""Planner-Synthesizer agent loop.

One Anthropic call orchestrates everything: Sonnet picks tools, reads their
results, decides whether to call more tools or finalise. The final answer is
emitted via a synthetic ``emit_answer`` tool whose ``input_schema`` matches
:class:`backend.schemas.AnswerEnvelope` — that forces the model to return
structured JSON rather than free prose.

Read order:
  * :func:`answer_question`     — public entry point (~10 lines)
  * :func:`_run_planner_loop`   — the iteration loop (~25 lines)
  * :func:`_process_tool_uses`  — fan out one turn's tool_use blocks
  * :func:`_execute_one_tool`   — call one tool, format result for the model
  * the various ``_wrap_*`` /  ``_*_envelope`` helpers handle the edge cases
    (no tool_use, max iterations exhausted, force-emit nudge).

Design knobs:
  * max 8 tool iterations (plus 1 final forced-emit turn — see
    ``MAX_TOOL_ITERATIONS``)
  * tool result payloads truncated when very large (keeps context window
    manageable)
  * the system prompt embeds: the analytical agent's role, the bilingual
    handling contract, the "prefer concrete examples over caveats"
    preference saved to memory, and a thumbnail of the 15-category taxonomy
"""
from __future__ import annotations

import json
from typing import Any

from anthropic import Anthropic
from pydantic import BaseModel

from backend import memory, tools
from backend.preprocessing import taxonomy as tax
from backend.config import Config
from backend.schemas import AnswerEnvelope, EvidenceItem, ToolCallTrace


MAX_TOOL_ITERATIONS = 8
MAX_TOOL_RESULT_CHARS = 8_000


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def answer_question(
    question: str,
    cfg: Config,
    *,
    session_id: str | None = None,
) -> tuple[str, AnswerEnvelope]:
    """Run the planner-synthesizer loop and persist the result to session memory.

    Returns ``(session_id, envelope)``. A fresh ``session_id`` is minted when
    none is supplied so the caller can thread follow-up questions.
    """
    session_id = session_id or memory.new_session_id()

    client = Anthropic(api_key=cfg.require_api_key())
    history = memory.load_recent(cfg, session_id, n=3)
    messages = _build_messages(question, history)
    tool_defs = tools.all_definitions() + [_emit_answer_definition()]
    trace: list[ToolCallTrace] = []

    envelope = _run_planner_loop(client, cfg, messages, tool_defs, trace)
    memory.save_turn(cfg, session_id, question, envelope)
    return session_id, envelope


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------


def _run_planner_loop(
    client: Anthropic,
    cfg: Config,
    messages: list[dict[str, Any]],
    tool_defs: list[dict[str, Any]],
    trace: list[ToolCallTrace],
) -> AnswerEnvelope:
    """Iterate planner → tool calls until ``emit_answer`` or budget exhausted."""
    for iteration in range(MAX_TOOL_ITERATIONS + 1):
        resp = client.messages.create(
            model=cfg.planner_model,
            max_tokens=4096,
            system=_system_prompt(cfg),
            tools=tool_defs,
            messages=messages,
        )
        tool_uses = [b for b in resp.content if b.type == "tool_use"]

        # Bare prose without any tool_use → wrap whatever the model said.
        if not tool_uses:
            return _wrap_bare_prose(resp, trace)

        # The assistant turn (tool_use blocks) must be appended verbatim so the
        # next round's tool_result blocks can reference the right tool_use ids.
        messages.append({"role": "assistant", "content": resp.content})

        envelope, tool_results = _process_tool_uses(tool_uses, cfg, trace)
        if envelope is not None:
            envelope.tool_calls = trace
            return envelope

        messages.append({"role": "user", "content": tool_results})

        # One iteration before the cap, nudge the model to call emit_answer
        # on its next turn.
        if iteration == MAX_TOOL_ITERATIONS - 1:
            messages.append(_force_emit_nudge())

    return _exhausted_envelope(trace)


def _process_tool_uses(
    tool_uses: list[Any],
    cfg: Config,
    trace: list[ToolCallTrace],
) -> tuple[AnswerEnvelope | None, list[dict[str, Any]]]:
    """Run each tool_use block from this turn.

    Returns ``(envelope, tool_results)``:
      * If one of the blocks is ``emit_answer``, ``envelope`` is the
        validated :class:`AnswerEnvelope` and we stop processing this turn.
      * Otherwise ``envelope`` is None and ``tool_results`` holds the
        ``tool_result`` blocks the caller should feed back into the next turn.
    """
    tool_results: list[dict[str, Any]] = []
    for tu in tool_uses:
        if tu.name == "emit_answer":
            return AnswerEnvelope.model_validate(tu.input), tool_results

        payload, summary = _execute_one_tool(tu, cfg)
        trace.append(
            ToolCallTrace(
                tool=tu.name,
                arguments=dict(tu.input or {}),
                result_summary=summary,
            )
        )
        tool_results.append(
            {
                "type": "tool_result",
                "tool_use_id": tu.id,
                "content": payload,
            }
        )
    return None, tool_results


def _execute_one_tool(tu: Any, cfg: Config) -> tuple[str, str]:
    """Call one tool. Returns ``(json_payload_for_model, trace_summary)``.

    Tool errors are intentionally surfaced to the model rather than raised —
    the planner can recover by trying a different tool or arguments.
    """
    try:
        result = tools.call(tu.name, tu.input or {}, cfg)
        return _serialise_tool_result(result), _summarise_for_trace(result)
    except Exception as e:  # noqa: BLE001 — tool errors must be visible to the model
        return (
            json.dumps({"error": f"{type(e).__name__}: {e}"}),
            f"ERROR: {e}",
        )


# ---------------------------------------------------------------------------
# Envelope helpers — these handle the loop's three terminal states
# ---------------------------------------------------------------------------


def _wrap_bare_prose(resp: Any, trace: list[ToolCallTrace]) -> AnswerEnvelope:
    """The model returned prose without calling any tool — surface it anyway."""
    prose = "".join(b.text for b in resp.content if b.type == "text").strip()
    return AnswerEnvelope(
        answer=prose or "I couldn't gather enough data to answer.",
        evidence=[],
        tool_calls=trace,
        reasoning_brief="Model returned prose without calling emit_answer.",
        uncertainty="The agent loop did not produce a structured answer.",
    )


def _force_emit_nudge() -> dict[str, Any]:
    """One-shot reminder appended just before the final iteration."""
    return {
        "role": "user",
        "content": (
            "You have reached the maximum number of tool calls. "
            "Call emit_answer NOW with whatever data you have. "
            "Note any limitations in the `uncertainty` field."
        ),
    }


def _exhausted_envelope(trace: list[ToolCallTrace]) -> AnswerEnvelope:
    """The loop ran its full budget without the model ever calling emit_answer."""
    return AnswerEnvelope(
        answer="The agent loop exhausted its iteration budget without producing an answer.",
        evidence=[],
        tool_calls=trace,
        reasoning_brief="Iteration cap reached without emit_answer.",
        uncertainty="Increase MAX_TOOL_ITERATIONS or simplify the question.",
    )


# ---------------------------------------------------------------------------
# emit_answer — the structured-output sink
# ---------------------------------------------------------------------------


def _emit_answer_definition() -> dict[str, Any]:
    """The synthetic tool the model calls to deliver its final structured answer."""
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
# Message + tool-result formatting
# ---------------------------------------------------------------------------


def _build_messages(
    question: str, history: list[tuple[str, AnswerEnvelope]]
) -> list[dict[str, Any]]:
    """Prepend recent session history as alternating user/assistant turns.

    We pass only the prose answer for prior assistant turns — the structured
    evidence / tool-call fields are useful to the human caller but bloat the
    conversation context for the model.
    """
    msgs: list[dict[str, Any]] = []
    for prior_q, prior_env in history:
        msgs.append({"role": "user", "content": prior_q})
        msgs.append({"role": "assistant", "content": prior_env.answer})
    msgs.append({"role": "user", "content": question})
    return msgs


def _serialise_tool_result(result: Any) -> str:
    """Convert a Pydantic / list / dict tool result to compact JSON for the LLM.

    Oversized payloads are truncated with a length marker so the context window
    stays predictable even on accidentally-large queries.
    """

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
    """Short human-readable summary stored in the response envelope's trace."""
    if isinstance(result, list):
        return f"{len(result)} row(s)"
    if isinstance(result, BaseModel):
        name = type(result).__name__
        if hasattr(result, "conv_id"):
            return f"{name}(conv_id={result.conv_id})"
        return name
    return str(result)[:120]


# Compatibility shim — re-export EvidenceItem so backend.api can import it from here.
__all__ = ["answer_question", "AnswerEnvelope", "EvidenceItem"]
