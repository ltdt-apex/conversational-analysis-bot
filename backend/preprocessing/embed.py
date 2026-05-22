"""ChromaDB seeding — one document per conversation.

Each Chroma document is the role-tagged turn list of a single conversation,
joined by newlines. We embed the *original* ``text_clean`` (not the English
translation) because the configured embedding model is multilingual — see
CLAUDE.md, "Bilingual data handling", rules 1 and 4.

The ``run()`` entry point is idempotent:

  * If the collection already holds exactly 3,000 docs and ``force`` is
    ``False``, we skip the rebuild.
  * If ``force`` is ``True``, we drop the collection and rebuild from scratch.

Conversation-level metadata is read from the ``conversations`` rollup table
when it has been populated; otherwise we derive everything from ``turns``
directly so this stage does not block the rollups stage running in parallel.
"""
from __future__ import annotations

import sqlite3
from collections import Counter
from dataclasses import dataclass
from typing import Iterable

from backend.config import Config
from backend.db import connect


COLLECTION_NAME = "conversations"
EXPECTED_DOC_COUNT = 3000
EMBED_BATCH = 64
CHROMA_ADD_BATCH = 500

# Use cosine similarity for the HNSW index, paired with L2-normalised
# embeddings below. This gives semantic_search() interpretable [0,1] scores
# (1.0 = identical, ~0 = unrelated) instead of unbounded L2-squared distances,
# and matches the training objective of paraphrase-multilingual-MiniLM.
_COLLECTION_METADATA = {"hnsw:space": "cosine"}


@dataclass
class _ConvDoc:
    conv_id: str
    document: str
    metadata: dict[str, str | int | float]


def run(cfg: Config, *, force: bool = False) -> None:
    """Build (or rebuild) the ``conversations`` Chroma collection.

    Parameters
    ----------
    cfg:
        Loaded :class:`backend.config.Config` — provides the SQLite path,
        Chroma persistence path, and the embedding model id.
    force:
        When True, the existing Chroma collection is dropped before reseeding.
    """
    # Lazy imports — keep the package importable on machines without these heavy
    # deps installed (e.g. CI lint stages).
    import chromadb
    from sentence_transformers import SentenceTransformer

    cfg.chroma_path.mkdir(parents=True, exist_ok=True)
    client = chromadb.PersistentClient(path=str(cfg.chroma_path))

    if force:
        try:
            client.delete_collection(COLLECTION_NAME)
            print(f"[embed] --force: dropped existing '{COLLECTION_NAME}' collection")
        except Exception:
            # Collection didn't exist yet — fine.
            pass

    collection = client.get_or_create_collection(
        name=COLLECTION_NAME, metadata=_COLLECTION_METADATA
    )
    existing = collection.count()
    if existing == EXPECTED_DOC_COUNT and not force:
        print(
            f"[embed] skipping — '{COLLECTION_NAME}' already has "
            f"{existing} documents (use --force to rebuild)"
        )
        return
    if 0 < existing < EXPECTED_DOC_COUNT and not force:
        # Partial collection from an interrupted prior run — start over so we
        # don't leak duplicate or stale documents.
        print(
            f"[embed] partial collection ({existing} docs) — recreating "
            f"to ensure consistency"
        )
        client.delete_collection(COLLECTION_NAME)
        collection = client.get_or_create_collection(
        name=COLLECTION_NAME, metadata=_COLLECTION_METADATA
    )

    with connect(cfg.sqlite_path) as conn:
        conv_meta = _load_conversation_rollups(conn)
        docs = list(_build_documents(conn, conv_meta))

    if not docs:
        raise RuntimeError(
            "[embed] no conversations found in turns table — run ingest_raw first"
        )

    print(
        f"[embed] preparing to embed {len(docs):,} conversations with "
        f"model={cfg.embedding_model}"
    )
    print("[embed] loading sentence-transformers model (first run downloads ~120MB)…")
    model = SentenceTransformer(cfg.embedding_model)

    ids: list[str] = []
    documents: list[str] = []
    metadatas: list[dict[str, str | int | float]] = []
    embeddings: list[list[float]] = []

    total = len(docs)
    for start in range(0, total, EMBED_BATCH):
        chunk = docs[start : start + EMBED_BATCH]
        chunk_texts = [d.document for d in chunk]
        # convert_to_numpy=True returns ndarray; convert to list of lists for Chroma.
        chunk_embeddings = model.encode(
            chunk_texts,
            batch_size=EMBED_BATCH,
            show_progress_bar=False,
            convert_to_numpy=True,
            # Unit-normalise so cosine similarity reduces to a dot product.
            normalize_embeddings=True,
        )
        for doc, vec in zip(chunk, chunk_embeddings):
            ids.append(doc.conv_id)
            documents.append(doc.document)
            metadatas.append(doc.metadata)
            embeddings.append(vec.tolist())
        done = min(start + EMBED_BATCH, total)
        print(f"  embedded {done:,}/{total:,} conversations", end="\r")
    print()

    # Flush to Chroma in larger batches; Chroma can comfortably take 500/req.
    for start in range(0, len(ids), CHROMA_ADD_BATCH):
        end = start + CHROMA_ADD_BATCH
        collection.add(
            ids=ids[start:end],
            documents=documents[start:end],
            metadatas=metadatas[start:end],
            embeddings=embeddings[start:end],
        )
        print(f"  added {min(end, len(ids)):,}/{len(ids):,} to Chroma", end="\r")
    print()

    final = collection.count()
    print(f"[embed] done — collection '{COLLECTION_NAME}' now has {final:,} documents")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _load_conversation_rollups(
    conn: sqlite3.Connection,
) -> dict[str, sqlite3.Row]:
    """Return ``{conv_id: row}`` for any rows already in ``conversations``.

    Tolerates a completely empty rollups table — the caller falls back to
    turn-level aggregation in that case.
    """
    try:
        rows = conn.execute(
            "SELECT conv_id, topic, customer_sentiment_overall, escalation_flag "
            "FROM conversations"
        ).fetchall()
    except sqlite3.OperationalError:
        # Schema not initialised — treat as empty.
        return {}
    return {r["conv_id"]: r for r in rows}


def _build_documents(
    conn: sqlite3.Connection,
    conv_meta: dict[str, sqlite3.Row],
) -> Iterable[_ConvDoc]:
    """Stream one :class:`_ConvDoc` per ``conv_id``.

    Turns are read in (conv_id, turn_index) order; per-conversation aggregates
    are computed as we walk.
    """
    cur = conn.execute(
        """
        SELECT conv_id, turn_index, timestamp, role,
               text_clean, language, sentiment_score, is_escalation,
               customer_name, agent_name
          FROM turns
         ORDER BY conv_id, turn_index
        """
    )

    current_id: str | None = None
    buf: list[sqlite3.Row] = []

    def flush(rows: list[sqlite3.Row]) -> _ConvDoc:
        return _assemble_doc(rows, conv_meta.get(rows[0]["conv_id"]))

    for row in cur:
        if row["conv_id"] != current_id:
            if buf:
                yield flush(buf)
                buf = []
            current_id = row["conv_id"]
        buf.append(row)
    if buf:
        yield flush(buf)


def _assemble_doc(
    rows: list[sqlite3.Row],
    rollup: sqlite3.Row | None,
) -> _ConvDoc:
    """Compose document text + metadata for a single conversation."""
    conv_id = rows[0]["conv_id"]

    # Document text — role-tagged turns in turn_index order. text_clean is
    # already in turn order because the upstream query sorts by turn_index.
    doc_lines = [f"[{r['role']}] {r['text_clean'] or ''}" for r in rows]
    document = "\n".join(doc_lines)

    # First non-null customer/agent names. Same value across the conv in our
    # data, but coalesce defensively.
    customer_name = _first_nonnull(rows, "customer_name") or ""
    agent_name = _first_nonnull(rows, "agent_name") or ""

    # Timestamps — turn timestamps are not monotonic, so take min/max.
    timestamps = [r["timestamp"] for r in rows if r["timestamp"]]
    start_ts = min(timestamps) if timestamps else ""
    end_ts = max(timestamps) if timestamps else ""

    # Language: majority vote across turns; ties → "mixed".
    lang_counts: Counter[str] = Counter(
        r["language"] for r in rows if r["language"]
    )
    if not lang_counts:
        language = "unknown"
    else:
        top_count = max(lang_counts.values())
        top_langs = [l for l, c in lang_counts.items() if c == top_count]
        language = top_langs[0] if len(top_langs) == 1 else "mixed"

    # Topic / sentiment / escalation — prefer rollup table; otherwise derive
    # from turns so we don't block on the parallel rollups stage.
    if rollup is not None:
        topic = rollup["topic"] or "unknown"
        sentiment_overall = (
            float(rollup["customer_sentiment_overall"])
            if rollup["customer_sentiment_overall"] is not None
            else _mean_customer_sentiment(rows)
        )
        escalation_flag = (
            int(rollup["escalation_flag"])
            if rollup["escalation_flag"] is not None
            else _any_escalation(rows)
        )
    else:
        topic = "unknown"
        sentiment_overall = _mean_customer_sentiment(rows)
        escalation_flag = _any_escalation(rows)

    metadata: dict[str, str | int | float] = {
        "conv_id": conv_id,
        "agent_name": agent_name,
        "customer_name": customer_name,
        "turn_count": len(rows),
        "start_ts": start_ts,
        "end_ts": end_ts,
        "topic": topic,
        "customer_sentiment_overall": float(sentiment_overall),
        "escalation_flag": int(escalation_flag),
        "language": language,
    }
    return _ConvDoc(conv_id=conv_id, document=document, metadata=metadata)


def _first_nonnull(rows: list[sqlite3.Row], col: str) -> str | None:
    for r in rows:
        v = r[col]
        if v:
            return v
    return None


def _mean_customer_sentiment(rows: list[sqlite3.Row]) -> float:
    scores = [
        r["sentiment_score"]
        for r in rows
        if r["role"] == "customer" and r["sentiment_score"] is not None
    ]
    if not scores:
        return 0.0
    return sum(scores) / len(scores)


def _any_escalation(rows: list[sqlite3.Row]) -> int:
    for r in rows:
        if r["is_escalation"]:
            return 1
    return 0
