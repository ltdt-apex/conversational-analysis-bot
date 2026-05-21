"""Pydantic schemas for tool I/O and the response envelope.

Every tool input/output is a Pydantic model so:
  - validation runs once per request
  - the Anthropic tool-use ``input_schema`` is derived from the model rather
    than hand-written
  - the FastAPI response is automatically OpenAPI-documented
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Tool INPUT schemas
# ---------------------------------------------------------------------------


class QueryConversationsInput(BaseModel):
    """Filtered/grouped aggregation over the ``conversations`` table."""

    month: str | None = Field(
        default=None,
        description=(
            "Calendar-month filter as 'YYYY-MM' (e.g. '2025-11'). Matches "
            "conversations whose start_ts falls in that month. Use this for "
            "'last month in the dataset' (= November 2025)."
        ),
    )
    date_from: str | None = Field(
        default=None,
        description="Inclusive ISO date lower bound (e.g. '2025-10-01').",
    )
    date_to: str | None = Field(
        default=None,
        description="Inclusive ISO date upper bound (e.g. '2025-11-30').",
    )
    topic: str | None = Field(
        default=None,
        description=(
            "Filter to a single topic id from the closed-set taxonomy "
            "(call list_topics first if unsure)."
        ),
    )
    sentiment_max: float | None = Field(
        default=None,
        description=(
            "Upper bound on customer_sentiment_overall. Use -0.1 for "
            "'negative-leaning', -0.3 for 'clearly negative'."
        ),
    )
    sentiment_min: float | None = Field(
        default=None, description="Lower bound on customer_sentiment_overall."
    )
    escalation_flag: bool | None = Field(
        default=None, description="If set, filter to conversations with this flag."
    )
    resolution_flag: bool | None = Field(
        default=None, description="If set, filter to conversations with this flag."
    )
    language: Literal["en", "hi", "mixed"] | None = Field(
        default=None,
        description=(
            "Majority language of the conversation. Don't use this unless the "
            "question is explicitly language-scoped."
        ),
    )
    agent_name: str | None = Field(
        default=None, description="Filter to conversations handled by this agent."
    )
    group_by: Literal["topic", "agent_name", "month", "language"] | None = Field(
        default=None,
        description=(
            "If set, aggregate by this column and return one row per group. "
            "If not set, returns individual conversation rows."
        ),
    )
    metrics: list[
        Literal[
            "count",
            "avg_sentiment",
            "avg_empathy",
            "escalation_rate",
            "resolution_rate",
        ]
    ] = Field(
        default_factory=lambda: ["count"],
        description=(
            "Metrics to compute when group_by is set. Ignored when group_by is "
            "None (per-conversation rows always come back fully populated)."
        ),
    )
    order_by: str | None = Field(
        default=None,
        description=(
            "Metric or column to order by. For grouped results, must be one of "
            "`metrics` (count, avg_sentiment, etc.). For raw rows, may be "
            "start_ts or customer_sentiment_overall."
        ),
    )
    order: Literal["asc", "desc"] = Field(default="desc")
    limit: int = Field(default=10, ge=1, le=100)


class QueryAgentsInput(BaseModel):
    """Filtered/sorted query over the ``agents`` rollup table."""

    min_conv_count: int = Field(
        default=1,
        ge=1,
        description=(
            "Lower bound on conv_count. NOTE: dataset has 1.004 conversations "
            "per agent on average — setting this >2 will return 0 rows."
        ),
    )
    sort_by: Literal[
        "empathy_mean",
        "resolution_rate",
        "avg_customer_sentiment",
        "escalation_rate",
        "conv_count",
    ] = Field(default="empathy_mean")
    order: Literal["asc", "desc"] = Field(default="asc")
    limit: int = Field(default=10, ge=1, le=100)


class SemanticSearchInput(BaseModel):
    """Cosine-similarity search over the ChromaDB ``conversations`` collection."""

    query: str = Field(description="Natural-language query (any language).")
    k: int = Field(default=5, ge=1, le=20)
    topic: str | None = Field(default=None, description="Restrict to this topic id.")
    agent_name: str | None = Field(default=None)
    language: Literal["en", "hi", "mixed"] | None = Field(default=None)
    escalation_flag: bool | None = Field(default=None)
    sentiment_max: float | None = Field(
        default=None,
        description="Restrict to conversations with customer_sentiment_overall <= this.",
    )
    sentiment_min: float | None = Field(default=None)


class GetTrajectoryInput(BaseModel):
    """Per-turn sentiment arc for a single conversation."""

    conv_id: str


class GetConversationInput(BaseModel):
    """Full transcript of a single conversation, for quoting."""

    conv_id: str
    include_translation: bool = Field(
        default=False,
        description=(
            "If True, include text_clean_en alongside the original text_clean. "
            "Costs ~2x tokens — only set when the analyst's question is "
            "English-only and the conversation is Hindi/Hinglish."
        ),
    )


class ListTopicsInput(BaseModel):
    """Return the closed-set topic taxonomy with counts."""

    with_examples: bool = Field(
        default=False,
        description="If True, include one representative conv_id per topic.",
    )


# ---------------------------------------------------------------------------
# Tool OUTPUT schemas
# ---------------------------------------------------------------------------


class ConversationRow(BaseModel):
    conv_id: str
    customer_name: str | None
    agent_name: str | None
    start_ts: str
    end_ts: str
    turn_count: int
    topic: str
    customer_sentiment_overall: float | None
    resolution_flag: int
    escalation_flag: int
    agent_empathy_mean: float | None


class ConversationAggregateRow(BaseModel):
    group_key: str
    count: int
    avg_sentiment: float | None = None
    avg_empathy: float | None = None
    escalation_rate: float | None = None
    resolution_rate: float | None = None


class AgentRow(BaseModel):
    agent_name: str
    conv_count: int
    avg_customer_sentiment: float | None
    empathy_mean: float | None
    resolution_rate: float | None
    escalation_rate: float | None
    top_topics: list[tuple[str, int]]


class SemanticHit(BaseModel):
    conv_id: str
    similarity: float
    document_excerpt: str
    topic: str
    agent_name: str
    customer_sentiment_overall: float
    language: str
    escalation_flag: int


class TrajectoryTurn(BaseModel):
    turn_index: int
    role: str
    sentiment_label: str | None
    sentiment_score: float | None
    intent: str | None
    empathy_signal: str | None
    text_clean: str


class TrajectoryResponse(BaseModel):
    conv_id: str
    topic: str
    customer_sentiment_overall: float | None
    resolution_flag: int
    escalation_flag: int
    customer_arc: list[float]
    turns: list[TrajectoryTurn]


class FullTurn(BaseModel):
    turn_index: int
    role: str
    timestamp: str
    text_clean: str
    text_clean_en: str | None
    language: str | None
    sentiment_label: str | None
    sentiment_score: float | None
    intent: str | None
    empathy_signal: str | None
    is_escalation: int | None
    contains_pii: int | None


class ConversationDetail(BaseModel):
    conv_id: str
    customer_name: str
    agent_name: str
    start_ts: str
    end_ts: str
    topic: str
    customer_sentiment_overall: float | None
    resolution_flag: int
    escalation_flag: int
    turns: list[FullTurn]


class TopicEntry(BaseModel):
    id: str
    label: str
    description: str
    count_in_dataset: int
    example_conv_id: str | None = None


# ---------------------------------------------------------------------------
# Response envelope (the FastAPI /ask response)
# ---------------------------------------------------------------------------


class EvidenceItem(BaseModel):
    conv_id: str
    quote: str
    relevance: str = Field(
        description="One-line note explaining why this conversation supports the answer."
    )
    similarity: float | None = Field(
        default=None,
        description="Cosine similarity when this came from semantic_search.",
    )


class ToolCallTrace(BaseModel):
    tool: str
    arguments: dict
    result_summary: str


class AnswerEnvelope(BaseModel):
    answer: str = Field(description="Prose answer, 1-3 short paragraphs.")
    evidence: list[EvidenceItem] = Field(default_factory=list)
    tool_calls: list[ToolCallTrace] = Field(default_factory=list)
    reasoning_brief: str = Field(
        description="One sentence on how the answer was constructed."
    )
    uncertainty: str | None = Field(
        default=None,
        description=(
            "Optional caveat when the data weakly supports the answer. "
            "Do NOT use for sample-size hedging on agent-coaching questions — "
            "prefer concrete examples over disclaimers."
        ),
    )


class AskRequest(BaseModel):
    question: str
    session_id: str | None = Field(
        default=None,
        description="Optional session id; new one is minted if not provided.",
    )


class AskResponse(BaseModel):
    session_id: str
    envelope: AnswerEnvelope
