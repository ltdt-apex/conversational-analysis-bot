# Conversation Analysis Bot

Prototype that lets a contact-centre analyst ask analytical questions over a
3,000-conversation customer-support dataset and get evidence-backed answers
through an agent workflow.

---

## Setup

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

Cleans the raw CSV, labels every distinct turn (sentiment, intent, empathy,
language, …), rolls up to conversation and agent level, and builds the
multilingual vector index. One-shot, idempotent — safe to re-run; finished
stages skip on subsequent runs.

The raw dataset (~23 MB) ships in the repo at `data/cs_conversations.csv`,
so no extra download is needed.

```bash
uv run python scripts/prepare_data.py
```

---

## Run

After preprocessing is done:

```bash
# Terminal 1 — start the API
uv run uvicorn backend.api:app --reload --port 8000

# Terminal 2 — start the analyst chat UI
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
