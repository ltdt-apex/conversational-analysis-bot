"""Conversation- and agent-level rollups built from the per-turn ``turns`` table.

This stage runs after per-turn classification has populated every row in
``turns`` (sentiment_score, intent, empathy_signal, is_escalation,
contains_pii, language, text_clean_en).

It is pure SQL/Python aggregation — no LLM calls. Outputs:
  - ``conversations``: one row per ``conv_id``
  - ``agents``: one row per ``agent_name``

Idempotency: if ``conversations`` already has rows and ``force`` is False, we
print a skip message and return. With ``force`` we DELETE both rollup tables
before recomputing.

Notes on the spec (and assumptions verified against the live DB):

  * ``customer_name``/``agent_name`` are conv-constant — verified zero
    conversations have more than one distinct value, so picking any turn is safe.
  * Every conversation has a ``role='customer'`` turn at ``turn_index=0`` —
    verified. We still defensively code for the case where it might not.
  * Aggregations are language-invariant — we never filter by ``language``.
  * Empathy mapping (per spec): empathetic=1.0, neutral=0.5, dismissive=0.0.
    Turns with ``empathy_signal='na'`` are excluded from empathy means.
  * ``resolution_flag``: 1 iff any of the LAST TWO turns (highest two
    ``turn_index`` values) is a customer turn with intent in
    {resolution_confirmation, thanks}.
  * ``escalation_flag``: 1 iff MAX(is_escalation) over all turns of the conv is 1.
  * ``contains_pii_any``: 1 iff MAX(contains_pii) over all turns is 1.
  * ``topic``: resolved via ``Taxonomy.topic_for(first_customer_turn.text_clean)``;
    if the taxonomy returns None (unrecognised opener), we store ``'unknown'``.
"""
from __future__ import annotations

import json
import sqlite3
from collections import Counter
from statistics import fmean

from backend.config import Config
from backend.db import connect, init_schema, transaction
from backend import taxonomy as tax


# Per-spec empathy score mapping. 'na' (typically customer turns) is excluded
# from means by callers — we never look it up here.
_EMPATHY_SCORE: dict[str, float] = {
    "empathetic": 1.0,
    "neutral": 0.5,
    "dismissive": 0.0,
}


def run(cfg: Config, *, force: bool = False) -> None:
    """Build conversation- and agent-level rollups from the ``turns`` table."""
    taxonomy_path = cfg.data_dir / "topic_taxonomy.yaml"
    if not taxonomy_path.exists():
        raise FileNotFoundError(
            f"Topic taxonomy not found at {taxonomy_path}. "
            "Run `prepare_data.py --stage taxonomy` first."
        )
    taxonomy = tax.load(taxonomy_path)

    with connect(cfg.sqlite_path) as conn:
        init_schema(conn)

        existing = conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
        if existing > 0 and not force:
            print(
                f"[rollups] skipping — {existing:,} rows already in conversations "
                "table (use --force to recompute)"
            )
            return

        if force:
            print("[rollups] --force: truncating conversations and agents")
            conn.execute("DELETE FROM conversations")
            conn.execute("DELETE FROM agents")

        # Sanity check: classification must be complete before we aggregate.
        unclassified = conn.execute(
            "SELECT COUNT(*) FROM turns WHERE sentiment_label IS NULL"
        ).fetchone()[0]
        if unclassified > 0:
            raise RuntimeError(
                f"[rollups] {unclassified:,} turns are still unclassified — "
                "run `prepare_data.py --stage classify` first."
            )

        conv_rows = _build_conversation_rows(conn, taxonomy)
        print(f"[rollups] computed {len(conv_rows):,} conversation rows")
        _insert_conversations(conn, conv_rows)

        agent_rows = _build_agent_rows(conn)
        print(f"[rollups] computed {len(agent_rows):,} agent rows")
        _insert_agents(conn, agent_rows)

        # Final readout.
        n_conv = conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
        n_agent = conn.execute("SELECT COUNT(*) FROM agents").fetchone()[0]
        n_unknown_topic = conn.execute(
            "SELECT COUNT(*) FROM conversations WHERE topic='unknown'"
        ).fetchone()[0]
        print(
            f"[rollups] done — {n_conv:,} conversations, {n_agent:,} agents, "
            f"{n_unknown_topic:,} convs with topic='unknown'"
        )


# ---------------------------------------------------------------------------
# Conversation rollups
# ---------------------------------------------------------------------------


def _build_conversation_rows(
    conn: sqlite3.Connection, taxonomy: tax.Taxonomy
) -> list[tuple]:
    """Compute one rollup row per conv_id.

    Strategy: stream every turn once in (conv_id, turn_index) order and fold
    each conversation's running state in pure Python. This is O(N) over turns
    and trivially handles the "last two turns" rule without a self-join.
    """
    cur = conn.execute(
        """
        SELECT conv_id, turn_index, timestamp, role, text_clean, customer_name,
               agent_name, sentiment_score, intent, empathy_signal,
               is_escalation, contains_pii
          FROM turns
         ORDER BY conv_id, turn_index
        """
    )

    rows: list[tuple] = []
    current_id: str | None = None
    buf: list[sqlite3.Row] = []

    for r in cur:
        if r["conv_id"] != current_id:
            if current_id is not None:
                rows.append(_finalize_conversation(current_id, buf, taxonomy))
            current_id = r["conv_id"]
            buf = []
        buf.append(r)
    if current_id is not None and buf:
        rows.append(_finalize_conversation(current_id, buf, taxonomy))
    return rows


def _finalize_conversation(
    conv_id: str, turns: list[sqlite3.Row], taxonomy: tax.Taxonomy
) -> tuple:
    """Reduce one conversation's turns (already ordered by turn_index) to a row tuple."""
    # Conv-constant fields. Pick the first non-null we see.
    customer_name = next((t["customer_name"] for t in turns if t["customer_name"]), None)
    agent_name = next((t["agent_name"] for t in turns if t["agent_name"]), None)

    timestamps = [t["timestamp"] for t in turns if t["timestamp"]]
    start_ts = min(timestamps) if timestamps else None
    end_ts = max(timestamps) if timestamps else None
    turn_count = len(turns)

    # Topic resolution from the first customer turn (smallest turn_index where
    # role='customer'). Turns are already ordered by turn_index so we take the
    # first hit.
    first_customer = next((t for t in turns if t["role"] == "customer"), None)
    if first_customer is not None:
        topic = taxonomy.topic_for(first_customer["text_clean"]) or "unknown"
    else:
        topic = "unknown"

    # Customer sentiment series, ordered by turn_index (== buffer order).
    customer_scores = [
        t["sentiment_score"]
        for t in turns
        if t["role"] == "customer" and t["sentiment_score"] is not None
    ]
    customer_sentiment_overall = (
        fmean(customer_scores) if customer_scores else None
    )
    sentiment_trajectory = json.dumps(customer_scores)

    # Resolution: did any of the LAST TWO turns close the loop?
    # Take the two highest-turn_index rows. The buffer is already sorted by
    # turn_index ascending, so the last two entries are the right ones.
    last_two = turns[-2:]
    resolution_flag = int(
        any(
            t["role"] == "customer"
            and t["intent"] in ("resolution_confirmation", "thanks")
            for t in last_two
        )
    )

    escalation_flag = int(any((t["is_escalation"] or 0) == 1 for t in turns))
    contains_pii_any = int(any((t["contains_pii"] or 0) == 1 for t in turns))

    # Agent empathy mean — only over agent turns with a real label.
    empathy_scores = [
        _EMPATHY_SCORE[t["empathy_signal"]]
        for t in turns
        if t["role"] == "agent"
        and t["empathy_signal"] is not None
        and t["empathy_signal"] != "na"
        and t["empathy_signal"] in _EMPATHY_SCORE
    ]
    agent_empathy_mean = fmean(empathy_scores) if empathy_scores else None

    return (
        conv_id,
        customer_name,
        agent_name,
        start_ts,
        end_ts,
        turn_count,
        topic,
        customer_sentiment_overall,
        sentiment_trajectory,
        resolution_flag,
        escalation_flag,
        agent_empathy_mean,
        contains_pii_any,
    )


def _insert_conversations(conn: sqlite3.Connection, rows: list[tuple]) -> None:
    with transaction(conn):
        conn.executemany(
            """INSERT INTO conversations
                 (conv_id, customer_name, agent_name, start_ts, end_ts,
                  turn_count, topic, customer_sentiment_overall,
                  sentiment_trajectory, resolution_flag, escalation_flag,
                  agent_empathy_mean, contains_pii_any)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )


# ---------------------------------------------------------------------------
# Agent rollups
# ---------------------------------------------------------------------------


def _build_agent_rows(conn: sqlite3.Connection) -> list[tuple]:
    """Compute one rollup row per agent_name.

    Most metrics come from ``conversations`` (which was just populated), since
    that's where per-conv resolution/escalation/sentiment flags live.
    ``empathy_mean`` is computed across all of an agent's turns directly from
    ``turns``, which is faithful to the spec's wording ("mean empathy score
    across all this agent's turns").
    """
    # Per-conv metrics keyed by agent.
    conv_rows = list(
        conn.execute(
            """
            SELECT agent_name, conv_id, topic, customer_sentiment_overall,
                   resolution_flag, escalation_flag
              FROM conversations
             WHERE agent_name IS NOT NULL
            """
        )
    )

    by_agent: dict[str, dict] = {}
    for r in conv_rows:
        a = by_agent.setdefault(
            r["agent_name"],
            {
                "convs": [],
                "sent": [],
                "resolution": [],
                "escalation": [],
                "topics": Counter(),
            },
        )
        a["convs"].append(r["conv_id"])
        if r["customer_sentiment_overall"] is not None:
            a["sent"].append(r["customer_sentiment_overall"])
        a["resolution"].append(r["resolution_flag"] or 0)
        a["escalation"].append(r["escalation_flag"] or 0)
        a["topics"][r["topic"] or "unknown"] += 1

    # Empathy mean: average over all agent turns (any conv) where empathy_signal
    # is one of {empathetic, neutral, dismissive}. We compute this with a
    # single sweep over turns rather than per-agent queries.
    empathy_sum: dict[str, float] = {}
    empathy_n: dict[str, int] = {}
    for r in conn.execute(
        """
        SELECT agent_name, empathy_signal
          FROM turns
         WHERE role='agent'
           AND agent_name IS NOT NULL
           AND empathy_signal IS NOT NULL
           AND empathy_signal != 'na'
        """
    ):
        score = _EMPATHY_SCORE.get(r["empathy_signal"])
        if score is None:
            continue
        empathy_sum[r["agent_name"]] = empathy_sum.get(r["agent_name"], 0.0) + score
        empathy_n[r["agent_name"]] = empathy_n.get(r["agent_name"], 0) + 1

    rows: list[tuple] = []
    for agent, a in by_agent.items():
        conv_count = len(a["convs"])
        avg_sent = fmean(a["sent"]) if a["sent"] else None
        n_emp = empathy_n.get(agent, 0)
        empathy_mean = (empathy_sum[agent] / n_emp) if n_emp else None
        resolution_rate = (sum(a["resolution"]) / conv_count) if conv_count else None
        escalation_rate = (sum(a["escalation"]) / conv_count) if conv_count else None
        top_topics = json.dumps(
            [list(item) for item in a["topics"].most_common(3)]
        )
        rows.append(
            (
                agent,
                conv_count,
                avg_sent,
                empathy_mean,
                resolution_rate,
                escalation_rate,
                top_topics,
            )
        )
    return rows


def _insert_agents(conn: sqlite3.Connection, rows: list[tuple]) -> None:
    with transaction(conn):
        conn.executemany(
            """INSERT INTO agents
                 (agent_name, conv_count, avg_customer_sentiment, empathy_mean,
                  resolution_rate, escalation_rate, top_topics)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )
