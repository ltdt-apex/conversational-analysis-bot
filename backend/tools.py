"""The six tool functions the planner agent can call.

Each tool:
  - takes a Pydantic input model (validation runs once at the boundary)
  - returns a Pydantic output (typed all the way to FastAPI)
  - exposes ``definition()`` returning the Anthropic tool-use schema dict
  - is registered in :data:`TOOLS` keyed by name so the agent loop can
    dispatch by string

Every tool is read-only and side-effect-free. The agent cannot write to
SQLite or Chroma — by design.
"""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from pydantic import BaseModel

from backend import schemas as S
from backend import taxonomy as tax
from backend.config import Config
from backend.db import connect


# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------


@dataclass
class Tool:
    """A registered tool: name, description, input model, runner."""

    name: str
    description: str
    input_model: type[BaseModel]
    runner: Callable[[BaseModel, Config], BaseModel | list[BaseModel]]

    def definition(self) -> dict[str, Any]:
        """Anthropic tool-use input schema."""
        schema = self.input_model.model_json_schema()
        # Strip Pydantic's title metadata — keeps the prompt smaller.
        schema.pop("title", None)
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": schema,
        }

    def run(self, raw_args: dict[str, Any], cfg: Config) -> Any:
        """Validate args and execute. Returns the raw result for the agent loop."""
        args = self.input_model.model_validate(raw_args)
        return self.runner(args, cfg)


# ---------------------------------------------------------------------------
# 1. query_conversations
# ---------------------------------------------------------------------------


def _run_query_conversations(
    args: S.QueryConversationsInput, cfg: Config
) -> list[BaseModel]:
    """Aggregate or list rows from the ``conversations`` table."""
    where_clauses: list[str] = []
    params: list[Any] = []

    if args.month:
        where_clauses.append("strftime('%Y-%m', start_ts) = ?")
        params.append(args.month)
    if args.date_from:
        where_clauses.append("start_ts >= ?")
        params.append(args.date_from)
    if args.date_to:
        where_clauses.append("start_ts <= ?")
        params.append(args.date_to + "T23:59:59")
    if args.topic:
        where_clauses.append("topic = ?")
        params.append(args.topic)
    if args.sentiment_max is not None:
        where_clauses.append("customer_sentiment_overall <= ?")
        params.append(args.sentiment_max)
    if args.sentiment_min is not None:
        where_clauses.append("customer_sentiment_overall >= ?")
        params.append(args.sentiment_min)
    if args.escalation_flag is not None:
        where_clauses.append("escalation_flag = ?")
        params.append(int(args.escalation_flag))
    if args.resolution_flag is not None:
        where_clauses.append("resolution_flag = ?")
        params.append(int(args.resolution_flag))
    if args.agent_name:
        where_clauses.append("agent_name = ?")
        params.append(args.agent_name)
    if args.language:
        # `conversations` doesn't carry language, so join through any turn.
        where_clauses.append(
            "conv_id IN (SELECT conv_id FROM turns WHERE language = ?)"
        )
        params.append(args.language)

    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

    with connect(cfg.sqlite_path) as conn:
        if args.group_by is None:
            # Raw conversation rows.
            order_col = args.order_by or "start_ts"
            sql = f"""
                SELECT conv_id, customer_name, agent_name, start_ts, end_ts,
                       turn_count, topic, customer_sentiment_overall,
                       resolution_flag, escalation_flag, agent_empathy_mean
                  FROM conversations
                  {where_sql}
                 ORDER BY {_safe_col(order_col)} {args.order.upper()}
                 LIMIT ?
            """
            rows = conn.execute(sql, params + [args.limit]).fetchall()
            return [
                S.ConversationRow(
                    conv_id=r[0],
                    customer_name=r[1],
                    agent_name=r[2],
                    start_ts=r[3],
                    end_ts=r[4],
                    turn_count=r[5],
                    topic=r[6],
                    customer_sentiment_overall=r[7],
                    resolution_flag=r[8],
                    escalation_flag=r[9],
                    agent_empathy_mean=r[10],
                )
                for r in rows
            ]

        # Grouped aggregation. Alias each metric so ORDER BY <metric> works.
        select_cols = [f"{_GROUP_COL[args.group_by]} AS group_key"]
        for m in args.metrics:
            select_cols.append(f"{_METRIC_SQL[m]} AS {m}")
        order_col = (
            args.order_by if args.order_by in args.metrics else args.metrics[0]
        )
        sql = f"""
            SELECT {", ".join(select_cols)}
              FROM conversations
              {where_sql}
             GROUP BY {_GROUP_COL[args.group_by]}
             ORDER BY {order_col} {args.order.upper()}
             LIMIT ?
        """
        rows = conn.execute(sql, params + [args.limit]).fetchall()
        out: list[BaseModel] = []
        for r in rows:
            kwargs: dict[str, Any] = {"group_key": str(r[0]), "count": 0}
            for i, m in enumerate(args.metrics, start=1):
                kwargs[m if m != "count" else "count"] = r[i]
            out.append(S.ConversationAggregateRow(**kwargs))
        return out


_GROUP_COL: dict[str, str] = {
    "topic": "topic",
    "agent_name": "agent_name",
    "month": "strftime('%Y-%m', start_ts)",
    "language": "(SELECT language FROM turns WHERE turns.conv_id=conversations.conv_id LIMIT 1)",
}

_METRIC_SQL: dict[str, str] = {
    "count": "COUNT(*)",
    "avg_sentiment": "ROUND(AVG(customer_sentiment_overall), 3)",
    "avg_empathy": "ROUND(AVG(agent_empathy_mean), 3)",
    "escalation_rate": "ROUND(AVG(escalation_flag), 3)",
    "resolution_rate": "ROUND(AVG(resolution_flag), 3)",
}

_SAFE_RAW_COLS = {
    "start_ts",
    "end_ts",
    "customer_sentiment_overall",
    "turn_count",
    "escalation_flag",
    "resolution_flag",
    "agent_empathy_mean",
}


def _safe_col(name: str) -> str:
    return name if name in _SAFE_RAW_COLS else "start_ts"


# ---------------------------------------------------------------------------
# 2. query_agents
# ---------------------------------------------------------------------------


def _run_query_agents(args: S.QueryAgentsInput, cfg: Config) -> list[S.AgentRow]:
    with connect(cfg.sqlite_path) as conn:
        rows = conn.execute(
            f"""
            SELECT agent_name, conv_count, avg_customer_sentiment, empathy_mean,
                   resolution_rate, escalation_rate, top_topics
              FROM agents
             WHERE conv_count >= ?
             ORDER BY {args.sort_by} {args.order.upper()}, agent_name ASC
             LIMIT ?
            """,
            (args.min_conv_count, args.limit),
        ).fetchall()
    out: list[S.AgentRow] = []
    for r in rows:
        try:
            tt = [tuple(x) for x in json.loads(r[6] or "[]")]
        except (json.JSONDecodeError, ValueError):
            tt = []
        out.append(
            S.AgentRow(
                agent_name=r[0],
                conv_count=r[1],
                avg_customer_sentiment=r[2],
                empathy_mean=r[3],
                resolution_rate=r[4],
                escalation_rate=r[5],
                top_topics=tt,
            )
        )
    return out


# ---------------------------------------------------------------------------
# 3. semantic_search
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _embedding_model(model_name: str):
    """Load the sentence-transformer once per process."""
    from sentence_transformers import SentenceTransformer

    return SentenceTransformer(model_name)


@lru_cache(maxsize=1)
def _chroma_collection(chroma_path: str):
    import chromadb

    client = chromadb.PersistentClient(path=chroma_path)
    return client.get_collection("conversations")


def _run_semantic_search(
    args: S.SemanticSearchInput, cfg: Config
) -> list[S.SemanticHit]:
    model = _embedding_model(cfg.embedding_model)
    coll = _chroma_collection(str(cfg.chroma_path))
    vec = model.encode([args.query], normalize_embeddings=True)[0].tolist()

    # Build Chroma where-filter. Chroma 'where' takes a flat dict of equalities
    # plus operator dicts for ranges.
    where: dict[str, Any] = {}
    if args.topic:
        where["topic"] = args.topic
    if args.agent_name:
        where["agent_name"] = args.agent_name
    if args.language:
        where["language"] = args.language
    if args.escalation_flag is not None:
        where["escalation_flag"] = int(args.escalation_flag)
    if args.sentiment_max is not None or args.sentiment_min is not None:
        bounds: dict[str, float] = {}
        if args.sentiment_max is not None:
            bounds["$lte"] = args.sentiment_max
        if args.sentiment_min is not None:
            bounds["$gte"] = args.sentiment_min
        where["customer_sentiment_overall"] = bounds

    # Chroma rejects empty {} but is happy with no key.
    query_kwargs: dict[str, Any] = {
        "query_embeddings": [vec],
        "n_results": args.k,
        "include": ["documents", "metadatas", "distances"],
    }
    if where:
        # Chroma expects $and only when there are 2+ predicates.
        if len(where) >= 2:
            query_kwargs["where"] = {"$and": [{k: v} for k, v in where.items()]}
        else:
            query_kwargs["where"] = where

    res = coll.query(**query_kwargs)

    hits: list[S.SemanticHit] = []
    if not res["ids"] or not res["ids"][0]:
        return hits
    for i in range(len(res["ids"][0])):
        meta = res["metadatas"][0][i]
        doc = res["documents"][0][i]
        excerpt = "\n".join(doc.splitlines()[:6])  # first ~3 turn pairs
        hits.append(
            S.SemanticHit(
                conv_id=res["ids"][0][i],
                similarity=1.0 - float(res["distances"][0][i]),
                document_excerpt=excerpt,
                topic=str(meta.get("topic", "unknown")),
                agent_name=str(meta.get("agent_name", "")),
                customer_sentiment_overall=float(
                    meta.get("customer_sentiment_overall", 0.0)
                ),
                language=str(meta.get("language", "unknown")),
                escalation_flag=int(meta.get("escalation_flag", 0)),
            )
        )
    return hits


# ---------------------------------------------------------------------------
# 4. get_trajectory
# ---------------------------------------------------------------------------


def _run_get_trajectory(
    args: S.GetTrajectoryInput, cfg: Config
) -> S.TrajectoryResponse:
    with connect(cfg.sqlite_path) as conn:
        head = conn.execute(
            """SELECT topic, customer_sentiment_overall, resolution_flag,
                      escalation_flag, sentiment_trajectory
                 FROM conversations WHERE conv_id = ?""",
            (args.conv_id,),
        ).fetchone()
        if head is None:
            raise ValueError(f"conv_id {args.conv_id!r} not found")
        turns = conn.execute(
            """SELECT turn_index, role, sentiment_label, sentiment_score,
                      intent, empathy_signal, text_clean
                 FROM turns WHERE conv_id = ?
                 ORDER BY turn_index ASC""",
            (args.conv_id,),
        ).fetchall()

    try:
        arc = list(json.loads(head["sentiment_trajectory"] or "[]"))
    except (json.JSONDecodeError, TypeError):
        arc = []

    return S.TrajectoryResponse(
        conv_id=args.conv_id,
        topic=head["topic"],
        customer_sentiment_overall=head["customer_sentiment_overall"],
        resolution_flag=head["resolution_flag"],
        escalation_flag=head["escalation_flag"],
        customer_arc=arc,
        turns=[
            S.TrajectoryTurn(
                turn_index=t[0],
                role=t[1],
                sentiment_label=t[2],
                sentiment_score=t[3],
                intent=t[4],
                empathy_signal=t[5],
                text_clean=t[6],
            )
            for t in turns
        ],
    )


# ---------------------------------------------------------------------------
# 5. get_conversation
# ---------------------------------------------------------------------------


def _run_get_conversation(
    args: S.GetConversationInput, cfg: Config
) -> S.ConversationDetail:
    with connect(cfg.sqlite_path) as conn:
        head = conn.execute(
            """SELECT customer_name, agent_name, start_ts, end_ts, topic,
                      customer_sentiment_overall, resolution_flag, escalation_flag
                 FROM conversations WHERE conv_id = ?""",
            (args.conv_id,),
        ).fetchone()
        if head is None:
            raise ValueError(f"conv_id {args.conv_id!r} not found")
        turn_rows = conn.execute(
            """SELECT turn_index, role, timestamp, text_clean, text_clean_en,
                      language, sentiment_label, sentiment_score, intent,
                      empathy_signal, is_escalation, contains_pii
                 FROM turns WHERE conv_id = ?
                 ORDER BY turn_index ASC""",
            (args.conv_id,),
        ).fetchall()

    turns = [
        S.FullTurn(
            turn_index=t[0],
            role=t[1],
            timestamp=t[2],
            text_clean=t[3],
            text_clean_en=t[4] if args.include_translation else None,
            language=t[5],
            sentiment_label=t[6],
            sentiment_score=t[7],
            intent=t[8],
            empathy_signal=t[9],
            is_escalation=t[10],
            contains_pii=t[11],
        )
        for t in turn_rows
    ]

    return S.ConversationDetail(
        conv_id=args.conv_id,
        customer_name=head["customer_name"],
        agent_name=head["agent_name"],
        start_ts=head["start_ts"],
        end_ts=head["end_ts"],
        topic=head["topic"],
        customer_sentiment_overall=head["customer_sentiment_overall"],
        resolution_flag=head["resolution_flag"],
        escalation_flag=head["escalation_flag"],
        turns=turns,
    )


# ---------------------------------------------------------------------------
# 6. list_topics
# ---------------------------------------------------------------------------


def _run_list_topics(args: S.ListTopicsInput, cfg: Config) -> list[S.TopicEntry]:
    taxonomy = tax.load(cfg.data_dir / "topic_taxonomy.yaml")
    with connect(cfg.sqlite_path) as conn:
        counts = dict(
            conn.execute("SELECT topic, COUNT(*) FROM conversations GROUP BY topic").fetchall()
        )
        examples: dict[str, str] = {}
        if args.with_examples:
            # One representative conv_id per topic — picks the most-negative
            # conversation to make it analytically interesting.
            for r in conn.execute(
                """SELECT topic, conv_id FROM conversations
                    GROUP BY topic
                    HAVING MIN(customer_sentiment_overall)
                """
            ):
                examples[r[0]] = r[1]

    out: list[S.TopicEntry] = []
    for c in taxonomy.categories:
        out.append(
            S.TopicEntry(
                id=c.id,
                label=c.label,
                description=c.description,
                count_in_dataset=counts.get(c.id, 0),
                example_conv_id=examples.get(c.id),
            )
        )
    return out


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


TOOLS: dict[str, Tool] = {
    "query_conversations": Tool(
        name="query_conversations",
        description=(
            "Filtered/grouped aggregation over the conversations table. Use "
            "this for counts, rankings, averages, and time-bucketed metrics. "
            "Set group_by to aggregate, leave it None for raw conversation rows."
        ),
        input_model=S.QueryConversationsInput,
        runner=_run_query_conversations,
    ),
    "query_agents": Tool(
        name="query_agents",
        description=(
            "Filtered/sorted query over the agents rollup table. Use for "
            "agent-centric questions (coaching, performance rankings). Dataset "
            "is sparse: most agents handle 1 conversation, max is 2."
        ),
        input_model=S.QueryAgentsInput,
        runner=_run_query_agents,
    ),
    "semantic_search": Tool(
        name="semantic_search",
        description=(
            "Cosine-similarity search over all 3000 conversations. Use to "
            "find conversations by MEANING or TONE that aren't captured as "
            "structured columns. Multilingual — English queries also surface "
            "Hindi/Hinglish conversations. Always prefer this when the user "
            "asks for examples, evidence, or 'similar cases'."
        ),
        input_model=S.SemanticSearchInput,
        runner=_run_semantic_search,
    ),
    "get_trajectory": Tool(
        name="get_trajectory",
        description=(
            "Per-turn sentiment arc for a single conversation. Use this for "
            "questions about how sentiment changed within ONE conversation. "
            "Returns customer_arc (compact sentiment scores) plus per-turn "
            "details (role, intent, empathy, short text)."
        ),
        input_model=S.GetTrajectoryInput,
        runner=_run_get_trajectory,
    ),
    "get_conversation": Tool(
        name="get_conversation",
        description=(
            "Full transcript of one conversation for quoting in evidence. "
            "Use after semantic_search or query_conversations has identified a "
            "specific conv_id worth quoting."
        ),
        input_model=S.GetConversationInput,
        runner=_run_get_conversation,
    ),
    "list_topics": Tool(
        name="list_topics",
        description=(
            "Return the closed-set topic taxonomy (15 categories) with "
            "conversation counts. Call this first when you need a valid "
            "topic id for a filter argument."
        ),
        input_model=S.ListTopicsInput,
        runner=_run_list_topics,
    ),
}


def all_definitions() -> list[dict[str, Any]]:
    """Tool definitions in the format Anthropic's tool-use API expects."""
    return [t.definition() for t in TOOLS.values()]


def call(name: str, raw_args: dict[str, Any], cfg: Config) -> Any:
    """Dispatch a tool call by name."""
    if name not in TOOLS:
        raise KeyError(f"unknown tool: {name!r}")
    return TOOLS[name].run(raw_args, cfg)
