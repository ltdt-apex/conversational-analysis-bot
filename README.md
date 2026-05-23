# Conversation Analysis Bot

Prototype that lets a contact-centre analyst ask analytical questions over a
3,000-conversation customer-support dataset and get evidence-backed answers
through an agent workflow.

I committed the preprocessed artifacts (SQLite + ChromaDB, ~66 MB)
under `data/` to make setup easier and skip the ~5-minute preprocess step
on first run. You can delete `data/processed.db` and `data/chroma/` if
you want to test the preprocess pipeline yourself.

---

## Setup — Docker (recommended)

Three services, three images, one command. Each service has its own
Dockerfile with only the dependencies it needs:

| Service | Dockerfile | Port | Role |
|---|---|---|---|
| `prepare` | `preprocess/Dockerfile` | — | One-shot offline preprocessing |
| `api` | `backend/Dockerfile` | 8000 | FastAPI serving the agent loop |
| `ui` | `ui/Dockerfile` | 8501 | Streamlit chat (slim — no torch/chromadb) |

```bash
# 1. Configure secrets (.env is gitignored)
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

### 2. Configure secrets

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

# Terminal 2 — analyst chat UI
uv run streamlit run ui/app.py
```

Open the Streamlit URL it prints and ask a question.

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

A full set of recorded outputs across 15 sample questions (covering all four
assignment example questions plus follow-ups in English and Hindi/Hinglish)
lives in `eval/results-latest.json`.

---

## Environment variables

Every variable is documented in `.env.example`. The only required one is
`ANTHROPIC_API_KEY`; everything else has a sensible default. Credentials are
loaded from `.env` via `python-dotenv` and are never hardcoded.
