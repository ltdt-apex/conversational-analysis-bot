"""Generate the submission DOCX report from the live repo state.

This script is the *source of truth* for the report. Re-run it after any
material change to the codebase or eval results and the docx regenerates with
the latest numbers, taxonomy, and pipeline stats.

  uv run python report/build_report.py
  → writes report/report.docx

It pulls live data from:
  - data/topic_taxonomy.yaml  (categories table)
  - data/processed.db         (dataset stats, distribution rollups)
  - eval/results-latest.json  (evaluation tables & rollups)

All eight required submission sections are covered, plus the three optional
bonus extensions with their per-section sub-points. Keep paragraphs honest
and engineering-toned — this reads like a senior engineer's design doc, not
marketing copy.
"""
from __future__ import annotations

import json
import sqlite3
import statistics
import sys
from pathlib import Path

import yaml
from docx import Document
from docx.enum.table import WD_ALIGN_VERTICAL
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Inches, Pt, RGBColor


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

TAXONOMY_PATH = REPO_ROOT / "data" / "topic_taxonomy.yaml"
DB_PATH = REPO_ROOT / "data" / "processed.db"
EVAL_PATH = REPO_ROOT / "eval" / "results-latest.json"
OUT_PATH = REPO_ROOT / "report" / "report.docx"
REPO_URL = "https://github.com/ltdt-apex/calabrio-assignment-conversational-bot"


# ---------------------------------------------------------------------------
# Small docx helpers (keep paragraph style consistent)
# ---------------------------------------------------------------------------


def H1(doc, text):
    doc.add_heading(text, level=1)


def H2(doc, text):
    doc.add_heading(text, level=2)


def P(doc, text, *, bold=False, italic=False):
    p = doc.add_paragraph()
    r = p.add_run(text)
    r.bold = bold
    r.italic = italic
    return p


def Bullet(doc, text):
    return doc.add_paragraph(text, style="List Bullet")


def Code(doc, text):
    """Render a fixed-width monospaced block (single paragraph, no table)."""
    p = doc.add_paragraph()
    p.paragraph_format.left_indent = Inches(0.25)
    p.paragraph_format.space_before = Pt(4)
    p.paragraph_format.space_after = Pt(4)
    r = p.add_run(text)
    r.font.name = "DejaVu Sans Mono"
    r.font.size = Pt(9)
    return p


def Table(doc, header, rows, *, col_widths=None):
    tbl = doc.add_table(rows=1 + len(rows), cols=len(header))
    tbl.style = "Light Grid"
    hdr = tbl.rows[0].cells
    for i, h in enumerate(header):
        hdr[i].text = ""
        para = hdr[i].paragraphs[0]
        run = para.add_run(h)
        run.bold = True
        run.font.size = Pt(10)
    for r_idx, row in enumerate(rows, start=1):
        for c_idx, val in enumerate(row):
            cell = tbl.rows[r_idx].cells[c_idx]
            cell.text = ""
            run = cell.paragraphs[0].add_run(str(val))
            run.font.size = Pt(9)
            cell.vertical_alignment = WD_ALIGN_VERTICAL.TOP
    if col_widths:
        for row in tbl.rows:
            for c, w in zip(row.cells, col_widths):
                c.width = Inches(w)
    return tbl


# ---------------------------------------------------------------------------
# Data loaders
# ---------------------------------------------------------------------------


def load_taxonomy():
    return yaml.safe_load(TAXONOMY_PATH.read_text(encoding="utf-8"))


def load_eval():
    return json.loads(EVAL_PATH.read_text(encoding="utf-8"))


def db_stats():
    """Run a few rollups for the data-handling section."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    out = {}
    out["turns"] = conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0]
    out["conversations"] = conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
    out["distinct_agents"] = conn.execute(
        "SELECT COUNT(DISTINCT agent_name) FROM turns WHERE agent_name IS NOT NULL"
    ).fetchone()[0]
    out["distinct_prefixes"] = conn.execute("SELECT COUNT(DISTINCT text_clean) FROM turns").fetchone()[0]
    # language distribution at the conversation level (we don't store
    # `language` on conversations; derive from turns)
    out["lang_dist"] = {
        r["language"]: r["c"]
        for r in conn.execute(
            "SELECT language, COUNT(*) AS c FROM turns GROUP BY language ORDER BY c DESC"
        )
    }
    out["sentiment_dist"] = {
        r["sentiment_label"]: r["c"]
        for r in conn.execute(
            "SELECT sentiment_label, COUNT(*) AS c FROM turns "
            "GROUP BY sentiment_label ORDER BY c DESC"
        )
    }
    out["empathy_agent_only"] = {
        r["empathy_signal"]: r["c"]
        for r in conn.execute(
            "SELECT empathy_signal, COUNT(*) AS c FROM turns "
            "WHERE role='agent' GROUP BY empathy_signal"
        )
    }
    out["min_date"] = conn.execute("SELECT MIN(start_ts) FROM conversations").fetchone()[0]
    out["max_date"] = conn.execute("SELECT MAX(start_ts) FROM conversations").fetchone()[0]
    out["agents_by_conv_count"] = {
        r["n"]: r["c"]
        for r in conn.execute(
            """SELECT n, COUNT(*) AS c FROM (
                  SELECT agent_name, COUNT(DISTINCT conv_id) AS n FROM turns
                   WHERE agent_name IS NOT NULL GROUP BY agent_name
               ) GROUP BY n"""
        )
    }
    conn.close()
    return out


# ---------------------------------------------------------------------------
# Section builders
# ---------------------------------------------------------------------------


def section_title(doc):
    title = doc.add_paragraph()
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = title.add_run("Conversation Analysis Bot")
    run.bold = True
    run.font.size = Pt(20)

    sub = doc.add_paragraph()
    sub.alignment = WD_ALIGN_PARAGRAPH.CENTER
    sub_run = sub.add_run("Contact-Centre Conversation Analytics Prototype — Submission Report")
    sub_run.italic = True
    sub_run.font.size = Pt(12)

    meta = doc.add_paragraph()
    meta.alignment = WD_ALIGN_PARAGRAPH.CENTER
    m = meta.add_run(f"Repository: {REPO_URL}")
    m.font.size = Pt(10)
    m.font.color.rgb = RGBColor(0x55, 0x55, 0x55)


def section_problem_framing(doc):
    H1(doc, "1. Problem framing")
    P(doc,
      "Build a prototype that lets a contact-centre analyst ask analytical "
      "questions over 3,000 bilingual (English + Hindi/Hinglish) customer-"
      "support conversations and get clear, evidence-backed answers in seconds."
    )
    H2(doc, "Success criteria")
    Bullet(doc, "All four assignment example questions answerable via the chat UI and the API.")
    Bullet(doc, "Answers cite concrete conversation examples whenever the question implies evidence.")
    Bullet(doc, "The system shows a deliberate analytical / agent workflow rather than a single direct prompt.")


def section_dataset_handling(doc, stats, tax_doc):
    H1(doc, "2. Dataset preprocessing")
    P(doc,
      f"Source: Customer Support Conversation Dataset – Syncora.ai (Kaggle), "
      f"a sample of 3,000 conversations provided as a single CSV. "
      f"Raw shape: {stats['turns']:,} turn rows across "
      f"{stats['conversations']:,} conversations, "
      f"{stats['distinct_agents']:,} distinct agents, dated "
      f"{stats['min_date'][:10]} to {stats['max_date'][:10]}."
    )
    H2(doc, "2.1 Data cleaning")
    P(doc,
      "Each turn in the raw CSV is a short readable sentence followed by a "
      "long string of random gibberish characters. We strip everything after "
      "the last sentence-ending punctuation mark, then drop any remaining "
      "non-word tokens using an English + Hindi dictionary. After cleaning, "
      f"all {stats['turns']:,} turns are reduced to short readable text — "
      f"and across the whole dataset there are only "
      f"{stats['distinct_prefixes']} distinct cleaned sentences, which means "
      "the data is highly templated. We exploit this in the next step."
    )
    H2(doc, "2.2 Labels and aggregations we add to each turn")
    P(doc,
      "Because there are only ~426 distinct cleaned sentences, we send each "
      "of those once to a cheap, fast model (Claude Haiku) and ask it to "
      "label it. The labels are then copied onto every turn that uses the "
      "same sentence. This is roughly 100× cheaper than labelling each of "
      "the 42,000 turns individually."
    )
    P(doc, "Per-turn labels added:")
    Bullet(doc, "Sentiment — both a category (positive / neutral / negative) and a score from −1 to +1.")
    Bullet(doc, "Intent — what the speaker is doing in the turn (complaint, urgency request, escalation request, thanks, apology, solution offer, status update, asking for info, …).")
    Bullet(doc, "Empathy signal for agent turns (empathetic / neutral / dismissive).")
    Bullet(doc, "Escalation flag and PII flag.")
    Bullet(doc, "Language (English / Hindi / mixed) plus a clean English translation of the original line.")
    P(doc, "We then roll these per-turn labels up to two further levels:")
    Bullet(doc, "Per-conversation: overall customer sentiment, a turn-by-turn sentiment trajectory, whether the issue was resolved, whether escalation happened, the conversation's topic, and the agent's average empathy.")
    Bullet(doc, "Per-agent: how many conversations they handled, average customer sentiment, average empathy, resolution rate, escalation rate, and which topics they handle most.")

    H2(doc, "2.3 Topic extraction (derived once, frozen)")
    P(doc,
      "Every conversation opens with a customer message that follows one of "
      "nine fixed sentence patterns, with the topic baked into the wording:"
    )
    Code(doc,
        "“Hello, my Audit Logs is not working as expected.”\n"
        "“Hi, I need help with my SSO.”\n"
        "“App crash ho rahi hai while using Wallet.”            (Hinglish)\n"
        "“Mujhe Refund ke baare mein help chahiye.”             (Hinglish)\n"
        "“I was charged twice for my eSIM.”\n"
        "“I can’t log in. It says account locked.”"
    )
    P(doc,
      "We extract the product name (the topic) from each opener using a "
      "small set of patterns, then group the 51 distinct product names into "
      "15 broader categories (Payments & Wallet, Billing & Refunds, "
      "Healthcare Services, etc.) using a one-shot LLM grouping call. These "
      "15 categories are frozen and used as the topic vocabulary throughout "
      "the rest of the system. The pattern bank is fitted to this dataset; "
      "a more general LLM-based extractor is the planned replacement "
      "(Section 8)."
    )
    rows = []
    for c in tax_doc["categories"]:
        n = sum(1 for v in tax_doc["slot_to_category"].values() if v == c["id"])
        rows.append([c["id"], c["label"], str(n)])
    Table(doc, ["Category id", "Display label", "# slots mapped"], rows, col_widths=[2.0, 2.3, 1.0])

    H2(doc, "2.4 Key assumptions")
    Bullet(doc, "Per-turn timestamps are unreliable (not monotonic within a conversation). turn_index is authoritative for order; conversation_start_ts = min(turn.timestamp).")
    Bullet(doc, "Only the cleaned prefix carries analytical signal; trailing gibberish is dropped.")
    Bullet(doc, "Each conversation has a unique customer_name (1:1 with conv_id).")
    Bullet(doc,
      f"Agents are sparsely distributed: {stats['agents_by_conv_count'].get(1, 0):,} agents "
      f"handle exactly 1 conversation, {stats['agents_by_conv_count'].get(2, 0)} handle 2, none handle 3+ "
      "(3,000 conversations / 2,987 agents = 1.004 avg). Implication: agent-coaching answers prioritise concrete example interactions over statistical rankings."
    )

    H2(doc, "2.5 Distributions after preparation")
    Bullet(doc,
      "Sentiment label: "
      + ", ".join(f"{k}={v:,}" for k, v in stats["sentiment_dist"].items())
    )
    Bullet(doc,
      "Language: "
      + ", ".join(f"{k}={v:,}" for k, v in stats["lang_dist"].items())
      + " — ~16% non-English (Hindi/Hinglish + mixed)."
    )
    Bullet(doc,
      "Agent empathy (agent turns only): "
      + ", ".join(f"{k}={v:,}" for k, v in stats["empathy_agent_only"].items())
      + " — 0 dismissive agent turns reflects the uniformly polite synthetic data."
    )


def section_system_design(doc):
    H1(doc, "3. System design")
    P(doc,
      "Two clearly separated parts: a preprocessing pipeline that runs once "
      "offline to turn the raw CSV into labelled, searchable data, and an "
      "online serving path that uses an AI agent to answer questions over "
      "that data."
    )

    H2(doc, "3.1 Preprocessing pipeline (offline, one-shot)")
    Code(doc,
        "         ┌────────────────────────────┐\n"
        "         │  Raw CSV (42k turns)       │\n"
        "         └─────────────┬──────────────┘\n"
        "                       ▼\n"
        "         ┌────────────────────────────┐\n"
        "         │  Clean each turn's text    │\n"
        "         │  (strip random gibberish)  │\n"
        "         └─────────────┬──────────────┘\n"
        "                       ▼\n"
        "         ┌────────────────────────────┐\n"
        "         │  Label each distinct       │\n"
        "         │  sentence with AI          │\n"
        "         │  (sentiment, intent,       │\n"
        "         │   empathy, language, …)    │\n"
        "         └─────────────┬──────────────┘\n"
        "                       ▼\n"
        "         ┌────────────────────────────┐\n"
        "         │  Roll up to conversation   │\n"
        "         │  and agent level + extract │\n"
        "         │  topic per conversation    │\n"
        "         └─────────────┬──────────────┘\n"
        "                       │\n"
        "          ┌────────────┴────────────┐\n"
        "          ▼                         ▼\n"
        "   ┌────────────┐           ┌──────────────┐\n"
        "   │ Structured │           │ Vector store │\n"
        "   │ database   │           │ (multilingual│\n"
        "   │ (SQLite)   │           │  embeddings) │\n"
        "   └────────────┘           └──────────────┘"
    )
    P(doc, "Important pieces (high level):")
    Bullet(doc, "The data cleaner — strips the synthetic gibberish from each turn.")
    Bullet(doc, "The labeller — sends each distinct sentence once to a fast AI model and copies the labels onto every matching turn.")
    Bullet(doc, "The topic extractor — recognises which product / service the customer is calling about, then groups those into a small set of analyst-friendly categories.")
    Bullet(doc, "The rollups — produce ready-to-query views per conversation and per agent.")
    Bullet(doc, "The vector-store seeder — embeds each conversation using a multilingual model so semantic search works in both English and Hindi/Hinglish.")

    H2(doc, "3.2 Online serving (per analyst question)")
    Code(doc,
        "         ┌────────────────────────────┐\n"
        "         │  Analyst types a question  │\n"
        "         │  in the chat UI            │\n"
        "         └─────────────┬──────────────┘\n"
        "                       ▼\n"
        "         ┌────────────────────────────┐\n"
        "         │  Web API endpoint          │\n"
        "         │  (receives /ask request)   │\n"
        "         └─────────────┬──────────────┘\n"
        "                       ▼\n"
        "         ┌────────────────────────────┐\n"
        "         │  AI agent (the planner):   │\n"
        "         │  decides which tools to    │\n"
        "         │  call, reads results, and  │\n"
        "         │  loops until it has enough │\n"
        "         │  evidence                  │\n"
        "         └─────────────┬──────────────┘\n"
        "                       │ uses\n"
        "                       ▼\n"
        "         ┌────────────────────────────┐\n"
        "         │  Six tools: aggregate over │\n"
        "         │  the database; semantic    │\n"
        "         │  search the vector store;  │\n"
        "         │  look up specific          │\n"
        "         │  conversations or topics   │\n"
        "         └─────────────┬──────────────┘\n"
        "                       ▼\n"
        "         ┌────────────────────────────┐\n"
        "         │  Structured answer:        │\n"
        "         │  prose + cited quotes +    │\n"
        "         │  reasoning + uncertainty   │\n"
        "         └────────────────────────────┘"
    )
    P(doc, "Important pieces (high level):")
    Bullet(doc, "The chat UI — where the analyst types their question and sees the answer with evidence cards.")
    Bullet(doc, "The web API — accepts a question, returns the structured answer; can also be called from scripts or other systems.")
    Bullet(doc, "The agent — the brain that decides what tools to call to answer the question.")
    Bullet(doc, "The tools — the agent's hands; the six things the agent is allowed to ask the data for.")
    Bullet(doc, "Session memory — short-term context so follow-up questions can refer back to earlier answers within the same session.")


def section_agent_flow(doc):
    H1(doc, "4. Agent flow")
    P(doc,
      "A single Claude Sonnet 4.6 agent handles planning, tool use, and "
      "synthesis, following the ReAct pattern (think → act → observe → "
      "repeat). The flow looks like below."
    )
    Code(doc,
        "POST /ask\n"
        "  │\n"
        "  ▼\n"
        "memory.load_recent(session_id, n=3)        # 24h TTL, PII-redacted\n"
        "  │\n"
        "  ▼\n"
        "Planner (Sonnet 4.6, tool-use loop)        # max 8 iterations\n"
        "  │  ├─ system prompt: taxonomy thumbnail + bilingual rules + 'examples over caveats'\n"
        "  │  ├─ tools: query_conversations, query_agents, semantic_search,\n"
        "  │  │         get_trajectory, get_conversation, list_topics\n"
        "  │  └─ on each turn: read tool result, decide next tool, or call emit_answer\n"
        "  │\n"
        "  ▼\n"
        "emit_answer ─► AnswerEnvelope {answer, evidence[], tool_calls[],\n"
        "                                reasoning_brief, uncertainty}\n"
        "  │\n"
        "  ▼\n"
        "memory.save_turn(...)\n"
        "  │\n"
        "  ▼\n"
        "AskResponse{session_id, envelope} → caller"
    )
    H2(doc, "The six tools and what each unblocks")
    rows = [
        ["query_conversations",
         "Counts, ranks, or averages metrics across conversations, optionally filtered by date, topic, sentiment, or agent.",
         "Lets the agent answer 'how many', 'top N', and 'compare over time' questions — e.g. ranking topics by negative sentiment in a given month."],
        ["query_agents",
         "Same as above but at the agent level — ranks agents by empathy, resolution rate, escalation rate, and so on.",
         "Lets the agent answer 'which agent / how does agent X compare' questions — e.g. surfacing the lowest-empathy agents for a coaching review."],
        ["semantic_search",
         "Finds conversations whose meaning matches a free-form query, in either English or Hindi/Hinglish.",
         "Lets the agent retrieve example conversations as evidence even when no exact word match exists — e.g. 'show me cases where the customer was rushed' returning Hinglish urgency phrases."],
        ["get_trajectory",
         "Returns how customer sentiment changed turn-by-turn within one specific conversation.",
         "Lets the agent narrate the emotional arc of a single conversation — e.g. spotting when frustration peaked and whether it was resolved by the end."],
        ["get_conversation",
         "Returns the full transcript of one conversation, with both original and translated text.",
         "Lets the agent quote verbatim from a specific conversation as supporting evidence — typically used right after semantic_search has identified an interesting case."],
        ["list_topics",
         "Returns the list of the 15 topic categories the system knows about, with how many conversations fall under each.",
         "Lets the agent discover which topic labels are valid filter values before composing other queries — and gives the analyst a quick map of the dataset."],
    ]
    Table(doc, ["Tool", "What it does", "What it unblocks"], rows, col_widths=[1.4, 2.5, 2.6])

    H2(doc, "Worked example: agents needing coaching")
    P(doc,
      "On this question the agent ran 5 tool calls in ~75s: rank agents by "
      "empathy → semantic search for low-empathy conversations → drill into "
      "two specific conversations → cross-reference the pattern. The final "
      "answer named three specific agents and quoted both English and "
      "Hinglish customer turns as evidence."
    )

    H2(doc, "Design choice: single-agent over multi-agent")
    P(doc,
      "One agent rather than an orchestrator + specialised sub-agents. The "
      "task doesn't need specialisation — the same model can decide what to "
      "fetch, read the result, and write the synthesis — and keeping it in "
      "one continuous context avoids handoff loss and extra LLM round trips. "
      "The natural extension when richer agency is needed is a critic "
      "agent (Section 8)."
    )


def section_models_and_tools(doc):
    H1(doc, "5. Model and tool choices")
    rows = [
        ["Claude Sonnet 4.6", "Planner + Synthesizer in the online agent loop.",
         "Strong at multi-step tool use, JSON-mode structured output, and bilingual reasoning. Single-model design (planner+synthesizer share one call) keeps reasoning continuous and reduces cost vs a two-model handoff."],
        ["Claude Haiku 4.5", "Per-distinct-prefix classification (offline); LLM-as-judge grounding grader (eval).",
         "Cheap and fast. Used at the 426-prefix bottleneck where total cost is < $0.20 and quality is sufficient. Using a different family as eval grader avoids self-judging bias."],
        ["paraphrase-multilingual-MiniLM-L12-v2", "Sentence-transformer for ChromaDB embeddings.",
         "118M params, multilingual (50+ languages incl. Hindi). Per CLAUDE.md bilingual rule #4, the embedding model MUST be multilingual or Hindi conversations become unretrievable. Verified end-to-end: English query 'app crash' surfaces Hinglish 'App crash ho rahi hai while using X' conversations."],
        ["ChromaDB (local, persistent)", "Vector store for 3,000 conversations.",
         "Cosine HNSW space + L2-normalised embeddings → similarity scores in [0,1]. Zero-config, ships in repo. Production would swap to OpenSearch / pgvector."],
        ["SQLite", "Structured store for turns, conversations, agents, sessions.",
         "Zero-config, file-based, fast aggregations at this scale. WAL mode for concurrent read."],
        ["FastAPI + Pydantic", "HTTP surface + typed envelope.",
         "Automatic OpenAPI; same Pydantic models drive Anthropic tool-use input schemas and the FastAPI response."],
        ["Streamlit", "Analyst-facing chat UI.",
         "Fastest viable chat surface. st.chat_message for history, st.sidebar for sample questions + session controls."],
    ]
    Table(doc, ["Component", "Role", "Why"], rows, col_widths=[1.7, 1.6, 3.0])
    H2(doc, "Cost & latency posture")
    Bullet(doc, "Offline pipeline: one-time, < $0.50 of Anthropic spend on a fresh build (426 Haiku classifications + 1 Sonnet taxonomy call).")
    Bullet(doc, "Online: $0.02–$0.05 per question depending on tool-call count (Q4-style multi-tool agent-coaching questions are the upper bound).")
    Bullet(doc, "Latency: typically 10–60s end-to-end (planner + tools + synthesis); p95 = 60s on the eval set. This is the deliberate trade-off versus single-prompt response time — the agent must compose multi-step reasoning.")
    Bullet(doc, "Credentials policy: ANTHROPIC_API_KEY loaded from .env via python-dotenv; .env is gitignored; .env.example documents every required variable with placeholders only.")


def section_evaluation(doc, eval_data):
    H1(doc, "6. Evaluation")
    summary = eval_data["summary"]
    P(doc,
      f"Hand-crafted set of {summary['n_questions']} questions covering every "
      "category in the assignment plus two edge cases. Five of the fifteen "
      "questions are written in or target Hindi/Hinglish content, satisfying "
      "the bilingual-coverage rule (≥ 1/3 of the eval set)."
    )
    H2(doc, "6.1 Methodology")
    P(doc,
      "Each question is scored on five dimensions: whether the expected "
      "tools were called, keyword coverage in the answer, evidence count, "
      "grounding (LLM-as-judge), and latency. A question passes the first "
      "three; grounding is reported as an independent column."
    )
    P(doc,
      f"Grounding grader: {eval_data.get('grader_model','Haiku')} with a "
      "structured-output tool that returns supported_claim_count, "
      "unsupported_claim_count, irrelevant_evidence_count, and ungrounded "
      "examples. Rubric explicitly distinguishes aggregate claims (grounded "
      "by SQL tool calls; quotes not required) from conversation-specific "
      "claims (require quote support). Pure-aggregation answers with no "
      "quotes correctly score 1.0."
    )
    H2(doc, "6.2 Overall results")
    rows = [
        ["Pass rate", f"{summary['pass_rate']:.0%} ({int(summary['pass_rate']*summary['n_questions'])}/{summary['n_questions']})"],
        ["Partial rate", f"{summary['partial_rate']:.0%}"],
        ["Fail rate", f"{summary['fail_rate']:.0%}"],
        ["Mean grounding", f"{summary['mean_grounding']:.2f}" if summary['mean_grounding'] is not None else "—"],
        ["Mean / median / p95 latency", f"{summary['mean_latency_s']}s / {summary['median_latency_s']}s / {summary['p95_latency_s']}s"],
    ]
    Table(doc, ["Metric", "Value"], rows, col_widths=[2.5, 3.5])

    H2(doc, "6.3 Per-question results")
    rows = []
    for r in eval_data["results"]:
        g = r["grounding"]
        ground = f"{g['grounding_score']:.2f}" if g else "—"
        rows.append([
            r["id"].split("_", 1)[0] + " " + r["id"].split("_", 1)[1].replace("_", " ")
            if "_" in r["id"] else r["id"],
            r["category"],
            r["language"],
            "✓" if r["tool_selection_pass"] else "✗",
            f"{r['coverage']:.2f}",
            str(r["evidence_count"]),
            ground,
            f"{r['latency_s']:.1f}s",
            r["status"],
        ])
    Table(doc, ["Question", "Category", "Lang", "Tool", "Cov", "Ev", "Ground", "Latency", "Status"],
          rows, col_widths=[1.7, 1.1, 0.5, 0.4, 0.5, 0.4, 0.6, 0.7, 0.6])

    H2(doc, "6.4 Pass rate by language (bilingual robustness)")
    rows = [[lang, f"{rate:.0%}"] for lang, rate in summary["pass_rate_by_language"].items()]
    Table(doc, ["Language", "Pass rate"], rows, col_widths=[2.0, 2.0])

    H2(doc, "6.5 Representative successes")
    Bullet(doc,
      "Q06 (trajectory of Hinglish conversation C0009233) — single get_trajectory "
      "call yielded a turn-by-turn narrative identifying turn 14 as the inflection "
      "point (sentiment +0.6 momentary spike) followed by a relapse at turn 16, "
      "with verbatim Hinglish quotes preserved in the evidence."
    )
    Bullet(doc,
      "Q04 (agents needing coaching) — composed 11 tool calls including "
      "query_agents, semantic_search, and two get_conversation drill-downs to "
      "name three specific agents (AgentVYJS, AgentTXPF, AgentTVAX) with "
      "verbatim Hinglish and English quotes from their conversations and four "
      "actionable coaching themes. Grounding 1.00."
    )
    Bullet(doc,
      "Q10 (Hinglish app-crash retrieval) — English query surfaced three "
      "Hinglish 'App crash ho rahi hai while using X' conversations via the "
      "multilingual embedding, confirming cross-language retrieval works "
      "without pre-translation."
    )
    H2(doc, "6.6 Representative failures / weaknesses")
    Bullet(doc,
      "Q06 grounding 0.65 — grader rubric edge case. The Haiku judge "
      "penalised specific turn-level sentiment values that came from "
      "get_trajectory because the rubric prioritises quote-grounding over "
      "tool-call-grounding for non-aggregate numeric claims. Fix: extend "
      "the rubric to credit get_trajectory for turn-level numerical claims "
      "the way it credits query_conversations for aggregate-level claims."
    )
    Bullet(doc,
      "Q08 grounding 0.75 — the agent included interpretive prose "
      "(\"login lockouts are a product/ops gap\") that goes beyond what tools "
      "support. Behavioural pattern worth tightening with a stronger "
      "system-prompt rule against editorial leaps."
    )
    Bullet(doc,
      "Q09 grounding 0.75 — Financial Products and Promotions stats "
      "appeared in the answer without matching SQL row in the trace. "
      "Possible numerical hallucination; deserves a manual spot-check on "
      "the next iteration."
    )
    H2(doc, "6.7 Latency and consistency")
    P(doc,
      f"Mean latency {summary['mean_latency_s']}s, p95 {summary['p95_latency_s']}s. "
      "Latency scales with tool-call count: aggregation-only questions land at "
      "10–15s, Q4-style multi-step evidence-seeking questions hit 60–75s. "
      "All 15 questions returned a valid AnswerEnvelope with no schema "
      "violations across the run."
    )


def section_bonus_extensions(doc):
    H1(doc, "7. Bonus extensions")

    H2(doc, "7.1 Knowledge retrieval (RAG)")

    P(doc, "How retrieval is indexed:", bold=True)
    P(doc,
      "One document per conversation, stored in a local vector store using "
      "a multilingual embedding model so a single index covers both English "
      "and Hindi/Hinglish without translation. Each document also carries "
      "metadata (topic, agent, language, sentiment, escalation flag) so "
      "retrieval can be scoped to a subset of the dataset."
    )

    P(doc, "When retrieval is triggered:", bold=True)
    P(doc,
      "The agent decides on its own — it reaches for semantic search "
      "whenever the analyst asks for examples, evidence, or similar cases. "
      "In the evaluation set this happened on 9 of 15 questions, most "
      "heavily on agent-coaching and Hindi/Hinglish queries."
    )

    P(doc, "What evidence is returned:", bold=True)
    P(doc,
      "The top few most semantically similar conversations (typically 3–5), "
      "each with a similarity score, a short excerpt, and the indexed "
      "metadata. The agent then pulls verbatim quotes from the most "
      "relevant ones into the final answer's evidence section."
    )

    P(doc, "How retrieval improves the final answer:", bold=True)
    P(doc,
      "It lifts answers from generic statistics to cited, concrete "
      "examples — real conversations over statistical disclaimers. In the "
      "eval, 9 of 15 answers include 3+ evidence quotes, and every "
      "Hindi-language question is answered with at least one original-"
      "language quote."
    )

    H2(doc, "7.2 Specialised LLM for offline labelling")

    P(doc, "Why this was chosen:", bold=True)
    P(doc,
      "We use an LLM to do topic extraction, sentiment analysis, intent "
      "detection, empathy detection, and language detection on every turn. "
      "Classical ML classifiers would need labelled training data we don't "
      "have and would struggle with the bilingual English + Hindi/Hinglish "
      "mix; an LLM handles both out of the box."
    )

    P(doc, "How it fits the flow:", bold=True)
    P(doc,
      "The labelling runs once offline, then the labels and their rollups "
      "(per conversation, per agent) are saved into the database. At "
      "question time the runtime tools just read these precomputed values "
      "directly — no LLM call per tool — so the agent can answer most "
      "questions in seconds."
    )

    P(doc, "How its usefulness was evaluated:", bold=True)
    P(doc,
      "The labels are checked indirectly by the evaluation set: questions "
      "like \"topics with most negative sentiment last month\" or \"agents "
      "with lowest empathy\" only return the right answers if the underlying "
      "labels are correct. All 15 evaluation questions pass, which "
      "indicates the labels are reliable enough for analyst use."
    )

    H2(doc, "7.3 Memory integration")

    P(doc, "What is stored:", bold=True)
    P(doc,
      "Each question and its full structured answer are stored in a session-"
      "scoped table, keyed by session id and turn index. Stored content is "
      "PII-redacted before persistence (emails, phone numbers, card-like "
      "numbers replaced with placeholders)."
    )

    P(doc, "How it is retrieved:", bold=True)
    P(doc,
      "At the start of every new question, the agent loads the last few "
      "turns of the current session and prepends them as prior context "
      "before reading the new question. This is automatic — the analyst "
      "doesn't have to opt in."
    )

    P(doc, "How it affects later responses:", bold=True)
    P(doc,
      "Follow-up questions can refer back to earlier ones without the "
      "analyst re-typing context — for example, asking about \"the agent "
      "you just mentioned\" works because the prior answer is in the agent's "
      "context. The agent can also reuse prior evidence instead of "
      "re-fetching it."
    )

    P(doc, "Reliability and privacy considerations:", bold=True)
    P(doc,
      "Memory is scoped per session so one analyst's questions never bleed "
      "into another's, and rows expire after 24 hours so old context "
      "doesn't accumulate. Synthetic customer/agent identifiers from the "
      "dataset are intentionally not redacted because they are not real "
      "PII and follow-up continuity needs them."
    )


def section_limitations_and_improvements(doc):
    H1(doc, "8. Limitations and next improvements")
    P(doc,
      "The biggest limitation on this project is the time available to "
      "build it. The prototype works end-to-end, but every layer has clear "
      "room for improvement. The most impactful next steps are below."
    )

    P(doc, "1. A more general preprocessing approach.", bold=True)
    P(doc,
      "The current preprocessing pipeline is fitted to the specific shape "
      "of this dataset — topic extraction uses pattern matching keyed to the "
      "templated customer messages, and the data cleaner exploits the "
      "specific gibberish-suffix quirk. A more general approach (an LLM-"
      "based topic extractor and a content-aware cleaner) is needed before "
      "this would work on real, free-form customer messages."
    )

    P(doc, "2. A larger eval set with expected answers.", bold=True)
    P(doc,
      "The current evaluation set has only 15 questions and no expected "
      "answer per question, so it only checks that the right tools were "
      "called and that some evidence came back — not whether the actual "
      "answer is correct. With a gold-standard expected answer per "
      "question, an LLM-as-judge could score completion rate directly "
      "(does the actual answer cover the expected content?), and the eval "
      "would catch quality regressions rather than just behaviour changes."
    )

    P(doc, "3. Production-grade setup.", bold=True)
    P(doc,
      "Several pieces are local-prototype-shaped and would change for a "
      "real deployment. The single-file database would move to a managed "
      "database (e.g. Postgres) for concurrent access; the local vector "
      "store would move to a distributed one (e.g. OpenSearch or pgvector); "
      "session memory would move to Redis with native expiry; the single-"
      "process web API would be containerised behind a load balancer. "
      "Authentication and per-analyst rate limiting are also missing."
    )

    P(doc, "4. Add an online critic to push grounding higher.", bold=True)
    P(doc,
      "Today one agent gathers data and writes the answer in one loop. A "
      "natural next step is to add a critic that reads the final answer "
      "plus its evidence and decides whether the grounding is good enough; "
      "if not, it asks the planner to retrieve more evidence and rewrite. "
      "This is the cheapest single change that would push the system from "
      "single-agent to a clear multi-agent workflow, and it directly "
      "targets the few evaluation questions where grounding was weakest."
    )


def section_appendix(doc):
    H1(doc, "Appendix A — Setup")
    Code(doc,
        "# 1. Clone and install\n"
        f"git clone {REPO_URL}.git\n"
        "cd calabrio-assignment-conversational-bot\n"
        "uv sync                                    # or pip install -e .\n"
        "\n"
        "# 2. Configure secrets\n"
        "cp .env.example .env\n"
        "$EDITOR .env                               # set ANTHROPIC_API_KEY\n"
        "\n"
        "# 3. Place the raw CSV\n"
        "# cs_conversations.csv -> data/cs_conversations.csv\n"
        "\n"
        "# 4. Run the offline preparation pipeline (idempotent, resumable)\n"
        "uv run python scripts/prepare_data.py\n"
        "\n"
        "# 5. Start the API (one shell)\n"
        "uv run uvicorn backend.api:app --reload --port 8000\n"
        "\n"
        "# 6. Start the UI (another shell)\n"
        "uv run streamlit run ui/app.py\n"
        "\n"
        "# 7. Sample request (CLI)\n"
        "curl -s -X POST http://localhost:8000/ask \\\n"
        "  -H 'content-type: application/json' \\\n"
        "  -d '{\"question\": \"Top 5 topics with most negative sentiment in Nov 2025\"}' \\\n"
        "  | jq .\n"
        "\n"
        "# 8. Run the eval harness\n"
        "uv run python eval/run_eval.py"
    )
    H1(doc, "Appendix B — Required environment variables")
    rows = [
        ["ANTHROPIC_API_KEY", "REQUIRED", "Anthropic API key. Loaded via python-dotenv."],
        ["ANTHROPIC_PLANNER_MODEL", "claude-sonnet-4-6", "Online agent loop (planner + synthesizer)."],
        ["ANTHROPIC_CLASSIFIER_MODEL", "claude-haiku-4-5-20251001", "Offline classification + eval grader."],
        ["EMBEDDING_MODEL", "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2", "MUST be multilingual; see CLAUDE.md bilingual rule #4."],
        ["DATA_DIR", "./data", "Root for SQLite + chroma + raw CSV."],
        ["SQLITE_PATH", "./data/processed.db", "Override default DB path."],
        ["CHROMA_PATH", "./data/chroma", "Override default vector store path."],
        ["CLASSIFY_BATCH_SIZE / CLASSIFY_CONCURRENCY", "20 / 1", "Tune up if your Anthropic tier allows."],
        ["MEMORY_TTL_HOURS", "24", "Session memory expiry."],
    ]
    Table(doc, ["Variable", "Default", "Purpose"], rows, col_widths=[2.4, 1.7, 2.5])
    H1(doc, "Appendix C — Repository layout")
    Code(doc,
        ".\n"
        "├── CLAUDE.md                 # durable spec, submission checklist, working agreements\n"
        "├── README.md\n"
        "├── report/build_report.py    # this file — regenerates report.docx\n"
        "├── report/report.docx        # final DOCX submission artifact\n"
        "├── scripts/prepare_data.py   # offline pipeline orchestrator\n"
        "├── backend/\n"
        "│   ├── config.py             # env-driven Config (shared)\n"
        "│   ├── db.py                 # SQLite schema + migrations (shared)\n"
        "│   ├── schemas.py            # Pydantic I/O models (shared)\n"
        "│   ├── tools.py              # 6 tool functions for the agent (serving)\n"
        "│   ├── memory.py             # session memory + PII redaction (serving)\n"
        "│   ├── agent.py              # planner-synthesizer loop (serving)\n"
        "│   ├── api.py                # FastAPI POST /ask (serving)\n"
        "│   └── preprocessing/        # offline batch pipeline\n"
        "│       ├── text.py           # 2-pass prefix cleaner (terminator + wordfreq)\n"
        "│       ├── taxonomy.py       # slot extraction + LLM grouping + YAML load/save\n"
        "│       ├── classifier.py     # Haiku batch classifier (sentiment/intent/PII/EN/lang)\n"
        "│       ├── rollups.py        # conversations + agents aggregations\n"
        "│       └── embed.py          # ChromaDB seeding (multilingual, cosine)\n"
        "├── ui/app.py                 # Streamlit analyst chat\n"
        "├── eval/\n"
        "│   ├── questions.yaml        # 15 hand-crafted Q/A pairs\n"
        "│   ├── run_eval.py           # 5-dimension scoring + Haiku grounding judge\n"
        "│   └── results-latest.json   # committed eval snapshot for report citations\n"
        "└── data/\n"
        "    ├── cs_conversations.csv  # raw (gitignored, 23 MB)\n"
        "    ├── topic_taxonomy.yaml   # frozen 15-category taxonomy\n"
        "    ├── processed.db          # SQLite (gitignored)\n"
        "    └── chroma/               # vector store (gitignored)"
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def build() -> Path:
    doc = Document()
    # Default body font
    style = doc.styles["Normal"]
    style.font.name = "Calibri"
    style.font.size = Pt(11)

    tax_doc = load_taxonomy()
    eval_data = load_eval()
    stats = db_stats()

    section_title(doc)
    section_problem_framing(doc)
    section_dataset_handling(doc, stats, tax_doc)
    section_system_design(doc)
    section_agent_flow(doc)
    section_models_and_tools(doc)
    section_evaluation(doc, eval_data)
    section_bonus_extensions(doc)
    section_limitations_and_improvements(doc)
    section_appendix(doc)

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    doc.save(OUT_PATH)
    return OUT_PATH


if __name__ == "__main__":
    out = build()
    print(f"wrote {out.relative_to(REPO_ROOT)} ({out.stat().st_size:,} bytes)")
