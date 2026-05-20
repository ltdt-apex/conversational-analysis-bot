# CLAUDE.md — Conversation Analysis Bot (take-home)

This file is the durable spec for the project. Re-read it before every non-trivial change so submission obligations are not forgotten.

## Hard submission requirements (from the assignment doc)

The submission must include **both**:

1. **A short report (DOCX or PDF)** that covers:
   - Problem framing
   - Dataset handling (preparation, assumptions, derived fields, labels, aggregations)
   - System design
   - Agent flow
   - Model and tool choices
   - Evaluation (representative successes and failures, task-specific metrics where relevant, grounding quality, latency/consistency observations)
   - Limitations
   - Next improvements
   - If any new data was appended, cite the source

2. **Implementation repository** (GitHub link or ZIP) containing:
   - Source code
   - Clear setup/run instructions (README)
   - Sample requests and outputs
   - Instructions for obtaining or preparing the dataset
   - Environment-variable requirements and external-service notes
   - **Credentials must not be hardcoded** — load from `.env` / env vars only

## Prototype-quality bar (from the doc)

- Analyst-facing entry point (chat UI, web app, API with examples, or documented CLI).
- The system must show a **deliberate analytical / agent workflow**, not a single direct prompt.
- Answers should be evidence-based: structured outputs, counts, rankings, retrieved examples, brief reasoning, uncertainty notes where helpful.
- Must be able to answer arbitrary analytical questions over the dataset, not just the four examples.

## Example questions the system must be able to answer

1. Top 5 topics most associated with negative customer sentiment in the **last month represented in the dataset** (Nov 2025).
2. Which support agents may benefit from coaching on empathy / customer communication, with evidence.
3. How customer sentiment changed over the course of a selected conversation.
4. Which recurring customer concerns leadership should review first.

## Optional bonus extensions (each must be explained in the report)

- **Knowledge retrieval (RAG):** how it is indexed, when triggered, what evidence is returned, how it improves the answer.
- **Specialized ML / LLM agent:** why chosen, how it fits the flow, how usefulness is evaluated.
- **Memory integration:** what is stored, how retrieved, how it affects later responses, reliability/privacy considerations.

## Data notes (do not re-derive — pinned here)

- File: `data/cs_conversations.csv` — 41,965 rows / 3,000 conversations / ~14 turns avg / 2,987 distinct agents.
- Columns: `conv_id, turn_index, timestamp, role, text, customer_name, agent_name`.
- Date range: **2025-08-01 → 2025-12-02**. "Last month in dataset" = **November 2025**.
- Each turn's `text` = a short coherent prefix + long random gibberish suffix. **Only the prefix carries signal.**
- The prefix is recoverable by truncating at the **last `.`/`!`/`?`** — gibberish never contains a sentence terminator (verified on a random 25-sample probe and a full-data run yielded 0 empty prefixes).
- **Only ~426 distinct cleaned prefixes exist across all 41,965 turns** — the dataset is highly templated. Per-turn classification should classify the 426 distinct prefixes once and join back, not run 41,965 separate LLM calls.
- Per-turn timestamps are **not monotonic** within a conversation. `turn_index` is authoritative for order. Conversation-level timestamp = `min(turn.timestamp)`.
- Each conversation has a unique `customer_name`. Agents are reused (top agent: 35 conversations).
- Customer-opening prefixes embed the topic as a slot, e.g. *"Hello, my **Audit Logs** is not working as expected."*, *"App crash ho rahi hai while using **Flight**."* — topic extraction is template-driven, not free-form NLU.
- Dataset is multilingual (English + Hindi/Hinglish templates). Classifier prompt must handle both.

## Architecture summary (the locked design)

```
CSV ─► prefix_clean ─► per-turn LLM classifier ─► rollups ─► SQLite (turns/conversations/agents)
                                                          └► ChromaDB (one doc / conversation)

Streamlit UI ─► FastAPI /ask ─► Planner agent (Claude Sonnet 4.6, tool-use loop)
                                  ├─ query_conversations (SQL)
                                  ├─ query_agents (SQL)
                                  ├─ semantic_search (Chroma)
                                  ├─ get_trajectory (SQL)
                                  ├─ get_conversation (SQL)
                                  └─ list_topics (SQL)
                                ─► Synthesizer (structured JSON: answer, evidence[], tool_calls[], uncertainty)
```

- **Models:** Claude Sonnet 4.6 for planning/synthesis, Claude Haiku 4.5 for batch turn classification.
- **Topic taxonomy is closed-set** (~20 labels) derived once in a clustering pre-pass and frozen.
- **Memory:** session-scoped SQLite table, 24h TTL, PII-redacted.

## Required directory layout (target)

```
/
├── CLAUDE.md                     (this file)
├── README.md                     (setup, run, sample requests/outputs, dataset instructions, env vars)
├── report/                       (DOCX/PDF deliverable + assets)
├── .env.example                  (lists required env vars, no real values)
├── pyproject.toml or requirements.txt
├── data/
│   ├── cs_conversations.csv      (input; gitignored if too large)
│   ├── processed.db              (SQLite, gitignored)
│   └── chroma/                   (vector store, gitignored)
├── scripts/
│   └── prepare_data.py           (offline preprocessing pipeline)
├── backend/
│   ├── api.py                    (FastAPI)
│   ├── agent.py                  (Planner + Synthesizer)
│   ├── tools.py                  (the 6 tool functions)
│   ├── memory.py                 (session memory)
│   └── schemas.py                (Pydantic models for the response envelope)
├── ui/
│   └── app.py                    (Streamlit chat)
└── eval/
    ├── questions.yaml            (~15 hand-crafted Q/A pairs)
    └── run_eval.py
```

## Pre-flight checklist before declaring "done"

- [ ] README has setup steps that work from a clean clone.
- [ ] `.env.example` lists every env var; no real keys in repo.
- [ ] Sample requests + sample outputs included (curl examples in README, or `examples/` directory).
- [ ] Dataset prep step is documented and reproducible (`python scripts/prepare_data.py`).
- [ ] All four example questions answerable end-to-end via the UI and the API.
- [ ] Report covers every section listed in "Hard submission requirements" above.
- [ ] Bonus extensions that were implemented are documented in the report with the required four points each (indexed/triggered/evidence/improvement for RAG; chosen/fit/evaluated for specialised agent; stored/retrieved/affects/privacy for memory).
- [ ] Eval results table in the report with successes, failures, latency, grounding notes.
- [ ] Limitations and next-improvements section written.

## Bilingual data handling (English + romanized Hindi/Hinglish)

**The dataset is bilingual.** Roughly half the conversations are in English, the
other half in romanized Hindi / Hinglish (e.g. *"Mujhe Refund ke baare mein
help chahiye."*, *"App crash ho rahi hai while using Wallet."*, *"Main abhi
verify kar rahi/raha hoon, kindly wait."*). Topic slots are always in English
even inside Hindi templates.

**Design rules — every new feature must honour these:**

1. **Preserve the original text.** `turns.text_clean` is the source of truth
   and stays in the original language. Never overwrite it with a translation.
2. **English translation is a parallel field.** `turns.text_clean_en` and
   `turns.language` are populated by the classifier (one of `en` / `hi` /
   `mixed`). Use `text_clean_en` only where a single-language view is genuinely
   needed (e.g. UI "Show English" toggle).
3. **Sentiment / empathy / intent classification must handle both languages
   natively.** Claude handles Hinglish fine — do not pre-translate before
   classifying. Translation flattens tone (politeness register, urgency
   markers) and would corrupt the sentiment signal.
4. **Embeddings must be multilingual.** Default model is
   `paraphrase-multilingual-MiniLM-L12-v2` (config: `EMBEDDING_MODEL`). Do not
   silently swap to an English-only embedding model — Hindi conversations
   would become unretrievable in semantic search.
5. **Topic extraction is language-invariant** because the topic is always an
   English slot in every template family. Treat the slot as language-agnostic.
6. **Eval sets must include Hindi/Hinglish examples** — at least 1/3 of the
   ~15 hand-crafted Q/A pairs should target Hindi conversations to catch
   regressions.

**If you hit a technical blocker** that makes bilingual handling unworkable
for a specific feature (e.g. an analytical model that only supports English,
or a domain-specific lexicon that has no Hindi equivalent), **stop and
discuss with the user before falling back to translation**. The user
explicitly wants to preserve language information; pre-translation is a
last-resort, not a default.

## Working agreements

- Do not hardcode API keys. Always read from env via `python-dotenv`.
- Keep the topic taxonomy frozen once chosen — re-running the classifier should be deterministic given the same input.
- Per-turn timestamps are unreliable; never use them for conversation-level time bucketing. Use `conversation_start_ts`.
- Before merging any change: re-read this file's "Pre-flight checklist" and verify nothing regressed.
- Every feature touching text must respect the "Bilingual data handling" rules above.

## Future improvements (note for the report's "Next improvements" section)

- **Generalise topic-slot extraction.** Current `backend/taxonomy.py` uses
  regex templates fitted to the Syncora.ai synthetic data (8 templates ×
  51 slot values, 100% coverage on the shipped CSV). On real, free-form
  customer openings this would not generalise. Plug-in points already exist
  behind `extract_slot()`; the candidate replacements are:
  - LLM-based slot identification (Claude reads each opener and emits the
    topic mention), or
  - Embedding-cluster-then-label (multilingual embeddings + HDBSCAN +
    LLM cluster labelling).
  Either approach drops in without touching downstream rollups, RAG, or
  the agent loop.
