# Conversation Analysis Bot

Prototype for the take-home assignment: an analyst-facing chat bot that answers
analytical questions over a 3,000-conversation customer-support dataset using a
deliberate planner-synthesizer agent workflow.

> **Submission deliverables** — see `CLAUDE.md` for the locked checklist.
> The report lives in `report/`; the source code is this repo.

---

## Quick start

```bash
# 1. Install (uv is recommended; pip works too)
uv sync                          # or: pip install -e .

# 2. Configure secrets
cp .env.example .env
$EDITOR .env                     # set ANTHROPIC_API_KEY

# 3. Obtain the dataset (see "Dataset" below) and place it at:
#    data/cs_conversations.csv

# 4. Run the offline preparation pipeline (one-shot, resumable)
uv run python scripts/prepare_data.py

# 5. Start the API
uv run uvicorn backend.api:app --reload --port 8000

# 6. In another terminal, start the analyst UI
uv run streamlit run ui/app.py
```

Open the Streamlit URL it prints and ask a question.

---

## Dataset

Primary source: **Customer Support Conversation Dataset — Syncora.ai** on Kaggle.
We use the 3,000-conversation sample shipped with the assignment as
`data/cs_conversations.csv` (41,965 turn rows, Aug–Dec 2025).

The raw CSV is not committed (`.gitignore`'d at 23 MB). Place it at
`data/cs_conversations.csv` before running the prep pipeline.

Schema: `conv_id, turn_index, timestamp, role, text, customer_name, agent_name`.

### Bilingual content

**The dataset is bilingual** — roughly half the conversations are in English
and half in romanized Hindi / Hinglish (e.g.
*"App crash ho rahi hai while using Wallet."*,
*"Mujhe Refund ke baare mein help chahiye."*). Topic slots are always in
English even inside Hindi templates (`Wallet`, `Refund`, `FASTag` …).

The preprocessing pipeline preserves the original text in `turns.text_clean`
and stores a parallel English translation in `turns.text_clean_en`, with a
`turns.language` label (`en` / `hi` / `mixed`). Classification, embeddings,
and the agent loop are all multilingual by design — translation is never
used as an intermediate step. See `CLAUDE.md` for the full bilingual-handling
contract.

See `report/` for the documented preparation steps, assumptions, and derived
fields.

---

## Architecture

```
CSV ─► prefix_clean ─► per-turn Haiku classifier ─► rollups ─► SQLite
                                                            └► ChromaDB

Streamlit ─► FastAPI /ask ─► Planner (Sonnet 4.6, tool-use) ─► Synthesizer
                              tools: query_conversations, query_agents,
                                     semantic_search, get_trajectory,
                                     get_conversation, list_topics
```

The Planner decides which tools to call for each analyst question; the
Synthesizer composes a structured JSON answer with cited evidence.

---

## Sample requests

See `examples/` for `curl` requests and recorded JSON outputs covering the four
example questions plus a few harder follow-ups.

```bash
curl -s -X POST http://localhost:8000/ask \
  -H 'content-type: application/json' \
  -d '{"question": "Top 5 topics with the most negative sentiment in November 2025"}' \
  | jq .
```

---

## Environment variables

Documented in `.env.example`. The only required variable is
`ANTHROPIC_API_KEY`; everything else has sensible defaults.

**Credentials are never hardcoded** — all keys load from environment via
`python-dotenv`.

---

## Observability (optional, via Langfuse)

When the `LANGFUSE_PUBLIC_KEY` env var is set, every `/ask` request becomes
a trace in the Langfuse UI showing the full agent loop — system prompt,
each tool call with args + result, latency per step, the final structured
envelope. Skipping setup is fine; the agent loop simply doesn't emit traces.

### Self-host in one command

```bash
docker compose -f docker-compose.langfuse.yml up -d
open http://localhost:3000
```

In the Langfuse UI: create an org, create a project, copy the **Public** and
**Secret** API keys, then add them to your `.env`:

```bash
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
LANGFUSE_HOST=http://localhost:3000
```

Restart `uvicorn` and the next `/ask` request will appear in the Langfuse
UI's *Tracing* tab.

### Or use Langfuse Cloud

Sign up at https://cloud.langfuse.com → create a project → copy keys →
set `LANGFUSE_HOST=https://cloud.langfuse.com`. No Docker needed.

The vendored `docker-compose.langfuse.yml` is Langfuse's official self-host
stack (6 services: web, worker, postgres, clickhouse, redis, minio). Default
secrets are sufficient for local prototype use — see the file's header for
the production-hardening note.

---

## Repository layout

```
.
├── CLAUDE.md                 # locked submission spec + working agreements
├── README.md                 # this file
├── report/                   # DOCX/PDF deliverable + assets
├── scripts/prepare_data.py   # offline pipeline (idempotent, resumable)
├── backend/                  # serving (api/agent/tools/memory) + preprocessing/ subpackage
├── ui/app.py                 # Streamlit analyst chat
├── eval/                     # ~15 Q/A pairs and the eval runner
├── examples/                 # sample curl requests and recorded outputs
├── data/                     # raw CSV + processed.db + chroma/ (gitignored)
└── pyproject.toml
```

---

## Limitations & next improvements

Tracked in the report's final section. High level:
- Topic taxonomy is closed-set and frozen — new conversation types beyond the
  pre-derived 15 labels (in `data/topic_taxonomy.yaml`) fall into an `unknown`
  bucket.
- Per-turn timestamps in the source CSV are not monotonic; we use `turn_index`
  for ordering and `min(turn.timestamp)` as the conversation start time.
- Single-region, single-process deployment; production would move SQLite →
  Postgres/Redshift and ChromaDB → OpenSearch / pgvector.
