"""SQLite schema + connection helper for the processed data store.

Three tables:
  turns           - one row per conversation turn, with per-turn classifications
  conversations   - one row per conv_id, with rollups
  agents          - one row per agent_name, with rollups

Tables are created lazily; prepare_data.py is the canonical writer.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
CREATE TABLE IF NOT EXISTS turns (
    conv_id        TEXT NOT NULL,
    turn_index     INTEGER NOT NULL,
    timestamp      TEXT NOT NULL,
    role           TEXT NOT NULL,
    text_raw       TEXT NOT NULL,
    text_clean     TEXT NOT NULL,
    -- Bilingual support: the dataset mixes English and romanized Hindi/Hinglish.
    -- `text_clean` is always the original; `text_clean_en` is an English
    -- translation populated by the classifier (= text_clean for English turns).
    -- `language` is one of 'en' | 'hi' | 'mixed' | NULL (before classification).
    language          TEXT,
    text_clean_en     TEXT,
    customer_name  TEXT,
    agent_name     TEXT,
    -- per-turn classification (populated by the LLM classifier; NULL until then)
    sentiment_label   TEXT,    -- 'pos' | 'neu' | 'neg'
    sentiment_score   REAL,    -- in [-1, 1]
    intent            TEXT,    -- coarse intent label
    empathy_signal    TEXT,    -- 'empathetic' | 'neutral' | 'dismissive' (agent only)
    is_escalation     INTEGER, -- 0/1
    contains_pii      INTEGER, -- 0/1
    classified_at     TEXT,
    PRIMARY KEY (conv_id, turn_index)
);

CREATE INDEX IF NOT EXISTS turns_conv_idx       ON turns(conv_id);
CREATE INDEX IF NOT EXISTS turns_agent_idx      ON turns(agent_name);
CREATE INDEX IF NOT EXISTS turns_classified_idx ON turns(classified_at);

CREATE TABLE IF NOT EXISTS conversations (
    conv_id                    TEXT PRIMARY KEY,
    customer_name              TEXT,
    agent_name                 TEXT,
    start_ts                   TEXT,    -- min(turn.timestamp)
    end_ts                     TEXT,    -- max(turn.timestamp)
    turn_count                 INTEGER,
    topic                      TEXT,
    customer_sentiment_overall REAL,    -- mean of customer-turn sentiment scores
    sentiment_trajectory       TEXT,    -- JSON list of customer-turn scores, in turn order
    resolution_flag            INTEGER,
    escalation_flag            INTEGER,
    agent_empathy_mean         REAL,
    contains_pii_any           INTEGER
);

CREATE INDEX IF NOT EXISTS conv_topic_idx ON conversations(topic);
CREATE INDEX IF NOT EXISTS conv_agent_idx ON conversations(agent_name);
CREATE INDEX IF NOT EXISTS conv_start_idx ON conversations(start_ts);

CREATE TABLE IF NOT EXISTS agents (
    agent_name              TEXT PRIMARY KEY,
    conv_count              INTEGER,
    avg_customer_sentiment  REAL,
    empathy_mean            REAL,
    resolution_rate         REAL,
    escalation_rate         REAL,
    top_topics              TEXT  -- JSON list of (topic, count)
);

CREATE TABLE IF NOT EXISTS sessions (
    session_id   TEXT NOT NULL,
    turn_idx     INTEGER NOT NULL,
    created_at   TEXT NOT NULL,
    expires_at   TEXT NOT NULL,
    question     TEXT NOT NULL,
    answer_json  TEXT NOT NULL,
    PRIMARY KEY (session_id, turn_idx)
);
CREATE INDEX IF NOT EXISTS sessions_expires_idx ON sessions(expires_at);
"""


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None)  # autocommit
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.row_factory = sqlite3.Row
    return conn


# Idempotent column additions. When the schema gains a new nullable column,
# add a (table, column, type) entry here so existing databases catch up
# without needing a full --force re-ingest. SQLite has no `ADD COLUMN IF NOT
# EXISTS`, so we check `PRAGMA table_info` first.
_ADDITIVE_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("turns", "language", "TEXT"),
    ("turns", "text_clean_en", "TEXT"),
)


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    for table, col, typ in _ADDITIVE_COLUMNS:
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if col not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")
