"""Eval harness for the conversation analysis bot.

Reads ``eval/questions.yaml``, runs each question through the agent loop
in-process (faster + no API dependency), then grades five dimensions:

  1. tool_selection   — at least one expected_tools name appears in trace
  2. answer_coverage  — fraction of expected_keywords that appear in answer
  3. evidence_pass    — len(evidence) >= min_evidence
  4. grounding        — Haiku-as-judge: how well do the quotes support
                        the answer's claims, scored in [0, 1]
  5. latency_s        — wall-clock seconds for the agent call

A question is `pass` if tool_selection AND evidence_pass AND coverage >= 0.5.
Grounding is reported as an INDEPENDENT column — it does not gate pass/fail
(per design discussion). Single grading pass per question; no self-consistency.

Outputs:
  - eval/results-latest.json     (committed; the report cites these numbers)
  - markdown summary on stdout   (paste into the report's Evaluation section)

Run:
    uv run python eval/run_eval.py
    uv run python eval/run_eval.py --limit 3                    # smoke
    uv run python eval/run_eval.py --question-id q10_hinglish_app_crash  # one
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

# Allow running as a script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from anthropic import Anthropic  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

from backend import agent  # noqa: E402
from backend.config import Config  # noqa: E402
from backend.schemas import AnswerEnvelope  # noqa: E402


REPO_ROOT = Path(__file__).resolve().parent.parent
QUESTIONS_PATH = REPO_ROOT / "eval" / "questions.yaml"
RESULTS_PATH = REPO_ROOT / "eval" / "results-latest.json"


# ---------------------------------------------------------------------------
# Grounding grader (Haiku-as-judge)
# ---------------------------------------------------------------------------


class GroundingGrade(BaseModel):
    grounding_score: float = Field(
        ge=0.0,
        le=1.0,
        description=(
            "0.0 = none of the answer's claims are supported by the provided "
            "quotes; 0.5 = some claims supported; 1.0 = every substantive "
            "claim is directly supported by at least one quote."
        ),
    )
    supported_claim_count: int = Field(ge=0)
    unsupported_claim_count: int = Field(ge=0)
    irrelevant_evidence_count: int = Field(
        ge=0,
        description="Evidence items that don't support any claim in the answer.",
    )
    ungrounded_claim_examples: list[str] = Field(
        default_factory=list,
        description="Up to 3 short phrases from the answer that lack a supporting quote.",
    )
    one_line_judgment: str


_GROUNDING_TOOL = {
    "name": "submit_grounding_grade",
    "description": (
        "Submit the grounding evaluation for this answer. Call this exactly once."
    ),
    "input_schema": GroundingGrade.model_json_schema(),
}


_GROUNDING_SYSTEM = """\
You are evaluating a Q&A system that answers analytical questions about a \
customer-support conversation dataset. The system has TWO legitimate sources \
of grounding:

  (A) STRUCTURED DATA from SQL/database tools (you'll see these in the \
      tool_calls trace: query_conversations, query_agents, list_topics, etc.).
      These tools return aggregate statistics — counts, averages, rates, \
      top-N rankings. Numbers and rankings in the answer that match this \
      data type are GROUNDED BY THE TOOL CALL — they do NOT require a quote.
  (B) RETRIEVED CONVERSATION TEXT from semantic_search / get_conversation / \
      get_trajectory. These return verbatim conversation content. Claims that \
      paraphrase or quote specific conversations DO require a matching \
      evidence quote.

Your job:

For each substantive claim in the answer, decide which type of grounding it \
needs, then check whether that grounding is present.

CLAIM TYPES & RULES:

1. Aggregate-statistic claims ("the top 5 topics are X, Y, Z …", \
   "escalation rate is 77.4%", "Billing & Refunds had 220 conversations"):
     → Grounded if an SQL tool (query_conversations / query_agents / \
       list_topics) was called. Quotes are NOT required.

2. Conversation-specific claims ("conversation C0009233 started neutral, \
   dropped to -0.7 by turn 10", "AgentTVAX used scripted replies"):
     → Require a supporting evidence quote (or, for trajectory questions, \
       the get_trajectory call counts as grounding for the per-turn arc).

3. Verbatim phrasings ("the customer said 'Thoda jaldi please'"):
     → REQUIRE a matching quote in the evidence list.

4. Interpretive prose ("this suggests X", "may benefit from coaching", \
   "warrants attention"):
     → NOT a substantive claim. Ignore for grounding purposes.

SCORING:
  - 1.0: every substantive claim is grounded (by tool call OR quote, as \
         appropriate to its type).
  - 0.5: about half of substantive claims are grounded.
  - 0.0: substantive claims are present but none are grounded.

  A pure-aggregation answer with NO evidence quotes BUT WITH SQL tool calls \
  scores 1.0 — that is the correct configuration for that question type.

  An "irrelevant_evidence" item is a quote in the evidence list that doesn't \
  support any claim the answer actually makes.

Return ONLY the submit_grounding_grade tool call. Do not write prose."""


def grade_grounding(
    question: str,
    envelope: AnswerEnvelope,
    *,
    client: Anthropic,
    model: str,
) -> GroundingGrade:
    """One Haiku call → one GroundingGrade for this question."""
    evidence_lines = [
        f"- [{e.conv_id}] {e.quote!r}  (relevance: {e.relevance})"
        for e in envelope.evidence
    ] or ["(none provided)"]
    tool_lines = [
        f"- {t.tool}({list(t.arguments.keys())}) → {t.result_summary}"
        for t in envelope.tool_calls
    ] or ["(none)"]
    user_prompt = (
        f"QUESTION:\n{question.strip()}\n\n"
        f"ANSWER:\n{envelope.answer.strip()}\n\n"
        f"TOOL CALLS THE BOT MADE:\n" + "\n".join(tool_lines) + "\n\n"
        f"EVIDENCE QUOTES PROVIDED:\n" + "\n".join(evidence_lines)
    )
    resp = client.messages.create(
        model=model,
        max_tokens=1024,
        system=_GROUNDING_SYSTEM,
        tools=[_GROUNDING_TOOL],
        tool_choice={"type": "tool", "name": "submit_grounding_grade"},
        messages=[{"role": "user", "content": user_prompt}],
    )
    for block in resp.content:
        if block.type == "tool_use" and block.name == "submit_grounding_grade":
            return GroundingGrade.model_validate(block.input)
    raise RuntimeError("grader did not call submit_grounding_grade")


# ---------------------------------------------------------------------------
# Per-question evaluation
# ---------------------------------------------------------------------------


@dataclass
class QuestionResult:
    id: str
    category: str
    language: str
    question: str
    # Agent outputs
    answer: str
    evidence_count: int
    tools_used: list[str]
    latency_s: float
    # Scoring
    tool_selection_pass: bool
    coverage: float
    coverage_matched: list[str]
    coverage_missed: list[str]
    evidence_pass: bool
    grounding: GroundingGrade | None
    status: str  # pass | partial | fail
    error: str | None = None


def _check_tool_selection(
    expected: list[str], envelope: AnswerEnvelope
) -> bool:
    used = {t.tool for t in envelope.tool_calls}
    return any(e in used for e in expected)


def _check_coverage(
    keywords: list[str], answer: str
) -> tuple[float, list[str], list[str]]:
    if not keywords:
        return 1.0, [], []
    answer_lc = answer.lower()
    matched, missed = [], []
    for kw in keywords:
        (matched if kw.lower() in answer_lc else missed).append(kw)
    return len(matched) / len(keywords), matched, missed


def _status(tool_pass: bool, coverage: float, evidence_pass: bool) -> str:
    n = sum([tool_pass, coverage >= 0.5, evidence_pass])
    if n == 3:
        return "pass"
    if n == 2:
        return "partial"
    return "fail"


def evaluate_one(
    spec: dict[str, Any],
    cfg: Config,
    *,
    grader_client: Anthropic,
    grader_model: str,
    skip_grading: bool = False,
) -> QuestionResult:
    question = spec["question"].strip()
    expected_tools = spec.get("expected_tools", []) or []
    keywords = spec.get("expected_keywords", []) or []
    min_evidence = int(spec.get("min_evidence", 0) or 0)

    t0 = time.time()
    try:
        # Fresh session per question — no cross-talk through memory.
        _, envelope = agent.answer_question(question, cfg, session_id=None)
        latency = time.time() - t0
        error = None
    except Exception as e:  # noqa: BLE001 — surface in the report, don't kill the harness
        return QuestionResult(
            id=spec["id"],
            category=spec.get("category", ""),
            language=spec.get("language", ""),
            question=question,
            answer="",
            evidence_count=0,
            tools_used=[],
            latency_s=time.time() - t0,
            tool_selection_pass=False,
            coverage=0.0,
            coverage_matched=[],
            coverage_missed=keywords,
            evidence_pass=False,
            grounding=None,
            status="fail",
            error=f"{type(e).__name__}: {e}",
        )

    tool_pass = _check_tool_selection(expected_tools, envelope)
    coverage, matched, missed = _check_coverage(keywords, envelope.answer)
    evidence_pass = len(envelope.evidence) >= min_evidence
    status = _status(tool_pass, coverage, evidence_pass)

    grounding: GroundingGrade | None = None
    if not skip_grading:
        try:
            grounding = grade_grounding(
                question, envelope, client=grader_client, model=grader_model
            )
        except Exception as e:  # noqa: BLE001 — partial result is better than none
            grounding = GroundingGrade(
                grounding_score=0.0,
                supported_claim_count=0,
                unsupported_claim_count=0,
                irrelevant_evidence_count=0,
                ungrounded_claim_examples=[],
                one_line_judgment=f"grader error: {type(e).__name__}: {e}",
            )

    return QuestionResult(
        id=spec["id"],
        category=spec.get("category", ""),
        language=spec.get("language", ""),
        question=question,
        answer=envelope.answer,
        evidence_count=len(envelope.evidence),
        tools_used=[t.tool for t in envelope.tool_calls],
        latency_s=round(latency, 2),
        tool_selection_pass=tool_pass,
        coverage=round(coverage, 3),
        coverage_matched=matched,
        coverage_missed=missed,
        evidence_pass=evidence_pass,
        grounding=grounding,
        status=status,
        error=None,
    )


# ---------------------------------------------------------------------------
# Aggregation & reporting
# ---------------------------------------------------------------------------


@dataclass
class EvalSummary:
    n_questions: int
    pass_rate: float
    partial_rate: float
    fail_rate: float
    mean_latency_s: float
    median_latency_s: float
    p95_latency_s: float
    mean_grounding: float | None
    pass_rate_by_category: dict[str, float] = field(default_factory=dict)
    pass_rate_by_language: dict[str, float] = field(default_factory=dict)


def summarise(results: list[QuestionResult]) -> EvalSummary:
    n = len(results)
    if n == 0:
        return EvalSummary(0, 0, 0, 0, 0, 0, 0, None)

    statuses = [r.status for r in results]
    latencies = sorted(r.latency_s for r in results)
    grounding_scores = [
        r.grounding.grounding_score for r in results if r.grounding is not None
    ]

    def pct_pass(rows: list[QuestionResult]) -> float:
        return (
            round(sum(1 for r in rows if r.status == "pass") / len(rows), 3)
            if rows
            else 0.0
        )

    by_cat: dict[str, list[QuestionResult]] = {}
    by_lang: dict[str, list[QuestionResult]] = {}
    for r in results:
        by_cat.setdefault(r.category, []).append(r)
        by_lang.setdefault(r.language, []).append(r)

    p95_idx = max(0, int(0.95 * (len(latencies) - 1)))
    return EvalSummary(
        n_questions=n,
        pass_rate=round(statuses.count("pass") / n, 3),
        partial_rate=round(statuses.count("partial") / n, 3),
        fail_rate=round(statuses.count("fail") / n, 3),
        mean_latency_s=round(statistics.fmean(latencies), 2),
        median_latency_s=round(statistics.median(latencies), 2),
        p95_latency_s=round(latencies[p95_idx], 2),
        mean_grounding=(
            round(statistics.fmean(grounding_scores), 3)
            if grounding_scores
            else None
        ),
        pass_rate_by_category={c: pct_pass(rs) for c, rs in by_cat.items()},
        pass_rate_by_language={l: pct_pass(rs) for l, rs in by_lang.items()},
    )


def print_markdown_table(results: list[QuestionResult]) -> None:
    print()
    print(
        "| id | category | lang | tools | cov | ev | ground | latency | status |"
    )
    print(
        "|----|----------|------|-------|-----|----|--------|---------|--------|"
    )
    for r in results:
        tools_short = "✓" if r.tool_selection_pass else "✗"
        ground = (
            f"{r.grounding.grounding_score:.2f}" if r.grounding else "—"
        )
        icon = {"pass": "✅", "partial": "🟡", "fail": "❌"}[r.status]
        print(
            f"| {r.id} | {r.category} | {r.language} | {tools_short} | "
            f"{r.coverage:.2f} | {r.evidence_count} | {ground} | "
            f"{r.latency_s:.1f}s | {icon} {r.status} |"
        )


def print_rollups(summary: EvalSummary) -> None:
    print()
    print(f"**Overall**: {summary.pass_rate:.0%} pass · "
          f"{summary.partial_rate:.0%} partial · "
          f"{summary.fail_rate:.0%} fail "
          f"({summary.n_questions} questions)")
    print(f"**Latency**: mean {summary.mean_latency_s}s · "
          f"median {summary.median_latency_s}s · "
          f"p95 {summary.p95_latency_s}s")
    if summary.mean_grounding is not None:
        print(f"**Grounding (Haiku judge)**: mean {summary.mean_grounding:.2f}")
    print()
    print("**Pass rate by category**")
    for cat, rate in sorted(summary.pass_rate_by_category.items()):
        print(f"  - {cat:22s} {rate:.0%}")
    print()
    print("**Pass rate by language**")
    for lang, rate in sorted(summary.pass_rate_by_language.items()):
        print(f"  - {lang:8s} {rate:.0%}")


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def save_results(
    results: list[QuestionResult],
    summary: EvalSummary,
    *,
    path: Path = RESULTS_PATH,
    grader_model: str | None = None,
) -> None:
    payload = {
        "run_ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "grader_model": grader_model,
        "summary": asdict(summary),
        "results": [
            {
                **asdict(r),
                "grounding": r.grounding.model_dump() if r.grounding else None,
            }
            for r in results
        ],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nwrote {path.relative_to(REPO_ROOT)}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    ap = argparse.ArgumentParser(description="Run the bot evaluation set")
    ap.add_argument("--limit", type=int, default=None,
                    help="Run only the first N questions (smoke test).")
    ap.add_argument("--question-id", default=None,
                    help="Run only this question id.")
    ap.add_argument("--skip-grading", action="store_true",
                    help="Skip the Haiku grounding pass.")
    ap.add_argument("--grader-model", default=None,
                    help="Override grader model (default: cfg.classifier_model = Haiku).")
    args = ap.parse_args()

    cfg = Config.load()
    cfg.require_api_key()
    grader_model = args.grader_model or cfg.classifier_model
    grader_client = Anthropic(api_key=cfg.anthropic_api_key)

    spec = yaml.safe_load(QUESTIONS_PATH.read_text(encoding="utf-8"))
    questions = spec["questions"]
    if args.question_id:
        questions = [q for q in questions if q["id"] == args.question_id]
        if not questions:
            print(f"no question matched id={args.question_id!r}", file=sys.stderr)
            sys.exit(2)
    if args.limit:
        questions = questions[: args.limit]

    print(f"[eval] running {len(questions)} question(s) "
          f"agent_model={cfg.planner_model} "
          f"grader_model={'(skipped)' if args.skip_grading else grader_model}")

    results: list[QuestionResult] = []
    for i, q in enumerate(questions, 1):
        print(f"[eval] {i:2d}/{len(questions)}  {q['id']}  …", flush=True)
        r = evaluate_one(
            q,
            cfg,
            grader_client=grader_client,
            grader_model=grader_model,
            skip_grading=args.skip_grading,
        )
        ground = (
            f" ground={r.grounding.grounding_score:.2f}" if r.grounding else ""
        )
        print(
            f"           → {r.status:7s} cov={r.coverage:.2f} "
            f"ev={r.evidence_count} tools={len(r.tools_used)}"
            f"{ground} ({r.latency_s:.1f}s)"
        )
        if r.error:
            print(f"           ERROR: {r.error}")
        results.append(r)

    summary = summarise(results)
    print_markdown_table(results)
    print_rollups(summary)
    save_results(
        results,
        summary,
        grader_model=None if args.skip_grading else grader_model,
    )


if __name__ == "__main__":
    main()
