# Conversation Analysis Bot

Prototype that lets a contact-centre analyst ask analytical questions over a
3,000-conversation customer-support dataset and get evidence-backed answers
through an agent workflow.

---

## Setup — Docker (recommended)

Three services, three images, one command. Each service has its own
Dockerfile with only the dependencies it needs:

| Service | Dockerfile | Port | Role |
|---|---|---|---|
| `prepare` | `preprocess/Dockerfile` | — | One-shot offline preprocessing |
| `api` | `backend/Dockerfile` | 8000 | FastAPI serving the agent loop |
| `ui` | `ui/Dockerfile` | 8501 | Streamlit chat (slim — no torch/chromadb) |

The preprocess step take around 5 minute to generate labels (e.g. sentiment score) for fast data lookup in real step.

To save time, I committed the preprocessed artifacts (SQLite + ChromaDB, ~66 MB)
under `data/` to make setup easier and skip the ~5-minute preprocess step

You can delete `data/processed.db` and `data/chroma/` if
you want to test the preprocess step.

```bash
# 1. Configure API key (.env is gitignored)
cp .env.example .env
$EDITOR .env                     # set ANTHROPIC_API_KEY

# 2. Build + start everything
docker compose up --build
```

Then visit **http://localhost:8501** for the analyst chat.

### Quick check after `up`

```bash
curl -fsS http://localhost:8000/                          # → {"status":"ok",...}
curl -fsS http://localhost:8501/_stcore/health            # → ok
```

---

## Setup — local Python (no Docker)

### 1. Install dependencies

```bash
uv sync                          # or: pip install -e .
```

### 2. Configure API key

```bash
cp .env.example .env
$EDITOR .env                     # set ANTHROPIC_API_KEY
```

### 3. Run the offline preprocessing pipeline

```bash
uv run python preprocess/prepare_data.py
```

### 4. Start the API and the UI

```bash
# Terminal 1 — API
uv run uvicorn backend.api:app --reload --port 8000

# Terminal 2 — UI
uv run streamlit run ui/app.py
```

Then visit **http://localhost:8501** for the analyst chat.

---

## Sample requests

```bash
curl -s -X POST http://localhost:8000/ask \
  -H 'content-type: application/json' \
  -d '{"question": "Top 5 topics with the most negative sentiment in November 2025"}' \
  | jq .
```

Response shape:

```json
{
  "session_id": "abc123...",
  "envelope": {
    "answer": "In November 2025, the five topics ...",
    "evidence": [
      {"conv_id": "C0001241", "quote": "...", "relevance": "..."}
    ],
    "tool_calls": [
      {"tool": "query_conversations", "arguments": {"...": "..."},
       "result_summary": "5 row(s)"}
    ],
    "reasoning_brief": "...",
    "uncertainty": null
  }
}
```

---

## Environment variables

Every variable is documented in `.env.example`. The only required one is
`ANTHROPIC_API_KEY`; everything else has a sensible default. Credentials are
loaded from `.env` via `python-dotenv` and are never hardcoded.

---

## Preprocess pipeline

Run once offline to turn the raw CSV into a queryable SQLite database and a
vector store. Five stages run in sequence:

```
cs_conversations.csv
  │
  ▼ Stage 1 — text cleaning       (preprocess/text.py)
  │   Strips gibberish suffixes by truncating each turn at the last ./?/!
  │   ~426 distinct cleaned prefixes emerge from 41,965 raw turns
  │
  ▼ Stage 2 — topic taxonomy      (preprocess/taxonomy.py)
  │   Extracts the English topic slot from each customer-opener template
  │   Groups 51 raw slot values → 15 topic labels via a single Sonnet call
  │   Result frozen to data/topic_taxonomy.yaml
  │
  ▼ Stage 3 — per-prefix classifier  (preprocess/classifier.py)
  │   Sends the ~426 distinct prefixes to Claude Haiku 4.5 in parallel batches
  │   Each prefix gets: sentiment label/score, intent, empathy signal,
  │   escalation flag, PII flag, language (en/hi/mixed), English translation
  │   Results joined back to all 41,965 turns
  │
  ▼ Stage 4 — rollups              (preprocess/rollups.py)
  │   Aggregates turn-level labels into per-conversation and per-agent rows:
  │   avg sentiment, escalation/resolution flags, empathy mean, top topics
  │   Writes to SQLite tables: turns / conversations / agents
  │
  ▼ Stage 5 — embeddings           (preprocess/embed.py)
      One document per conversation (summary + metadata) embedded with
      paraphrase-multilingual-MiniLM-L12-v2 and stored in ChromaDB
      Supports bilingual semantic search (English + Hindi/Hinglish)
```

---

## Agent flow

Each `/ask` request runs a Planner → tool loop → Synthesizer cycle:

```
User question
  │
  ▼ Planner (Claude Sonnet 4.6, ReAct loop)
  │   Reads the question and decides which tool to call next.
  │   Repeats up to 10 times: think → call tool → observe result → think again.
  │
  ├─► tool-calling          6 tools backed by SQLite and ChromaDB
  │                         (SQL aggregations, agent rankings, semantic search,
  │                         per-conversation transcripts and sentiment arcs)
  │
  │─► emit_answer (structured sink)
  │    When the planner has enough evidence it calls │emit_answer to produce:
  │      answer          — prose, 1-3 paragraphs
  │      evidence[]      — conv_id + quote + relevance for │each cited conversation
  │      tool_calls[]    — full trace of every tool invoked
  │      reasoning_brief — one sentence on how the answer was │built
  │      uncertainty     — optional caveat when data is thin
  │
  ▼ FastAPI returns AnswerEnvelope → Streamlit renders answer + evidence cards
```
